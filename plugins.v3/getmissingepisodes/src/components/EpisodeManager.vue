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
async function callAction(endpoint, item, label) {
  try {
    const response = unwrapResponse(
      await props.api.get(`${base.value}/${endpoint}?key=${encodeURIComponent(item.unique)}`),
    )
    notify(response?.message || `${label}成功`)
    await loadBoard(true)
  } catch (error) {
    notify(error?.message || `${label}失败`, 'error')
  }
}

/** 操作按钮定义，文案与配色沿用原 Vuetify JSON 页面。 */
const ACTION_DEFS = {
  add_subscribe_history: { text: '订阅缺失', color: 'primary' },
  set_all_exist_history: { text: '标记存在', color: 'success' },
  toggle_skip_history: { text: '跳过', color: 'warning' },
  delete_history: { text: '删除记录', color: 'error' },
}

/** 各检测状态下的按钮顺序，与原页面 action_names 完全一致。 */
const ACTION_ORDER = {
  存在缺失: ['delete_history', 'set_all_exist_history', 'add_subscribe_history', 'toggle_skip_history'],
  已加订阅: ['delete_history', 'set_all_exist_history', 'toggle_skip_history'],
  全部存在: ['delete_history', 'toggle_skip_history'],
  获取失败: ['delete_history', 'toggle_skip_history'],
}

/** 根据检测状态给出可用操作，与原 JSON 页面保持一致。 */
function availableActions(item) {
  const names = ACTION_ORDER[item.exist_status] || ['delete_history', 'toggle_skip_history']
  return names.map(name => {
    const definition = ACTION_DEFS[name]
    const text = name === 'toggle_skip_history'
      ? (item.skip ? '取消跳过' : '跳过')
      : definition.text
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
  const text = keyword.value.trim().toLowerCase()
  return board.items.filter(item => {
    if (!item.tags?.includes(activeFilter.value)) return false
    if (!text) return true
    return (
      String(item.title || '').toLowerCase().includes(text) ||
      String(item.path || '').toLowerCase().includes(text)
    )
  })
})

const totalPages = computed(() => Math.max(1, Math.ceil(filteredItems.value.length / pageSize.value)))

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
        <VBtn prepend-icon="mdi-magnify-plus-outline" variant="tonal" color="primary" :loading="checking" @click="runCheck">
          立即检查
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
        v-model="keyword"
        prepend-inner-icon="mdi-magnify"
        placeholder="搜索剧名或路径"
        clearable
        density="comfortable"
        hide-details
        class="gme-toolbar__search"
        @update:model-value="page = 1"
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
        <VCard variant="tonal" class="gme-card h-100">
          <div class="gme-card__body">
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
              </div>
              <div class="text-caption gme-muted">剧集状态：{{ raw.status_cn || '未知' }}</div>
              <div class="text-caption gme-muted">开播年份：{{ raw.year || '未知' }} · 评分 {{ raw.vote_average ?? '-' }}</div>
              <div class="text-caption gme-muted">最后播出：{{ raw.last_air_date || '未知' }}</div>
              <div class="text-caption gme-muted">检查时间：{{ raw.last_check || '暂无' }}</div>
              <div v-if="decorate(raw).chips.length" class="gme-card__seasons">
                <VChip
                  v-for="chip in decorate(raw).chips"
                  :key="chip.season"
                  size="x-small"
                  variant="outlined"
                  :color="chip.missing.length ? 'error' : 'warning'"
                  :title="chip.text"
                >
                  {{ chip.season }}
                  <template v-if="chip.missing.length">
                    E{{ chip.missing.slice(0, 6).join(',E') }}<span v-if="chip.missing.length > 6">…</span>
                  </template>
                  <template v-else>缺整季</template>
                </VChip>
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
              @click="callAction(action.key, raw, action.text)"
            >
              {{ action.text }}
            </VBtn>
          </VBtnToggle>
        </VCard>
      </VCol>
    </VRow>

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
}
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
