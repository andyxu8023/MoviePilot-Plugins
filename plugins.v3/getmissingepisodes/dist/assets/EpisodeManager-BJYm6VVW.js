import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc, f as filterTabs, a as formatDateTime, b as actionHint, S as SettingsPanel, u as unwrapResponse, s as seasonChips, c as countMissing, e as existStatusMeta, D as DEFAULT_POSTER } from './SettingsPanel-C6XfKivv.js';

const {resolveComponent:_resolveComponent,createVNode:_createVNode,createElementVNode:_createElementVNode,openBlock:_openBlock,createElementBlock:_createElementBlock,createCommentVNode:_createCommentVNode,unref:_unref,toDisplayString:_toDisplayString,createTextVNode:_createTextVNode,withCtx:_withCtx,createBlock:_createBlock,renderList:_renderList,Fragment:_Fragment,normalizeClass:_normalizeClass} = await importShared('vue');


const _hoisted_1 = { class: "gme-header" };
const _hoisted_2 = { class: "gme-header__title" };
const _hoisted_3 = { class: "text-caption gme-muted" };
const _hoisted_4 = { key: 0 };
const _hoisted_5 = { key: 1 };
const _hoisted_6 = { key: 2 };
const _hoisted_7 = { class: "gme-header__actions" };
const _hoisted_8 = { class: "text-caption" };
const _hoisted_9 = { class: "text-h6" };
const _hoisted_10 = { class: "gme-toolbar" };
const _hoisted_11 = { class: "text-body-2 gme-muted" };
const _hoisted_12 = {
  key: 1,
  class: "gme-empty"
};
const _hoisted_13 = { class: "gme-card__body" };
const _hoisted_14 = { class: "gme-card__poster-fallback" };
const _hoisted_15 = { class: "gme-card__info" };
const _hoisted_16 = ["href"];
const _hoisted_17 = { class: "d-flex align-center mb-1" };
const _hoisted_18 = { class: "text-caption gme-muted" };
const _hoisted_19 = { class: "text-caption gme-muted" };
const _hoisted_20 = { class: "text-caption gme-muted" };
const _hoisted_21 = { class: "text-caption gme-muted" };
const _hoisted_22 = {
  key: 0,
  class: "gme-card__seasons"
};
const _hoisted_23 = { key: 0 };
const _hoisted_24 = {
  key: 1,
  class: "text-caption gme-muted"
};
const _hoisted_25 = { class: "text-caption gme-muted mt-2" };
const _hoisted_26 = { key: 0 };
const _hoisted_27 = { class: "text-caption gme-muted", style: "width: 100%" };
const _hoisted_28 = { class: "gme-toolbar" };
const _hoisted_29 = { class: "text-body-2 gme-muted" };

const {computed,onMounted,onUnmounted,reactive,ref} = await importShared('vue');


const _sfc_main = {
  __name: 'EpisodeManager',
  props: {
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'GetMissingEpisodes' },
  navKey: { type: String, default: 'main' },
  initialConfig: { type: Object, default: () => ({}) },
  initialTab: { type: String, default: 'board' },
  compact: { type: Boolean, default: false },
  showClose: { type: Boolean, default: false },
},
  emits: ['close', 'action'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;

const base = computed(() => `plugin/${props.pluginId}`);
const loading = ref(false);
const checking = ref(false);
const settingsOpen = ref(props.initialTab === 'config');
const activeFilter = ref('all');
const keyword = ref('');
const page = ref(1);
const pollTimer = ref(null);

const board = reactive({
  items: [],
  statistics: {},
  config: {},
  library_options: [],
  mediaserver_options: [],
  running: false,
  last_check: '',
  cron: '',
  enabled: false,
});

const snackbar = reactive({ show: false, text: '', color: 'success' });

/** 提示条统一入口。 */
function notify(text, color = 'success') {
  snackbar.text = text;
  snackbar.color = color;
  snackbar.show = true;
}

/** 拉取总览数据，silent 用于轮询时不闪加载态。 */
async function loadBoard(silent = false) {
  if (!silent) loading.value = true;
  try {
    const data = unwrapResponse(await props.api.get(`${base.value}/status`)) || {};
    Object.assign(board, {
      items: data.items || [],
      statistics: data.statistics || {},
      config: data.config || {},
      library_options: data.library_options || [],
      mediaserver_options: data.mediaserver_options || [],
      running: Boolean(data.running),
      last_check: data.last_check || '',
      cron: data.cron || '',
      enabled: Boolean(data.enabled),
    });
    if (board.running) startPolling();
    else stopPolling();
  } catch (error) {
    if (!silent) notify(error?.message || '加载剧集管家数据失败', 'error');
  } finally {
    loading.value = false;
  }
}

/** 检测进行中时每 6 秒刷新一次状态。 */
function startPolling() {
  if (pollTimer.value) return
  pollTimer.value = setInterval(() => loadBoard(true), 6000);
}

function stopPolling() {
  if (pollTimer.value) {
    clearInterval(pollTimer.value);
    pollTimer.value = null;
  }
}

/** 触发一次立即检测。 */
async function runCheck() {
  checking.value = true;
  try {
    unwrapResponse(await props.api.post(`${base.value}/run`, {}));
    notify('已触发一次检测，稍后自动刷新结果');
    startPolling();
    setTimeout(() => loadBoard(true), 3000);
  } catch (error) {
    notify(error?.message || '触发检测失败', 'error');
  } finally {
    checking.value = false;
  }
}

/** 调用单条记录的操作接口。 */
async function callAction(endpoint, item, label, query = '') {
  const url = `${base.value}/${endpoint}?key=${encodeURIComponent(item.unique)}${query}`;
  try {
    const envelope = await props.api.get(url);
    unwrapResponse(envelope);
    notify(envelope?.message || `${label}成功`);
    if (endpoint === 'set_all_exist_history') delete seasonSelection[item.unique];
    await loadBoard(true);
  } catch (error) {
    notify(error?.message || `${label}失败`, 'error');
  }
}

/** 每张卡片点选为「标记存在」的缺失季，键为记录 unique，值为季号数组。 */
const seasonSelection = reactive({});

/** 只有带「标记存在」按钮的状态才允许点选缺失季。 */
function canSelectSeasons(item) {
  return ['存在缺失', '已加订阅'].includes(item.exist_status);
}

/** 当前卡片已选中的季号数组。 */
function selectedSeasons(unique) {
  return seasonSelection[unique] || [];
}

/** 点一下缺失季标签即切换选中态，作为「标记存在」的作用范围。 */
function toggleSeasonChip(item, chip) {
  if (!canSelectSeasons(item)) return;
  const list = seasonSelection[item.unique] || [];
  const index = list.indexOf(chip.num);
  if (index >= 0) {
    list.splice(index, 1);
  } else {
    list.push(chip.num);
  }
  seasonSelection[item.unique] = list;
}

/** 选中态的视觉反馈统一走下面三个函数，模板与构建产物保持一致。 */
function chipChecked(item, chip) {
  return canSelectSeasons(item) && selectedSeasons(item.unique).includes(chip.num);
}
function chipVariant(item, chip) {
  return chipChecked(item, chip) ? 'flat' : 'outlined';
}
function chipColor(item, chip) {
  if (chipChecked(item, chip)) return 'success';
  return chip.missing.length ? 'error' : 'warning';
}
function chipTitle(item, chip) {
  if (!canSelectSeasons(item)) return chip.text;
  return chipChecked(item, chip) ? `${chip.text}（已选，点击取消）` : `${chip.text}（点击标记为已存在）`;
}

/** 卡片按钮点击入口：标记存在必须先点选缺失季，避免整条一键全部标记。 */
function onActionClick(name, item, label) {
  if (name === 'set_all_exist_history') {
    const selected = selectedSeasons(item.unique);
    if (!selected.length) {
      notify('请先点选要标记为已存在的缺失季', 'warning');
      return;
    }
    return callAction(name, item, label, `&seasons=${selected.join(',')}`);
  }
  return callAction(name, item, label);
}

/** 缺失处理方式三态，取值与后端 NoExistAction 逐字一致。 */
const ACTION_CYCLE = ['仅检查记录', '添加到订阅', '标记为存在'];

/** 下拉选项：空串表示该剧恢复跟随全局配置。 */
const ACTION_OPTIONS = [
  { title: '跟随全局', value: '' },
  ...ACTION_CYCLE.map(value => ({ title: value, value })),
];

/** 卡片按钮位窄，统一用两字短标签；菜单里仍是完整名称。 */
function actionShort(value) {
  const map = { 仅检查记录: '记录', 添加到订阅: '订阅', 标记为存在: '存在' };
  return map[value] || '全局'
}

/** 当前生效的全局缺失处理方式。 */
const currentAction = computed(() => board.config?.no_exist_action || ACTION_CYCLE[0]);

/** 顶栏下拉直接切换全局缺失处理方式，选择即保存。 */
async function setGlobalAction(value) {
  if (!value || value === currentAction.value) return;
  try {
    unwrapResponse(await props.api.post(`${base.value}/settings`, { no_exist_action: value }));
    notify(`缺失处理方式已设为「${value}」`);
    await loadBoard(true);
  } catch (error) {
    notify(error?.message || '设置缺失处理方式失败', 'error');
  }
}

/** 卡片下拉为单部剧指定缺失处理方式，空值表示跟随全局。 */
function setRecordAction(item, value) {
  if (value === (item.action_override || '')) return;
  return callAction('set_action_history', item, '缺失处理', `&action=${encodeURIComponent(value ?? '')}`)
}

/** 操作按钮定义，文案与配色沿用原 Vuetify JSON 页面。 */
const ACTION_DEFS = {
  add_subscribe_history: { text: '订阅缺失', color: 'primary' },
  set_all_exist_history: { text: '标记存在', color: 'success' },
  toggle_skip_history: { text: '跳过', color: 'warning' },
  toggle_follow_history: { text: '继续追新', color: 'info' },
  delete_history: { text: '删除记录', color: 'error' },
};

/** 各检测状态下的按钮顺序，与原页面 action_names 完全一致。 */
const ACTION_ORDER = {
  存在缺失: ['delete_history', 'set_all_exist_history', 'add_subscribe_history', 'toggle_skip_history'],
  已加订阅: ['delete_history', 'set_all_exist_history', 'toggle_skip_history'],
  全部存在: ['delete_history', 'toggle_skip_history'],
  获取失败: ['delete_history', 'toggle_skip_history'],
  已删除: ['delete_history', 'toggle_follow_history'],
};

/** 根据检测状态给出可用操作，与原 JSON 页面保持一致。 */
function availableActions(item) {
  const names = ACTION_ORDER[item.exist_status] || ['delete_history', 'toggle_skip_history'];
  return names.map(name => {
    const definition = ACTION_DEFS[name];
    let text = definition.text;
    if (name === 'toggle_skip_history') {
      text = item.skip ? '取消跳过' : '跳过'
    } else if (name === 'toggle_follow_history') {
      text = item.follow ? '停止追新' : '继续追新'
    } else if (name === 'set_all_exist_history') {
      const count = selectedSeasons(item.unique).length
      text = count ? `标记存在(${count})` : '标记存在'
    }
    return { key: name, text, color: definition.color }
  })
}

const pageSize = computed(() => (props.compact ? 12 : 24));

/** 统计卡片数量。 */
function statValue(key) {
  return Number(board.statistics?.[key] || 0)
}

/** 按过滤视图与关键字筛选记录。 */
const filteredItems = computed(() => {
  const text = String(keyword.value ?? '').trim().toLowerCase();
  return board.items.filter(item => {
    if (!item.tags?.includes(activeFilter.value)) return false
    if (!text) return true
    return (
      String(item.title || '').toLowerCase().includes(text) ||
      String(item.path || '').toLowerCase().includes(text)
    )
  })
});

/** 归一化搜索关键字，Vuetify clearable 清空时事件值为 null，需回落为空串。 */
function onKeywordInput(value) {
  keyword.value = typeof value === 'string' ? value : '';
  page.value = 1;
}

const totalPages = computed(() => Math.max(1, Math.ceil(filteredItems.value.length / pageSize.value)));

/** 底部翻页后回到卡片列表顶部，否则会停在新一页的下方看不到卡片。
 * 用瞬时滚动而非 smooth：平滑动画会被随后的列表重渲染打断，实测出现过分页后仍停在旧滚动位置。 */
function scrollToGridTop() {
  document.querySelector('.gme-root')?.scrollIntoView({ block: 'start' });
}

/** 底部页码条的切换入口：改页码并滚回列表顶部。 */
function changePage(value) {
  page.value = value;
  scrollToGridTop();
}

const pagedItems = computed(() => {
  const start = (page.value - 1) * pageSize.value;
  return filteredItems.value.slice(start, start + pageSize.value)
});

const currentFilterTitle = computed(
  () => filterTabs.find(tab => tab.key === activeFilter.value)?.title || '全部',
);

/** 切换过滤视图。 */
function selectFilter(key) {
  activeFilter.value = key;
  page.value = 1;
}

/** 生成卡片展示所需的派生信息。 */
function decorate(item) {
  const chips = seasonChips(item.seasons);
  // chip.season 只是展示标签（S01 这种），点选需要原始季号
  chips.forEach((chip, index) => {
    chip.num = Number((item.seasons || [])[index]?.season ?? 0);
  });
  const missing = countMissing(item.seasons);
  const meta = existStatusMeta(item.exist_status);
  let statusText = item.exist_status || '未知状态';
  if (item.exist_status === '存在缺失' && missing.seasonsCount) {
    statusText = `缺失${missing.seasonsCount}季, ${missing.episodesCount}集`;
  }
  return {
    ...item,
    chips,
    meta,
    statusText,
    poster: item.poster || DEFAULT_POSTER,
    href: item.tmdbid ? item.media_link : '#',
  }
}

/**
 * 卡片信息区背景：逐层照抄本实例订阅卡片「有背景图」时的图层（style-*.css / data-v-e9d64cc5）：
 * window-base #1d2026 实色底 → backdrop 原图(center top) → shade 90deg 十档阶梯 → scrim 黑色渐变。
 * 背景图本身不褪色；.subscribe-card-background(180deg,#1f293778,#1f2937) 属于无图占位分支，
 * 蒙在有图的卡上会整卡发灰，不要用。mask 羽化无法用在 background 上，左侧过渡由 shade 承担。
 */
const CARD_SHADE = '29, 32, 38';
const CARD_SHADE_LADDER = [
  [0, 1], [8, 0.97], [16, 0.9], [24, 0.78], [32, 0.62],
  [40, 0.45], [48, 0.28], [56, 0.14], [64, 0.05],
];

function cardBackground(item) {
  const url = String(item.backdrop || '');
  if (!/^https?:\/\//.test(url) || /["')\n\r]/.test(url)) return {};
  const shade = CARD_SHADE_LADDER
    .map(([stop, alpha]) => `rgba(${CARD_SHADE}, ${alpha}) ${stop}%`)
    .join(', ');
  return {
    backgroundImage: [
      'radial-gradient(70px 56px at 100% 0%, rgba(0, 0, 0, 0.5) 0%, transparent 100%)',
      'linear-gradient(0deg, rgba(0, 0, 0, 0.6) 0%, rgba(0, 0, 0, 0.35) 40%, transparent 70%)',
      `linear-gradient(90deg, ${shade}, transparent 72%)`,
      `url("${url}")`,
    ].join(', '),
    backgroundSize: 'auto, auto, auto, cover',
    backgroundPosition: '0 0, 0 0, 0 0, center top',
    backgroundRepeat: 'no-repeat',
  };
}

onMounted(() => {
  loadBoard();
});

onUnmounted(() => stopPolling());

return (_ctx, _cache) => {
  const _component_VIcon = _resolveComponent("VIcon");
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VCardText = _resolveComponent("VCardText");
  const _component_VCard = _resolveComponent("VCard");
  const _component_VCol = _resolveComponent("VCol");
  const _component_VRow = _resolveComponent("VRow");
  const _component_VTextField = _resolveComponent("VTextField");
  const _component_VSpacer = _resolveComponent("VSpacer");
  const _component_VPagination = _resolveComponent("VPagination");
  const _component_VProgressLinear = _resolveComponent("VProgressLinear");
  const _component_VImg = _resolveComponent("VImg");
  const _component_VChip = _resolveComponent("VChip");
  const _component_VBtnToggle = _resolveComponent("VBtnToggle");
  const _component_VMenu = _resolveComponent("VMenu");
  const _component_VList = _resolveComponent("VList");
  const _component_VListItem = _resolveComponent("VListItem");
  const _component_VDivider = _resolveComponent("VDivider");
  const _component_VDialog = _resolveComponent("VDialog");
  const _component_VSnackbar = _resolveComponent("VSnackbar");

  return (_openBlock(), _createElementBlock("div", {
    class: _normalizeClass(["gme-root", { 'gme-root--compact': __props.compact }])
  }, [
    _createElementVNode("div", _hoisted_1, [
      _createElementVNode("div", _hoisted_2, [
        _createVNode(_component_VIcon, {
          icon: "mdi-television-classic",
          size: "28",
          class: "mr-2",
          color: "primary"
        }),
        _createElementVNode("div", null, [
          _cache[9] || (_cache[9] = _createElementVNode("div", { class: "text-h6 font-weight-medium" }, "剧集管家", -1)),
          _createElementVNode("div", _hoisted_3, [
            (!board.enabled)
              ? (_openBlock(), _createElementBlock("span", _hoisted_4, "插件未启用，仅展示历史记录"))
              : (board.running)
                ? (_openBlock(), _createElementBlock("span", _hoisted_5, "正在检测媒体库缺失剧集…"))
                : (_openBlock(), _createElementBlock("span", _hoisted_6, "上次检查：" + _toDisplayString(_unref(formatDateTime)(board.last_check)) + " · 周期 " + _toDisplayString(board.cron || '默认 0 8 * * *'), 1))
          ])
        ])
      ]),
      _createElementVNode("div", _hoisted_7, [
        _createVNode(_component_VBtn, {
          "prepend-icon": "mdi-refresh",
          variant: "text",
          loading: loading.value,
          onClick: _cache[0] || (_cache[0] = $event => (loadBoard()))
        }, {
          default: _withCtx(() => [...(_cache[10] || (_cache[10] = [
            _createTextVNode(" 刷新 ", -1)
          ]))]),
          _: 1
        }, 8, ["loading"]),
        _createVNode(_component_VBtn, {
          "prepend-icon": "mdi-magnify-plus-outline",
          variant: "text",
          loading: checking.value,
          onClick: runCheck
        }, {
          default: _withCtx(() => [...(_cache[11] || (_cache[11] = [
            _createTextVNode(" 立即检查 ", -1)
          ]))]),
          _: 1
        }, 8, ["loading"]),
        _createVNode(_component_VBtn, {
          "prepend-icon": "mdi-tune-variant",
          variant: "text",
          "append-icon": "mdi-menu-down"
        }, {
          default: _withCtx(() => [
            _createTextVNode("全局缺失处理：" + _toDisplayString(currentAction.value), 1),
            _createVNode(_component_VMenu, { activator: "parent" }, {
              default: _withCtx(() => [
                _createVNode(_component_VList, { density: "compact" }, {
                  default: _withCtx(() => [
                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(ACTION_CYCLE, (option) => {
                      return (_openBlock(), _createBlock(_component_VListItem, {
                        key: option,
                        "prepend-icon": option === currentAction.value ? 'mdi-check' : undefined,
                        title: option,
                        onClick: $event => (setGlobalAction(option))
                      }, null, 8, ["prepend-icon", "title", "onClick"]))
                    }), 128))
                  ]),
                  _: 1
                })
              ]),
              _: 1
            })
          ]),
          _: 1
        }),
        _createVNode(_component_VBtn, {
          "prepend-icon": "mdi-book-open-outline",
          variant: "text",
          href: "https://github.com/andyxu8023/MoviePilot-Plugins/blob/main/plugins.v3/getmissingepisodes/README.md",
          target: "_blank",
          rel: "noopener noreferrer"
        }, {
          default: _withCtx(() => [...(_cache[15] || (_cache[15] = [
            _createTextVNode("使用说明", -1)
          ]))]),
          _: 1
        }),
        _createVNode(_component_VBtn, {
          "prepend-icon": "mdi-cog-outline",
          variant: "text",
          onClick: _cache[1] || (_cache[1] = $event => (settingsOpen.value = true))
        }, {
          default: _withCtx(() => [...(_cache[12] || (_cache[12] = [
            _createTextVNode("设置", -1)
          ]))]),
          _: 1
        }),
        (__props.showClose)
          ? (_openBlock(), _createBlock(_component_VBtn, {
              key: 0,
              icon: "mdi-close",
              variant: "text",
              "aria-label": "关闭",
              onClick: _cache[2] || (_cache[2] = $event => (emit('close')))
            }))
          : _createCommentVNode("", true)
      ])
    ]),
    _createVNode(_component_VRow, {
      dense: "",
      class: "gme-stats"
    }, {
      default: _withCtx(() => [
        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(_unref(filterTabs), (tab) => {
          return (_openBlock(), _createBlock(_component_VCol, {
            key: tab.key,
            cols: "6",
            sm: "4",
            md: "3",
            lg: "2",
            xl: "1"
          }, {
            default: _withCtx(() => [
              _createVNode(_component_VCard, {
                variant: "tonal",
                color: activeFilter.value === tab.key ? tab.color : undefined,
                class: _normalizeClass(['gme-stat', { 'gme-stat--active': activeFilter.value === tab.key }]),
                onClick: $event => (selectFilter(tab.key))
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VCardText, { class: "d-flex align-center pa-3" }, {
                    default: _withCtx(() => [
                      _createVNode(_component_VIcon, {
                        icon: tab.icon,
                        color: activeFilter.value === tab.key ? undefined : tab.color,
                        class: "mr-2"
                      }, null, 8, ["icon", "color"]),
                      _createElementVNode("div", null, [
                        _createElementVNode("div", _hoisted_8, _toDisplayString(tab.title), 1),
                        _createElementVNode("div", _hoisted_9, _toDisplayString(statValue(tab.key)) + " 部", 1)
                      ])
                    ]),
                    _: 2
                  }, 1024)
                ]),
                _: 2
              }, 1032, ["color", "class", "onClick"])
            ]),
            _: 2
          }, 1024))
        }), 128))
      ]),
      _: 1
    }),
    _createElementVNode("div", _hoisted_10, [
      _createVNode(_component_VTextField, {
        "model-value": keyword.value,
        "prepend-inner-icon": "mdi-magnify",
        placeholder: "搜索剧名或路径",
        clearable: "",
        density: "comfortable",
        "hide-details": "",
        class: "gme-toolbar__search",
        "onUpdate:modelValue": onKeywordInput
      }, null, 8, ["model-value"]),
      _createElementVNode("div", _hoisted_11, _toDisplayString(currentFilterTitle.value) + " · 共 " + _toDisplayString(filteredItems.value.length) + " 部 ", 1),
      _createVNode(_component_VSpacer),
      (totalPages.value > 1)
        ? (_openBlock(), _createBlock(_component_VPagination, {
            key: 0,
            modelValue: page.value,
            "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((page).value = $event)),
            length: totalPages.value,
            "total-visible": __props.compact ? 5 : 7,
            density: "comfortable"
          }, null, 8, ["modelValue", "length", "total-visible"]))
        : _createCommentVNode("", true)
    ]),
    (loading.value)
      ? (_openBlock(), _createBlock(_component_VProgressLinear, {
          key: 0,
          indeterminate: "",
          color: "primary",
          class: "mb-2"
        }))
      : _createCommentVNode("", true),
    (!loading.value && !pagedItems.value.length)
      ? (_openBlock(), _createElementBlock("div", _hoisted_12, [
          _createVNode(_component_VIcon, {
            icon: "mdi-inbox-outline",
            size: "42",
            class: "mb-2 gme-muted"
          }),
          _cache[13] || (_cache[13] = _createElementVNode("div", { class: "text-body-1 gme-muted" }, "当前视图没有记录，可先点击“立即检查”生成检测结果", -1))
        ]))
      : _createCommentVNode("", true),
    _createVNode(_component_VRow, { dense: "" }, {
      default: _withCtx(() => [
        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(pagedItems.value, (raw) => {
          return (_openBlock(), _createBlock(_component_VCol, {
            key: raw.unique,
            cols: "12",
            sm: "6",
            md: "4",
            lg: "3",
            xl: "3"
          }, {
            default: _withCtx(() => [
              _createVNode(_component_VCard, {
                variant: "tonal",
                class: "gme-card h-100 d-flex flex-column"
              }, {
                default: _withCtx(() => [
                  _createElementVNode("div", { class: "gme-card__body", style: cardBackground(raw) }, [
                    _createVNode(_component_VImg, {
                      src: decorate(raw).poster,
                      cover: "",
                      height: "200",
                      width: "134",
                      class: "gme-card__poster"
                    }, {
                      placeholder: _withCtx(() => [
                        _createElementVNode("div", _hoisted_14, [
                          _createVNode(_component_VIcon, { icon: "mdi-image-off-outline" })
                        ])
                      ]),
                      _: 1
                    }, 8, ["src"]),
                    _createElementVNode("div", _hoisted_15, [
                      _createElementVNode("a", {
                        href: decorate(raw).href,
                        target: "_blank",
                        class: "gme-card__title text-subtitle-1"
                      }, _toDisplayString(raw.title || '未知'), 9, _hoisted_16),
                      _createElementVNode("div", _hoisted_17, [
                        _createVNode(_component_VChip, {
                          size: "x-small",
                          variant: "flat",
                          color: decorate(raw).meta.color,
                          "prepend-icon": decorate(raw).meta.icon,
                          class: "mr-1"
                        }, {
                          default: _withCtx(() => [
                            _createTextVNode(_toDisplayString(decorate(raw).statusText), 1)
                          ]),
                          _: 2
                        }, 1032, ["color", "prepend-icon"]),
                        (raw.skip)
                          ? (_openBlock(), _createBlock(_component_VChip, {
                              key: 0,
                              size: "x-small",
                              variant: "outlined",
                              color: "warning"
                            }, {
                              default: _withCtx(() => [...(_cache[14] || (_cache[14] = [
                                _createTextVNode("已跳过", -1)
                              ]))]),
                              _: 1
                            }))
                          : _createCommentVNode("", true),
                        _createCommentVNode("", true),
                        (raw.follow)
                          ? (_openBlock(), _createBlock(_component_VChip, {
                              key: 0,
                              size: "x-small",
                              variant: "flat",
                              color: "info",
                              "prepend-icon": "mdi-bell-plus-outline"
                            }, {
                              default: _withCtx(() => [
                                _createTextVNode("追新中", -1)
                              ]),
                              _: 1
                            }))
                          : _createCommentVNode("", true)
                      ]),
                      _createElementVNode("div", _hoisted_18, "剧集状态：" + _toDisplayString(raw.status_cn || '未知'), 1),
                      _createElementVNode("div", _hoisted_19, "开播年份：" + _toDisplayString(raw.year || '未知') + " · 评分 " + _toDisplayString(raw.vote_average ?? '-'), 1),
                      _createElementVNode("div", _hoisted_20, "最后播出：" + _toDisplayString(raw.last_air_date || '未知'), 1),
                      _createElementVNode("div", _hoisted_21, "检查时间：" + _toDisplayString(raw.last_check || '暂无'), 1),
                      (raw.deleted)
                        ? (_openBlock(), _createElementBlock(_Fragment, { key: 0 }, [
                            _createElementVNode("div", _hoisted_18, "删除时间：" + _toDisplayString(formatDateTime(raw.deleted_at)), 1),
                            (raw.existing_label)
                              ? (_openBlock(), _createElementBlock("div", _hoisted_19, "删除前存在：" + _toDisplayString(raw.existing_label), 1))
                              : _createCommentVNode("", true),
                            (raw.follow_note)
                              ? (_openBlock(), _createElementBlock("div", _hoisted_20, "追新结果：" + _toDisplayString(raw.follow_note), 1))
                              : _createCommentVNode("", true),
                            (raw.follow)
                              ? (_openBlock(), _createElementBlock("div", _hoisted_21, "缺失季将整季重订（含已删除的集），新季自动补订"))
                              : _createCommentVNode("", true)
                          ], 64))
                        : _createCommentVNode("", true),
                      (decorate(raw).chips.length)
                        ? (_openBlock(), _createElementBlock("div", _hoisted_22, [
                            (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(decorate(raw).chips, (chip) => {
                              return (_openBlock(), _createBlock(_component_VChip, {
                                key: chip.season,
                                size: "x-small",
                                variant: chipVariant(raw, chip),
                                color: chipColor(raw, chip),
                                "prepend-icon": chipChecked(raw, chip) ? 'mdi-check' : undefined,
                                title: chipTitle(raw, chip),
                                style: canSelectSeasons(raw) ? { cursor: 'pointer', userSelect: 'none' } : {},
                                onClick: $event => (toggleSeasonChip(raw, chip))
                              }, {
                                default: _withCtx(() => [
                                  _createTextVNode(_toDisplayString(chip.season) + " ", 1),
                                  (chip.missing.length)
                                    ? (_openBlock(), _createElementBlock(_Fragment, { key: 0 }, [
                                        _createTextVNode(" E" + _toDisplayString(chip.missing.slice(0, 6).join(',E')), 1),
                                        (chip.missing.length > 6)
                                          ? (_openBlock(), _createElementBlock("span", _hoisted_23, "…"))
                                          : _createCommentVNode("", true)
                                      ], 64))
                                    : (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
                                        _createTextVNode("缺整季")
                                      ], 64))
                                ]),
                                _: 2
                              }, 1032, ["variant", "color", "prepend-icon", "title", "style", "onClick"]))
                            }), 128)),
                            (canSelectSeasons(raw))
                              ? (_openBlock(), _createElementBlock("div", _hoisted_27, "点选缺失季后再「标记存在」，未选中的季继续按缺失补全"))
                              : _createCommentVNode("", true)
                          ]))
                        : _createCommentVNode("", true),
                      (raw.ignored_seasons?.length)
                        ? (_openBlock(), _createElementBlock("div", _hoisted_24, " 已忽略季：" + _toDisplayString(raw.ignored_seasons.join('、')), 1))
                        : _createCommentVNode("", true)
                    ])
                  ], 16, ["style"]),
                  _createVNode(_component_VBtnToggle, {
                    variant: "tonal",
                    rounded: "0",
                    class: "gme-card__actions d-flex mt-auto",
                    style: {"width":"100%","display":"flex"}
                  }, {
                    default: _withCtx(() => [
                      (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(availableActions(raw), (action) => {
                        return (_openBlock(), _createBlock(_component_VBtn, {
                          key: action.key,
                          class: _normalizeClass(`text-${action.color}`),
                          variant: "tonal",
                          style: {"height":"100%","width":"100%","flex":"1"},
                          onClick: $event => (onActionClick(action.key, raw, action.text))
                        }, {
                          default: _withCtx(() => [
                            _createTextVNode(_toDisplayString(action.text), 1)
                          ]),
                          _: 2
                        }, 1032, ["class", "onClick"]))
                      }), 128))
                    ]),
                    _: 2
                  }, 1024),
                  _createVNode(_component_VBtn, {
                    variant: "tonal",
                    "prepend-icon": "mdi-tune-variant",
                    "append-icon": "mdi-menu-down",
                    title: "缺失处理方式",
                    class: "gme-card__action-extra text-none text-primary",
                    style: {"width":"100%","flex":"none","border-radius":"0"}
                  }, {
                    default: _withCtx(() => [
                      _createTextVNode("缺失处理：" + _toDisplayString(actionShort(raw.action_override)), 1),
                      _createVNode(_component_VMenu, { activator: "parent" }, {
                        default: _withCtx(() => [
                          _createVNode(_component_VList, { density: "compact" }, {
                            default: _withCtx(() => [
                              (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(ACTION_OPTIONS, (option) => {
                                return (_openBlock(), _createBlock(_component_VListItem, {
                                  key: option.value,
                                  "prepend-icon": (option.value || '') === (raw.action_override || '') ? 'mdi-check' : undefined,
                                  title: option.title,
                                  onClick: $event => (setRecordAction(raw, option.value))
                                }, null, 8, ["prepend-icon", "title", "onClick"]))
                              }), 128))
                            ]),
                            _: 1
                          })
                        ]),
                        _: 1
                      })
                    ]),
                    _: 1
                  })
                ]),
                _: 2
              }, 1024)
            ]),
            _: 2
          }, 1024))
        }), 128))
      ]),
      _: 1
    }),
    (totalPages.value > 1)
      ? (_openBlock(), _createElementBlock("div", _hoisted_28, [
          _createElementVNode("div", _hoisted_29, " 共 " + _toDisplayString(filteredItems.value.length) + " 部 · 第 " + _toDisplayString(page.value) + "/" + _toDisplayString(totalPages.value) + " 页 ", 1),
          _createVNode(_component_VSpacer),
          _createVNode(_component_VPagination, {
            "model-value": page.value,
            length: totalPages.value,
            "total-visible": __props.compact ? 5 : 7,
            density: "comfortable",
            "onUpdate:modelValue": $event => (changePage($event))
          }, null, 8, ["model-value", "length", "total-visible", "onUpdate:modelValue"])
        ]))
      : _createCommentVNode("", true),
    _createElementVNode("div", _hoisted_25, [
      _createTextVNode(" 缺失处理方式：" + _toDisplayString(_unref(actionHint)(board.config.no_exist_action)) + " ", 1),
      (board.config.whitelist_librarys?.length)
        ? (_openBlock(), _createElementBlock("span", _hoisted_26, " · 媒体库白名单 " + _toDisplayString(board.config.whitelist_librarys.length) + " 个 ", 1))
        : _createCommentVNode("", true)
    ]),
    _createVNode(_component_VDialog, {
      modelValue: settingsOpen.value,
      "onUpdate:modelValue": _cache[7] || (_cache[7] = $event => ((settingsOpen).value = $event)),
      "max-width": "56rem",
      scrollable: ""
    }, {
      default: _withCtx(() => [
        _createVNode(_component_VCard, { title: "剧集管家设置" }, {
          append: _withCtx(() => [
            _createVNode(_component_VBtn, {
              icon: "mdi-close",
              variant: "text",
              onClick: _cache[4] || (_cache[4] = $event => (settingsOpen.value = false))
            })
          ]),
          default: _withCtx(() => [
            _createVNode(_component_VDivider),
            _createVNode(_component_VCardText, null, {
              default: _withCtx(() => [
                _createVNode(SettingsPanel, {
                  api: __props.api,
                  "plugin-id": __props.pluginId,
                  config: board.config,
                  "library-options": board.library_options,
                  "mediaserver-options": board.mediaserver_options,
                  onSaved: _cache[5] || (_cache[5] = payload => Object.assign(board, payload || {})),
                  onMessage: _cache[6] || (_cache[6] = message => notify(message.text, message.color))
                }, null, 8, ["api", "plugin-id", "config", "library-options", "mediaserver-options"])
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue"]),
    _createVNode(_component_VSnackbar, {
      modelValue: snackbar.show,
      "onUpdate:modelValue": _cache[8] || (_cache[8] = $event => ((snackbar.show) = $event)),
      color: snackbar.color,
      timeout: "3000",
      location: "bottom"
    }, {
      default: _withCtx(() => [
        _createTextVNode(_toDisplayString(snackbar.text), 1)
      ]),
      _: 1
    }, 8, ["modelValue", "color"])
  ], 2))
}
}

};
const EpisodeManager = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-65b51c87"]]);

export { EpisodeManager as E };
