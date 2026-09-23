import { importShared } from './__federation_fn_import-JrT3xvdd.js';

/** 剧集管家联邦界面使用的公共工具。 */

/** 默认海报占位图，与后端 default_poster_path 保持一致。 */
const DEFAULT_POSTER = '/assets/no-image-CweBJ8Ee.jpeg';

/** 统一提取宿主 API 客户端与标准响应模型中的业务数据。 */
function unwrapResponse(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'success')) {
    if (response.success === false) throw new Error(response.message || '操作失败')
    return response.data
  }
  return response?.data ?? response
}

/** 九类视图过滤定义，顺序与标题对应工作台统计卡片，key 与后端 tags 一致。 */
const filterTabs = [
  { key: 'all', title: '所有剧集', icon: 'mdi-database-outline', color: 'primary' },
  { key: 'total', title: '总处理', icon: 'mdi-playlist-check', color: 'primary' },
  { key: 'all_exist', title: '全部存在', icon: 'mdi-check-all', color: 'success' },
  { key: 'finished', title: '已完结', icon: 'mdi-check-circle-outline', color: 'success' },
  { key: 'skipped', title: '已跳过', icon: 'mdi-skip-next-outline', color: 'secondary' },
  { key: 'no_exist', title: '存在缺失', icon: 'mdi-alert-outline', color: 'warning' },
  { key: 'not_all_no_exist', title: '已有季缺失', icon: 'mdi-target', color: 'error' },
  { key: 'added_rss', title: '已订阅', icon: 'mdi-rss', color: 'info' },
  { key: 'failed', title: '未识别', icon: 'mdi-bug-outline', color: 'error' },
];

/** 返回检测状态对应的主题色与图标。 */
function existStatusMeta(status) {
  const states = {
    全部存在: { color: 'success', icon: 'mdi-check-circle-outline' },
    已加订阅: { color: 'info', icon: 'mdi-rss' },
    存在缺失: { color: 'warning', icon: 'mdi-alert-outline' },
    获取失败: { color: 'error', icon: 'mdi-help-circle-outline' },
    未知状态: { color: 'secondary', icon: 'mdi-help-box-outline' },
  };
  return states[status] || states.未知状态
}

/** 汇总一季缺失集数的展示文本。 */
function seasonLabel(season) {
  const number = Number(season.season ?? 0);
  return `S${String(number).padStart(2, '0')}`
}

/** 把季与集缺失明细整理成紧凑的展示片段。 */
function seasonChips(seasons = []) {
  return (seasons || []).map(season => {
    const missing = (season.episode_no_exist || []).filter(Boolean);
    return {
      season: seasonLabel(season),
      missing,
      total: season.episode_total_unfiltered || season.episode_total || 0,
      ignored: Boolean(season.ignored),
      text: missing.length
        ? `${seasonLabel(season)} 缺 ${missing.length} 集`
        : `${seasonLabel(season)} 缺整季`,
    }
  })
}

/** 统计一部剧的缺失季数与缺失集数。 */
function countMissing(seasons = []) {
  let seasonsCount = 0;
  let episodesCount = 0
  ;(seasons || []).forEach(season => {
    const missing = season.episode_no_exist || [];
    if (missing.length) {
      seasonsCount += 1;
      episodesCount += missing.length;
    } else {
      seasonsCount += 1;
      episodesCount += season.episode_total || season.episode_total_unfiltered || 0;
    }
  });
  return { seasonsCount, episodesCount }
}

/** 格式化后端返回的时间文本，保留原始值兜底。 */
function formatDateTime(value) {
  if (!value) return '暂无'
  const date = new Date(String(value).replace(' ', 'T'));
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

/** 返回缺失处理方式的中文说明。 */
function actionHint(noExistAction) {
  const hints = {
    仅检查记录: '仅记录缺失，不自动订阅',
    添加到订阅: '发现缺失自动加入订阅',
    标记为存在: '发现缺失自动标记为存在',
  };
  return hints[noExistAction] || noExistAction || ''
}

const _export_sfc = (sfc, props) => {
  const target = sfc.__vccOpts || sfc;
  for (const [key, val] of props) {
    target[key] = val;
  }
  return target;
};

const {resolveComponent:_resolveComponent,createVNode:_createVNode,withCtx:_withCtx,createTextVNode:_createTextVNode,openBlock:_openBlock,createBlock:_createBlock} = await importShared('vue');


const {computed,reactive,ref,watch} = await importShared('vue');


const _sfc_main = {
  __name: 'SettingsPanel',
  props: {
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'GetMissingEpisodes' },
  config: { type: Object, default: () => ({}) },
  libraryOptions: { type: Array, default: () => [] },
  mediaserverOptions: { type: Array, default: () => [] },
  // host：交给宿主标准 save 流程（由宿主写配置并弹出保存通知）；api：组件自行调用插件接口保存
  submitMode: { type: String, default: 'api' },
},
  emits: ['saved', 'message', 'submit'],
  setup(__props, { expose: __expose, emit: __emit }) {

const props = __props;

const emit = __emit;

const saving = ref(false);
const base = computed(() => `plugin/${props.pluginId}`);

/** 把后端可能是字符串或数组的列表配置规整为数组。 */
function toList(value) {
  return Array.isArray(value)
    ? value.map(item => String(item).trim()).filter(Boolean)
    : String(value || '')
        .split(',')
        .map(item => item.trim())
        .filter(Boolean)
}

/** 把后端配置规整为表单可直接编辑的草稿。 */
function toDraft(config = {}) {
  return {
    enabled: Boolean(config.enabled),
    show_sidebar_nav: config.show_sidebar_nav !== false,
    cron: String(config.cron || ''),
    onlyonce: false,
    clear: false,
    only_aired: config.only_aired !== false,
    only_season_exist: config.only_season_exist !== false,
    auto_skip_finished: Boolean(config.auto_skip_finished),
    include_s00_season: Boolean(config.include_s00_season),
    no_exist_action: String(config.no_exist_action || '仅检查记录'),
    whitelist_librarys: toList(config.whitelist_librarys),
    whitelist_media_servers: toList(config.whitelist_media_servers),
    save_path_replaces: Array.isArray(config.save_path_replaces)
      ? config.save_path_replaces.join('\n')
      : String(config.save_path_replaces || ''),
  }
}

const draft = reactive(toDraft(props.config));
watch(
  () => props.config,
  next => Object.assign(draft, toDraft(next || {})),
  { deep: true },
);

const actionItems = [
  { title: '仅检查记录', value: '仅检查记录' },
  { title: '添加到订阅', value: '添加到订阅' },
  { title: '标记为存在', value: '标记为存在' },
];

const libraryItems = computed(() =>
  (props.libraryOptions || []).map(name => ({ title: name, value: name })),
);

const mediaserverItems = computed(() =>
  (props.mediaserverOptions || []).map(name => ({ title: name, value: name })),
);

/** 提交设置并让后端回传最新总览数据。 */
async function save() {
  saving.value = true;
  try {
    const payload = { ...draft };
    payload.whitelist_media_servers = toList(payload.whitelist_media_servers).join(',');
    payload.whitelist_librarys = toList(payload.whitelist_librarys);
    if (props.submitMode === 'host') {
      emit('submit', payload);
      return
    }
    const data = unwrapResponse(await props.api.post(`${base.value}/settings`, payload));
    emit('saved', data);
    emit('message', { text: '设置已保存', color: 'success' });
  } catch (error) {
    emit('message', { text: error?.message || '保存失败', color: 'error' });
  } finally {
    draft.onlyonce = false;
    draft.clear = false;
    saving.value = false;
  }
}

__expose({ save, saving });

return (_ctx, _cache) => {
  const _component_VSwitch = _resolveComponent("VSwitch");
  const _component_VCol = _resolveComponent("VCol");
  const _component_VTextField = _resolveComponent("VTextField");
  const _component_VSelect = _resolveComponent("VSelect");
  const _component_VCombobox = _resolveComponent("VCombobox");
  const _component_VTextarea = _resolveComponent("VTextarea");
  const _component_VRow = _resolveComponent("VRow");
  const _component_VAlert = _resolveComponent("VAlert");
  const _component_VCardText = _resolveComponent("VCardText");
  const _component_VDivider = _resolveComponent("VDivider");
  const _component_VSpacer = _resolveComponent("VSpacer");
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VCardActions = _resolveComponent("VCardActions");
  const _component_VCard = _resolveComponent("VCard");

  return (_openBlock(), _createBlock(_component_VCard, {
    flat: "",
    class: "settings-panel"
  }, {
    default: _withCtx(() => [
      _createVNode(_component_VCardText, null, {
        default: _withCtx(() => [
          _createVNode(_component_VRow, { dense: "" }, {
            default: _withCtx(() => [
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSwitch, {
                    modelValue: draft.enabled,
                    "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((draft.enabled) = $event)),
                    color: "primary",
                    inset: "",
                    "hide-details": "",
                    label: "启用插件"
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSwitch, {
                    modelValue: draft.show_sidebar_nav,
                    "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((draft.show_sidebar_nav) = $event)),
                    color: "primary",
                    inset: "",
                    label: "显示侧边栏入口",
                    hint: "开启后在主界面左侧导航的“订阅”分组中显示剧集管家入口",
                    "persistent-hint": ""
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSwitch, {
                    modelValue: draft.only_aired,
                    "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((draft.only_aired) = $event)),
                    color: "primary",
                    inset: "",
                    label: "仅订阅已开播剧集",
                    hint: "关闭后未开播剧集也会加入订阅",
                    "persistent-hint": ""
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSwitch, {
                    modelValue: draft.only_season_exist,
                    "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((draft.only_season_exist) = $event)),
                    color: "primary",
                    inset: "",
                    label: "仅检查已有季缺失",
                    hint: "开启：只检查已存在的季是否缺集；关闭：同时检查缺失的季",
                    "persistent-hint": ""
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSwitch, {
                    modelValue: draft.auto_skip_finished,
                    "onUpdate:modelValue": _cache[4] || (_cache[4] = $event => ((draft.auto_skip_finished) = $event)),
                    color: "primary",
                    inset: "",
                    label: "自动跳过已完结剧集",
                    hint: "TMDB 状态可能被误标，出现漏检时可手动取消跳过",
                    "persistent-hint": ""
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSwitch, {
                    modelValue: draft.include_s00_season,
                    "onUpdate:modelValue": _cache[5] || (_cache[5] = $event => ((draft.include_s00_season) = $event)),
                    color: "primary",
                    inset: "",
                    label: "包含 S00 季检测",
                    hint: "开启后特别季（S00）也参与缺失检测",
                    "persistent-hint": ""
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSwitch, {
                    modelValue: draft.onlyonce,
                    "onUpdate:modelValue": _cache[6] || (_cache[6] = $event => ((draft.onlyonce) = $event)),
                    color: "warning",
                    inset: "",
                    "hide-details": "",
                    label: "立即运行一次"
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSwitch, {
                    modelValue: draft.clear,
                    "onUpdate:modelValue": _cache[7] || (_cache[7] = $event => ((draft.clear) = $event)),
                    color: "error",
                    inset: "",
                    "hide-details": "",
                    label: "清理检查记录"
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VTextField, {
                    modelValue: draft.cron,
                    "onUpdate:modelValue": _cache[8] || (_cache[8] = $event => ((draft.cron) = $event)),
                    label: "执行周期",
                    placeholder: "5位 cron 表达式，留空默认 0 8 * * *",
                    clearable: ""
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSelect, {
                    modelValue: draft.no_exist_action,
                    "onUpdate:modelValue": _cache[9] || (_cache[9] = $event => ((draft.no_exist_action) = $event)),
                    items: actionItems,
                    label: "缺失处理方式",
                    variant: "outlined"
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, {
                cols: "12",
                md: "4"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VCombobox, {
                    modelValue: draft.whitelist_media_servers,
                    "onUpdate:modelValue": _cache[10] || (_cache[10] = $event => ((draft.whitelist_media_servers) = $event)),
                    items: mediaserverItems.value.map(item => item.value),
                    label: "媒体服务器名称白名单",
                    placeholder: "留空默认全部，多个名称用英文逗号分隔",
                    chips: "",
                    multiple: "",
                    "closable-chips": "",
                    delimiters: [',']
                  }, null, 8, ["modelValue", "items"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, { cols: "12" }, {
                default: _withCtx(() => [
                  _createVNode(_component_VSelect, {
                    modelValue: draft.whitelist_librarys,
                    "onUpdate:modelValue": _cache[11] || (_cache[11] = $event => ((draft.whitelist_librarys) = $event)),
                    items: libraryItems.value,
                    label: "电视剧媒体库白名单",
                    placeholder: "请选择要检查的电视剧媒体库",
                    multiple: "",
                    chips: "",
                    "closable-chips": "",
                    variant: "outlined"
                  }, null, 8, ["modelValue", "items"])
                ]),
                _: 1
              }),
              _createVNode(_component_VCol, { cols: "12" }, {
                default: _withCtx(() => [
                  _createVNode(_component_VTextarea, {
                    modelValue: draft.save_path_replaces,
                    "onUpdate:modelValue": _cache[12] || (_cache[12] = $event => ((draft.save_path_replaces) = $event)),
                    label: "下载路径替换，一行一个",
                    rows: "4",
                    "auto-grow": "",
                    placeholder: "用英文冒号分割源路径与目标路径，例如 /media/library:/downloads"
                  }, null, 8, ["modelValue"])
                ]),
                _: 1
              })
            ]),
            _: 1
          }),
          _createVNode(_component_VAlert, {
            type: "info",
            variant: "tonal",
            density: "compact",
            class: "mt-2"
          }, {
            default: _withCtx(() => [...(_cache[13] || (_cache[13] = [
              _createTextVNode(" 首次检测建议缺失处理方式选择“仅检查记录”，按记录手动跳过无需检测的剧集后再改为“添加到订阅”，可避免因 TMDB 信息错误产生误订阅。 ", -1)
            ]))]),
            _: 1
          })
        ]),
        _: 1
      }),
      _createVNode(_component_VDivider),
      _createVNode(_component_VCardActions, null, {
        default: _withCtx(() => [
          _createVNode(_component_VSpacer),
          _createVNode(_component_VBtn, {
            color: "primary",
            variant: "flat",
            loading: saving.value,
            onClick: save
          }, {
            default: _withCtx(() => [...(_cache[14] || (_cache[14] = [
              _createTextVNode("保存设置", -1)
            ]))]),
            _: 1
          }, 8, ["loading"])
        ]),
        _: 1
      })
    ]),
    _: 1
  }))
}
}

};
const SettingsPanel = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-34910c2f"]]);

export { DEFAULT_POSTER as D, SettingsPanel as S, _export_sfc as _, formatDateTime as a, actionHint as b, countMissing as c, existStatusMeta as e, filterTabs as f, seasonChips as s, unwrapResponse as u };
