<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { unwrapResponse } from '../utils.js'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'GetMissingEpisodes' },
  config: { type: Object, default: () => ({}) },
  libraryOptions: { type: Array, default: () => [] },
  mediaserverOptions: { type: Array, default: () => [] },
  // host：交给宿主标准 save 流程（由宿主写配置并弹出保存通知）；api：组件自行调用插件接口保存
  submitMode: { type: String, default: 'api' },
  // 是否在底部显示关闭按钮，供宿主配置弹窗等自身不带关闭入口的容器开启
  showClose: { type: Boolean, default: false },
})

const emit = defineEmits(['saved', 'message', 'submit', 'close'])

const saving = ref(false)
const base = computed(() => `plugin/${props.pluginId}`)

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

const draft = reactive(toDraft(props.config))
watch(
  () => props.config,
  next => Object.assign(draft, toDraft(next || {})),
  { deep: true },
)

const actionItems = [
  { title: '仅检查记录', value: '仅检查记录' },
  { title: '添加到订阅', value: '添加到订阅' },
  { title: '标记为存在', value: '标记为存在' },
]

const libraryItems = computed(() =>
  (props.libraryOptions || []).map(name => ({ title: name, value: name })),
)

const mediaserverItems = computed(() =>
  (props.mediaserverOptions || []).map(name => ({ title: name, value: name })),
)

/** 提交设置并让后端回传最新总览数据。 */
async function save() {
  saving.value = true
  try {
    const payload = { ...draft }
    payload.whitelist_media_servers = toList(payload.whitelist_media_servers).join(',')
    payload.whitelist_librarys = toList(payload.whitelist_librarys)
    if (props.submitMode === 'host') {
      emit('submit', payload)
      return
    }
    const data = unwrapResponse(await props.api.post(`${base.value}/settings`, payload))
    emit('saved', data)
    emit('message', { text: '设置已保存', color: 'success' })
  } catch (error) {
    emit('message', { text: error?.message || '保存失败', color: 'error' })
  } finally {
    draft.onlyonce = false
    draft.clear = false
    saving.value = false
  }
}

defineExpose({ save, saving })
</script>

<template>
  <VCard flat class="settings-panel">
    <VCardText>
      <VRow dense>
        <VCol cols="12" md="4">
          <VSwitch v-model="draft.enabled" color="primary" inset hide-details label="启用插件" />
        </VCol>
        <VCol cols="12" md="4">
          <VSwitch
            v-model="draft.show_sidebar_nav"
            color="primary"
            inset
            label="显示侧边栏入口"
            hint="开启后在主界面左侧导航的“订阅”分组中显示剧集管家入口"
            persistent-hint
          />
        </VCol>
        <VCol cols="12" md="4">
          <VSwitch
            v-model="draft.only_aired"
            color="primary"
            inset
            label="仅订阅已开播剧集"
            hint="关闭后未开播剧集也会加入订阅"
            persistent-hint
          />
        </VCol>
        <VCol cols="12" md="4">
          <VSwitch
            v-model="draft.only_season_exist"
            color="primary"
            inset
            label="仅检查已有季缺失"
            hint="开启：只检查已存在的季是否缺集；关闭：同时检查缺失的季"
            persistent-hint
          />
        </VCol>
        <VCol cols="12" md="4">
          <VSwitch
            v-model="draft.auto_skip_finished"
            color="primary"
            inset
            label="自动跳过已完结剧集"
            hint="TMDB 状态可能被误标，出现漏检时可手动取消跳过"
            persistent-hint
          />
        </VCol>
        <VCol cols="12" md="4">
          <VSwitch
            v-model="draft.include_s00_season"
            color="primary"
            inset
            label="包含 S00 季检测"
            hint="开启后特别季（S00）也参与缺失检测"
            persistent-hint
          />
        </VCol>
        <VCol cols="12" md="4">
          <VSwitch v-model="draft.onlyonce" color="warning" inset hide-details label="立即运行一次" />
        </VCol>
        <VCol cols="12" md="4">
          <VSwitch v-model="draft.clear" color="error" inset hide-details label="清理检查记录" />
        </VCol>
        <VCol cols="12" md="4">
          <VTextField
            v-model="draft.cron"
            label="执行周期"
            placeholder="5位 cron 表达式，留空默认 0 8 * * *"
            clearable
          />
        </VCol>
        <VCol cols="12" md="4">
          <VSelect
            v-model="draft.no_exist_action"
            :items="actionItems"
            label="缺失处理方式"
            variant="outlined"
          />
        </VCol>
        <VCol cols="12" md="4">
          <VCombobox
            v-model="draft.whitelist_media_servers"
            :items="mediaserverItems.map(item => item.value)"
            label="媒体服务器名称白名单"
            placeholder="留空默认全部，多个名称用英文逗号分隔"
            chips
            multiple
            closable-chips
            :delimiters="[',']"
          />
        </VCol>
        <VCol cols="12">
          <VSelect
            v-model="draft.whitelist_librarys"
            :items="libraryItems"
            label="电视剧媒体库白名单"
            placeholder="请选择要检查的电视剧媒体库"
            multiple
            chips
            closable-chips
            variant="outlined"
          />
        </VCol>
        <VCol cols="12">
          <VTextarea
            v-model="draft.save_path_replaces"
            label="下载路径替换，一行一个"
            rows="4"
            auto-grow
            placeholder="用英文冒号分割源路径与目标路径，例如 /media/library:/downloads"
          />
        </VCol>
      </VRow>
      <VAlert type="info" variant="tonal" density="compact" class="mt-2">
        首次检测建议缺失处理方式选择“仅检查记录”，按记录手动跳过无需检测的剧集后再改为“添加到订阅”，可避免因 TMDB 信息错误产生误订阅。
      </VAlert>
    </VCardText>
    <VDivider />
    <VCardActions>
      <VSpacer />
      <VBtn v-if="showClose" color="grey-darken-1" variant="flat" @click="emit('close')">关闭</VBtn>
      <VBtn color="primary" variant="flat" :loading="saving" @click="save">保存设置</VBtn>
    </VCardActions>
  </VCard>
</template>

<style scoped>
.settings-panel :deep(.v-input__details) {
  min-height: 20px;
}
</style>
