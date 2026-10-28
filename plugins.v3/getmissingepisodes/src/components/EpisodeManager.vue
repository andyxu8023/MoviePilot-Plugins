<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import SettingsPanel from './SettingsPanel.vue'
import {
  DEFAULT_POSTER,
  actionHint,
  countMissing,
  existStatusMeta,
  filterTabs,
  formatDateTime,
  seasonChips,
  unwrapResponse,
} from '../utils.js'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'GetMissingEpisodes' },
  navKey: { type: String, default: 'main' },
  initialConfig: { type: Object, default: () => ({}) },
  initialTab: { type: String, default: 'board' },
  compact: { type: Boolean, default: false },
  showClose: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'action'])

const base = computed(() => `plugin/${props.pluginId}`)
const loading = ref(false)
const checking = ref(false)
const settingsOpen = ref(props.initialTab === 'config')
const activeFilter = ref('all')
const keyword = ref('')
const page = ref(1)
const pollTimer = ref(null)

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
})

const snackbar = reactive({ show: false, text: '', color: 'success' })

/** 提示条统一入口。 */
function notify(text, color = 'success') {
  snackbar.text = text
  snackbar.color = color
  snackbar.show = true
}

/** 拉取总览数据，silent 用于轮询时不闪加载态。 */
async function loadBoard(silent = false) {
  if (!silent) loading.value = true
  try {
    const data = unwrapResponse(await props.api.get(`${base.value}/status`)) || {}
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
    })
    if (board.running) startPolling()
    else stopPolling()
  } catch (error) {
    if (!silent) notify(error?.message || '加载剧集管家数据失败', 'error')
  } finally {
    loading.value = false
  }
}

/** 检测进行中时每 6 秒刷新一次状态。 */
function startPolling() {
  if (pollTimer.value) return
  pollTimer.value = setInterval(() => loadBoard(true), 6000)
}

function stopPolling() {
  if (pollTimer.value) {
    clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

/** 触发一次立即检测。 */
async function runCheck() {
  checking.value = true
  try {
    unwrapResponse(await props.api.post(`${base.value}/run`, {}))
    notify('已触发一次检测，稍后自动刷新结果')
    startPolling()
    setTimeout(() => loadBoard(true), 3000)
  } catch (error) {
    notify(error?.message || '触发检测失败', 'error')
  } finally {
    checking.value = false
  }
}

/** 调用单条记录的操作接口。 */
async function callAction(endpoint, item, label, query = '') {
  const url = `${base.value}/${endpoint}?key=${encodeURIComponent(item.unique)}${query}`
  try {
    const envelope = await props.api.get(url)
    unwrapResponse(envelope)
    notify(envelope?.message || `${label}成功`)
    if (endpoint === 'set_all_exist_history') delete seasonSelection[item.unique]
    await loadBoard(true)
  } catch (error) {
    notify(error?.message || `${label}失败`, 'error')
  }
}

/** 每张卡片点选为「标记存在」的缺失季，键为记录 unique，值为季号数组。 */
const seasonSelection = reactive({})

/** 只有带「标记存在」按钮的状态才允许点选缺失季。 */
function canSelectSeasons(item) {
  return ['存在缺失', '已加订阅'].includes(item.exist_status)
}

/** 当前卡片已选中的季号数组。 */
function selectedSeasons(unique) {
  return seasonSelection[unique] || []
}

/** 点一下缺失季标签即切换选中态，作为「标记存在」的作用范围。 */
function toggleSeasonChip(item, chip) {
  if (!canSelectSeasons(item)) return
  const list = seasonSelection[item.unique] || []
  const index = list.indexOf(chip.num)
  if (index >= 0) {
    list.splice(index, 1)
  } else {
    list.push(chip.num)
  }
  seasonSelection[item.unique] = list
}

/** 选中态的视觉反馈统一走下面三个函数，模板与构建产物保持一致。 */
function chipChecked(item, chip) {
  return canSelectSeasons(item) && selectedSeasons(item.unique).includes(chip.num)
}
function chipVariant(item, chip) {
  return chipChecked(item, chip) ? 'flat' : 'outlined'
}
function chipColor(item, chip) {
  if (chipChecked(item, chip)) return 'success'
  return chip.missing.length ? 'error' : 'warning'
}
function chipTitle(item, chip) {
  if (!canSelectSeasons(item)) return chip.text
  return chipChecked(item, chip) ? `${chip.text}（已选，点击取消）` : `${chip.text}（点击标记为已存在）`
}

/** 卡片按钮点击入口：标记存在必须先点选缺失季，避免整条一键全部标记。 */
function onActionClick(name, item, label) {
  if (name === 'set_all_exist_history') {
    const selected = selectedSeasons(item.unique)
    if (!selected.length) {
      notify('请先点选要标记为已存在的缺失季', 'warning')
      return
    }
    return callAction(name, item, label, `&seasons=${selected.join(',')}`)
  }
  return callAction(name, item, label)
}

/** 缺失处理方式三态，取值与后端 NoExistAction 逐字一致。 */
const ACTION_CYCLE = ['仅检查记录', '添加到订阅', '标记为存在']

/** 下拉选项：空串表示该剧恢复跟随全局配置。 */
const ACTION_OPTIONS = [
  { title: '跟随全局', value: '' },
  ...ACTION_CYCLE.map(value => ({ title: value, value })),
]

/** 卡片按钮位窄，统一用两字短标签；菜单里仍是完整名称。 */
function actionShort(value) {
  const map = { 仅检查记录: '记录', 添加到订阅: '订阅', 标记为存在: '存在' }
  return map[value] || '全局'
}

/** 当前生效的全局缺失处理方式。 */
const currentAction = computed(() => board.config?.no_exist_action || ACTION_CYCLE[0])

/** 顶栏下拉直接切换全局缺失处理方式，选择即保存。 */
async function setGlobalAction(value) {
  if (!value || value === currentAction.value) return
  try {
    unwrapResponse(await props.api.post(`${base.value}/settings`, { no_exist_action: value }))
    notify(`缺失处理方式已设为「${value}」`)
    await loadBoard(true)
  } catch (error) {
    notify(error?.message || '设置缺失处理方式失败', 'error')
  }
}

/** 卡片下拉为单部剧指定缺失处理方式，空值表示跟随全局。 */
function setRecordAction(item, value) {
  if (value === (item.action_override || '')) return
  return callAction('set_action_history', item, '缺失处理', `&action=${encodeURIComponent(value ?? '')}`)
}

/** 操作按钮定义，文案与配色沿用原 Vuetify JSON 页面。 */
const ACTION_DEFS = {
  add_subscribe_history: { text: '订阅缺失', color: 'primary' },
  set_all_exist_history: { text: '标记存在', color: 'success' },
  toggle_skip_history: { text: '跳过', color: 'warning' },
  toggle_follow_history: { text: '继续追新', color: 'info' },
  delete_history: { text: '删除记录', color: 'error' },
}

/** 各检测状态下的按钮顺序，与原页面 action_names 完全一致。 */
const ACTION_ORDER = {
  存在缺失: ['delete_history', 'set_all_exist_history', 'add_subscribe_history', 'toggle_skip_history'],
  已加订阅: ['delete_history', 'set_all_exist_history', 'toggle_skip_history'],
  全部存在: ['delete_history', 'toggle_skip_history'],
  获取失败: ['delete_history', 'toggle_skip_history'],
  已删除: ['delete_history', 'toggle_follow_history'],
}

/** 根据检测状态给出可用操作，与原 JSON 页面保持一致。 */
function availableActions(item) {
  const names = ACTION_ORDER[item.exist_status] || ['delete_history', 'toggle_skip_history']
  return names.map(name => {
    const definition = ACTION_DEFS[name]
    let text = definition.text
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

const pageSize = computed(() => (props.compact ? 12 : 24))

/** 统计卡片数量。 */
function statValue(key) {
  return Number(board.statistics?.[key] || 0)
}

/** 按过滤视图与关键字筛选记录。 */
const filteredItems = computed(() => {
  const text = String(keyword.value ?? '').trim().toLowerCase()
  return board.items.filter(item => {
    if (!item.tags?.includes(activeFilter.value)) return false
    if (!text) return true
    return (
      String(item.title || '').toLowerCase().includes(text) ||
      String(item.path || '').toLowerCase().includes(text)
    )
  })
})

/** 归一化搜索关键字，Vuetify clearable 清空时事件值为 null，需回落为空串。 */
function onKeywordInput(value) {
  keyword.value = typeof value === 'string' ? value : ''
  page.value = 1
}

const totalPages = computed(() => Math.max(1, Math.ceil(filteredItems.value.length / pageSize.value)))

/** 底部翻页后回到卡片列表顶部，否则会停在新一页的下方看不到卡片。
 * 用瞬时滚动而非 smooth：平滑动画会被随后的列表重渲染打断，实测出现过分页后仍停在旧滚动位置。 */
function scrollToGridTop() {
  document.querySelector('.gme-root')?.scrollIntoView({ block: 'start' })
}

/** 底部页码条的切换入口：改页码并滚回列表顶部。 */
function changePage(value) {
  page.value = value
  scrollToGridTop()
}

const pagedItems = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredItems.value.slice(start, start + pageSize.value)
})

const currentFilterTitle = computed(
  () => filterTabs.find(tab => tab.key === activeFilter.value)?.title || '全部',
)

/** 切换过滤视图。 */
function selectFilter(key) {
  activeFilter.value = key
  page.value = 1
}

/** 生成卡片展示所需的派生信息。 */
function decorate(item) {
  const chips = seasonChips(item.seasons)
  // chip.season 只是展示标签（S01 这种），点选需要原始季号
  chips.forEach((chip, index) => {
    chip.num = Number((item.seasons || [])[index]?.season ?? 0)
  })
  const missing = countMissing(item.seasons)
  const meta = existStatusMeta(item.exist_status)
  let statusText = item.exist_status || '未知状态'
  if (item.exist_status === '存在缺失' && missing.seasonsCount) {
    statusText = `缺失${missing.seasonsCount}季, ${missing.episodesCount}集`
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
 * 卡片信息区背景：逐层照抄本实例订阅卡片「有背景图」时的图层，
 * 取自 /public/assets/style-*.css 的 data-v-e9d64cc5 作用域：
 *   window-base  background-color:#1d2026（--subscribe-card-tone 从未内联赋值，只有 fallback）
 *   backdrop     VImg 原图，position="center top"，左缘 inset-inline:18% 起 + mask 羽化
 *   shade        linear-gradient(90deg,#1d2026 十档阶梯 → transparent 72%)
 *   scrim        radial-gradient(70px 56px at 100% 0%,rgba(0,0,0,.5)) + 底部黑色渐变
 * 关键：背景图本身完全不褪色，所以画面保持原图通透度。
 * 不要用 .subscribe-card-background（linear-gradient(180deg,#1f293778,#1f2937)），
 * 那条属于「没有背景图」的占位分支，蒙在有图的卡上会让整卡发灰。
 * 宿主的 mask 羽化无法用在 background 上（mask-image 会连海报与文字一起裁掉），
 * 左侧过渡由 shade 阶梯承担。只铺信息区，不铺到底部按键条。
 */
const CARD_SHADE = '29, 32, 38'
const CARD_SHADE_LADDER = [
  [0, 1], [8, 0.97], [16, 0.9], [24, 0.78], [32, 0.62],
  [40, 0.45], [48, 0.28], [56, 0.14], [64, 0.05],
]

function cardBackground(item) {
  const url = String(item.backdrop || '')
  if (!/^https?:\/\//.test(url) || /["')\n\r]/.test(url)) return {}
  const shade = CARD_SHADE_LADDER
    .map(([stop, alpha]) => `rgba(${CARD_SHADE}, ${alpha}) ${stop}%`)
    .join(', ')
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
  }
}

onMounted(() => {
  loadBoard()
})

onUnmounted(() => stopPolling())
</script>

<template>
  <div class="gme-root" :class="{ 'gme-root--compact': compact }">
    <div class="gme-header">
      <div class="gme-header__title">
        <VIcon icon="mdi-television-classic" size="28" class="mr-2" color="primary" />
        <div>
          <div class="text-h6 font-weight-medium">剧集管家</div>
          <div class="text-caption gme-muted">
            <span v-if="!board.enabled">插件未启用，仅展示历史记录</span>
            <span v-else-if="board.running">正在检测媒体库缺失剧集…</span>
            <span v-else>上次检查：{{ formatDateTime(board.last_check) }} · 周期 {{ board.cron || '默认 0 8 * * *' }}</span>
          </div>
        </div>
      </div>
      <div class="gme-header__actions">
        <VBtn prepend-icon="mdi-refresh" variant="text" :loading="loading" @click="loadBoard()">
          刷新
        </VBtn>
        <VBtn prepend-icon="mdi-magnify-plus-outline" variant="text" :loading="checking" @click="runCheck">
          立即检查
        </VBtn>
        <VBtn prepend-icon="mdi-tune-variant" variant="text" append-icon="mdi-menu-down">
          全局缺失处理：{{ currentAction }}
          <VMenu activator="parent">
            <VList density="compact">
              <VListItem
                v-for="option in ACTION_CYCLE"
                :key="option"
                :prepend-icon="option === currentAction ? 'mdi-check' : undefined"
                :title="option"
                @click="setGlobalAction(option)"
              />
            </VList>
          </VMenu>
        </VBtn>
        <VBtn
          prepend-icon="mdi-book-open-outline"
          variant="text"
          href="https://github.com/andyxu8023/MoviePilot-Plugins/blob/main/plugins.v3/getmissingepisodes/README.md"
          target="_blank"
          rel="noopener noreferrer"
        >
          使用说明
        </VBtn>
        <VBtn prepend-icon="mdi-cog-outline" variant="text" @click="settingsOpen = true">设置</VBtn>
        <VBtn v-if="showClose" icon="mdi-close" variant="text" aria-label="关闭" @click="emit('close')" />
      </div>
    </div>

    <VRow dense class="gme-stats">
      <VCol
        v-for="tab in filterTabs"
        :key="tab.key"
        cols="6"
        sm="4"
        md="3"
        lg="2"
        xl="1"
      >
        <VCard
          variant="tonal"
          :color="activeFilter === tab.key ? tab.color : undefined"
          :class="['gme-stat', { 'gme-stat--active': activeFilter === tab.key }]"
          @click="selectFilter(tab.key)"
        >
          <VCardText class="d-flex align-center pa-3">
            <VIcon :icon="tab.icon" :color="activeFilter === tab.key ? undefined : tab.color" class="mr-2" />
            <div>
              <div class="text-caption">{{ tab.title }}</div>
              <div class="text-h6">{{ statValue(tab.key) }} 部</div>
            </div>
          </VCardText>
        </VCard>
      </VCol>
    </VRow>

    <div class="gme-toolbar">
      <VTextField
        :model-value="keyword"
        prepend-inner-icon="mdi-magnify"
        placeholder="搜索剧名或路径"
        clearable
        density="comfortable"
        hide-details
        class="gme-toolbar__search"
        @update:model-value="onKeywordInput"
      />
      <div class="text-body-2 gme-muted">
        {{ currentFilterTitle }} · 共 {{ filteredItems.length }} 部
      </div>
      <VSpacer />
      <VPagination
        v-if="totalPages > 1"
        v-model="page"
        :length="totalPages"
        :total-visible="compact ? 5 : 7"
        density="comfortable"
      />
    </div>

    <VProgressLinear v-if="loading" indeterminate color="primary" class="mb-2" />

    <div v-if="!loading && !pagedItems.length" class="gme-empty">
      <VIcon icon="mdi-inbox-outline" size="42" class="mb-2 gme-muted" />
      <div class="text-body-1 gme-muted">当前视图没有记录，可先点击“立即检查”生成检测结果</div>
    </div>

    <VRow dense>
      <VCol v-for="raw in pagedItems" :key="raw.unique" cols="12" sm="6" md="4" lg="3" xl="3">
        <VCard variant="tonal" class="gme-card h-100 d-flex flex-column">
          <div class="gme-card__body" :style="cardBackground(raw)">
            <VImg
              :src="decorate(raw).poster"
              cover
              height="200"
              width="134"
              class="gme-card__poster"
            >
              <template #placeholder>
                <div class="gme-card__poster-fallback"><VIcon icon="mdi-image-off-outline" /></div>
              </template>
            </VImg>
            <div class="gme-card__info">
              <a :href="decorate(raw).href" target="_blank" class="gme-card__title text-subtitle-1">
                {{ raw.title || '未知' }}
              </a>
              <div class="d-flex align-center mb-1">
                <VChip
                  size="x-small"
                  variant="flat"
                  :color="decorate(raw).meta.color"
                  :prepend-icon="decorate(raw).meta.icon"
                  class="mr-1"
                >
                  {{ decorate(raw).statusText }}
                </VChip>
                <VChip v-if="raw.skip" size="x-small" variant="outlined" color="warning">已跳过</VChip>
                <VChip
                  v-if="raw.follow"
                  size="x-small"
                  variant="flat"
                  color="info"
                  prepend-icon="mdi-bell-plus-outline"
                >追新中</VChip>
              </div>
              <div class="text-caption gme-muted">剧集状态：{{ raw.status_cn || '未知' }}</div>
              <div class="text-caption gme-muted">开播年份：{{ raw.year || '未知' }} · 评分 {{ raw.vote_average ?? '-' }}</div>
              <div class="text-caption gme-muted">最后播出：{{ raw.last_air_date || '未知' }}</div>
              <div class="text-caption gme-muted">检查时间：{{ raw.last_check || '暂无' }}</div>
              <template v-if="raw.deleted">
                <div class="text-caption gme-muted">删除时间：{{ formatDateTime(raw.deleted_at) }}</div>
                <div v-if="raw.existing_label" class="text-caption gme-muted">
                  删除前存在：{{ raw.existing_label }}
                </div>
                <div v-if="raw.follow_note" class="text-caption gme-muted">
                  追新结果：{{ raw.follow_note }}
                </div>
                <div v-if="raw.follow" class="text-caption gme-muted">
                  缺失季将整季重订（含已删除的集），新季自动补订
                </div>
              </template>
              <div v-if="decorate(raw).chips.length" class="gme-card__seasons">
                <VChip
                  v-for="chip in decorate(raw).chips"
                  :key="chip.season"
                  size="x-small"
                  :variant="chipVariant(raw, chip)"
                  :color="chipColor(raw, chip)"
                  :prepend-icon="chipChecked(raw, chip) ? 'mdi-check' : undefined"
                  :title="chipTitle(raw, chip)"
                  :style="canSelectSeasons(raw) ? 'cursor: pointer; user-select: none;' : ''"
                  @click="toggleSeasonChip(raw, chip)"
                >
                  {{ chip.season }}
                  <template v-if="chip.missing.length">
                    E{{ chip.missing.slice(0, 6).join(',E') }}<span v-if="chip.missing.length > 6">…</span>
                  </template>
                  <template v-else>缺整季</template>
                </VChip>
                <div v-if="canSelectSeasons(raw)" class="text-caption gme-muted" style="width: 100%">
                  点选缺失季后再「标记存在」，未选中的季继续按缺失补全
                </div>
              </div>
              <div v-if="raw.ignored_seasons?.length" class="text-caption gme-muted">
                已忽略季：{{ raw.ignored_seasons.join('、') }}
              </div>
            </div>
          </div>
          <VBtnToggle
            variant="tonal"
            rounded="0"
            class="gme-card__actions d-flex mt-auto"
            style="width: 100%; display: flex;"
          >
            <VBtn
              v-for="action in availableActions(raw)"
              :key="action.key"
              :class="`text-${action.color}`"
              variant="tonal"
              style="height: 100%; width: 100%; flex: 1;"
              @click="onActionClick(action.key, raw, action.text)"
            >
              {{ action.text }}
            </VBtn>
          </VBtnToggle>
          <VBtn
            variant="tonal"
            prepend-icon="mdi-tune-variant"
            append-icon="mdi-menu-down"
            title="缺失处理方式"
            class="gme-card__action-extra text-none text-primary"
            style="width: 100%; flex: none; border-radius: 0;"
          >
            缺失处理：{{ actionShort(raw.action_override) }}
            <VMenu activator="parent">
              <VList density="compact">
                <VListItem
                  v-for="option in ACTION_OPTIONS"
                  :key="option.value"
                  :prepend-icon="(option.value || '') === (raw.action_override || '') ? 'mdi-check' : undefined"
                  :title="option.title"
                  @click="setRecordAction(raw, option.value)"
                />
              </VList>
            </VMenu>
          </VBtn>
        </VCard>
      </VCol>
    </VRow>

    <div v-if="totalPages > 1" class="gme-toolbar">
      <div class="text-body-2 gme-muted">
        共 {{ filteredItems.length }} 部 · 第 {{ page }}/{{ totalPages }} 页
      </div>
      <VSpacer />
      <VPagination
        :model-value="page"
        :length="totalPages"
        :total-visible="compact ? 5 : 7"
        density="comfortable"
        @update:model-value="changePage"
      />
    </div>

    <div class="text-caption gme-muted mt-2">
      缺失处理方式：{{ actionHint(board.config.no_exist_action) }}
      <span v-if="board.config.whitelist_librarys?.length">
        · 媒体库白名单 {{ board.config.whitelist_librarys.length }} 个
      </span>
    </div>

    <VDialog v-model="settingsOpen" max-width="56rem" scrollable>
      <VCard title="剧集管家设置">
        <template #append>
          <VBtn icon="mdi-close" variant="text" @click="settingsOpen = false" />
        </template>
        <VDivider />
        <VCardText>
          <SettingsPanel
            :api="api"
            :plugin-id="pluginId"
            :config="board.config"
            :library-options="board.library_options"
            :mediaserver-options="board.mediaserver_options"
            @saved="payload => Object.assign(board, payload || {})"
            @message="message => notify(message.text, message.color)"
          />
        </VCardText>
      </VCard>
    </VDialog>

    <VSnackbar v-model="snackbar.show" :color="snackbar.color" timeout="3000" location="bottom">
      {{ snackbar.text }}
    </VSnackbar>
  </div>
</template>

<style scoped>
.gme-root {
  width: 100%;
}
.gme-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.gme-header__title {
  display: flex;
  align-items: center;
}
.gme-header__actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
.gme-muted {
  opacity: 0.7;
}
.gme-stats {
  margin-bottom: 8px;
}
.gme-stat {
  cursor: pointer;
  transition: transform 0.12s ease, box-shadow 0.12s ease;
}
.gme-stat:hover {
  transform: translateY(-1px);
}
.gme-stat--active {
  box-shadow: 0 0 0 2px rgba(var(--v-theme-primary), 0.45);
}
.gme-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.gme-toolbar__search {
  max-width: 320px;
}
.gme-empty {
  text-align: center;
  padding: 40px 12px;
}
.gme-card__body {
  display: flex;
  gap: 10px;
  padding: 10px;
  flex: 1 1 auto;
  /* 背景图上统一用白字，与订阅卡片一致 */
  color: #fff;
}
.gme-card__body .gme-muted {
  opacity: 1;
  color: #fff;
}
/* 剧集卡片固定为普通不透明卡面，不跟随系统玻璃效果。
   宿主玻璃规则带 !important 且形如 html[data-theme=glass] .v-dialog>.v-overlay__content>:where(.v-card)，
   :where() 不计特异性，故这里用 html body 前缀压过它。 */
html body .gme-root .gme-card.v-card {
  background-color: rgb(var(--v-theme-surface)) !important;
  background-image: none !important;
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
}
/* 卡内按钮与描边徽章：底色与卡面同族（仍不透明、不透背景图）。
   原先强制 #1f2937 与卡面不同色，再叠 tonal 的 currentColor 12% 色罩，
   红/橙字色罩压在深蓝灰底上就是那种浑浊的脏灰紫。 */
html body .gme-root .gme-card .v-btn,
html body .gme-root .gme-card .v-chip--variant-outlined {
  background-color: rgb(var(--v-theme-surface)) !important;
}
/* 关掉 tonal 色罩：.v-btn__underlay 是 VBtn 内部节点、不带本组件 scope 属性，
   必须走 :deep()；悬停与按下反馈由 .v-btn__overlay 承担，不受影响。 */
html body .gme-root .gme-card :deep(.v-btn__underlay) {
  opacity: 0 !important;
}
/* 按钮条与卡面同色，靠 1px 发丝线分隔，避免整条糊成一片 */
html body .gme-root .gme-card .gme-card__actions,
html body .gme-root .gme-card .gme-card__action-extra {
  border-top: 1px solid rgba(255, 255, 255, 0.1);
}
html body .gme-root .gme-card .gme-card__actions .v-btn + .v-btn {
  border-inline-start: 1px solid rgba(255, 255, 255, 0.1);
}
/* 玻璃主题会给 chip / btn 叠 backdrop-filter 与 --glass-*-sheen 高光，
   实色徽章也会因此发灰透亮；卡片内一律撤掉，保留 Vuetify 自身实色底色。 */
html body .gme-root .gme-card .v-chip,
html body .gme-root .gme-card .v-btn {
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  background-image: none !important;
}
/* 按键底色淡染：在不透明卡面底色上叠一层自身语义色渐变（15%），
   按钮本身仍是不透明实底，不会回到「透出背景图」的雾蒙感。
   不用 color-mix：实测在该链路里会算成同一中性值。
   特异性需高于上面那条 background-image:none !important，故带上 .gme-card__actions。 */
html body .gme-root .gme-card .gme-card__actions .v-btn.text-error {
  background-image: linear-gradient(rgba(var(--v-theme-error), .15), rgba(var(--v-theme-error), .15)) !important;
}
html body .gme-root .gme-card .gme-card__actions .v-btn.text-success {
  background-image: linear-gradient(rgba(var(--v-theme-success), .15), rgba(var(--v-theme-success), .15)) !important;
}
html body .gme-root .gme-card .gme-card__actions .v-btn.text-primary {
  background-image: linear-gradient(rgba(var(--v-theme-primary), .15), rgba(var(--v-theme-primary), .15)) !important;
}
html body .gme-root .gme-card .gme-card__actions .v-btn.text-warning {
  background-image: linear-gradient(rgba(var(--v-theme-warning), .15), rgba(var(--v-theme-warning), .15)) !important;
}
html body .gme-root .gme-card .gme-card__actions .v-btn.text-info {
  background-image: linear-gradient(rgba(var(--v-theme-info), .15), rgba(var(--v-theme-info), .15)) !important;
}
html body .gme-root .gme-card .gme-card__action-extra.text-primary {
  background-image: linear-gradient(rgba(var(--v-theme-primary), .1), rgba(var(--v-theme-primary), .1)) !important;
}
/* 玻璃主题会把 Vuetify flat chip 的 bg-<色> 类压成 34% 透明底
   （html[data-theme=glass] .v-chip.bg-success{background-color:rgba(...,34%)!important}），
   这里按颜色逐个还原成实色，特异性高于宿主规则。 */
html body .gme-root .gme-card .v-chip.bg-success { background-color: rgb(var(--v-theme-success)) !important; }
html body .gme-root .gme-card .v-chip.bg-info { background-color: rgb(var(--v-theme-info)) !important; }
html body .gme-root .gme-card .v-chip.bg-warning { background-color: rgb(var(--v-theme-warning)) !important; }
html body .gme-root .gme-card .v-chip.bg-error { background-color: rgb(var(--v-theme-error)) !important; }
html body .gme-root .gme-card .v-chip.bg-secondary { background-color: rgb(var(--v-theme-secondary)) !important; }
html body .gme-root .gme-card .v-chip.bg-primary { background-color: rgb(var(--v-theme-primary)) !important; }
.gme-card__poster {
  border-radius: 6px;
  flex: 0 0 auto;
}
.gme-card__poster-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  opacity: 0.4;
}
.gme-card__info {
  min-width: 0;
  flex: 1 1 auto;
}
.gme-card__title {
  display: block;
  text-decoration: none;
  color: inherit;
  word-break: break-word;
  line-height: 1.25;
  margin-bottom: 6px;
}
.gme-card__title:hover {
  color: rgb(var(--v-theme-primary));
}
.gme-card__seasons {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
.gme-card__actions {
  flex-wrap: nowrap;
}
</style>
