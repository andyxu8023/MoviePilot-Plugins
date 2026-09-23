<script setup>
import { computed, onMounted, ref } from 'vue'
import SettingsPanel from './SettingsPanel.vue'
import { unwrapResponse } from '../utils.js'

const props = defineProps({
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'GetMissingEpisodes' },
})

const emit = defineEmits(['layout', 'close', 'save'])

const base = computed(() => `plugin/${props.pluginId || 'GetMissingEpisodes'}`)
const loading = ref(true)
const options = ref({ library_options: [], mediaserver_options: [], config: props.initialConfig })
const message = ref('')
const messageType = ref('success')

/** 读取媒体库与媒体服务器候选项，失败时退回宿主下发的初始配置。 */
async function loadOptions() {
  loading.value = true
  try {
    const data = unwrapResponse(await props.api.get(`${base.value}/status`)) || {}
    options.value = {
      library_options: data.library_options || [],
      mediaserver_options: data.mediaserver_options || [],
      config: data.config || props.initialConfig,
    }
  } catch (error) {
    message.value = error?.message || '获取媒体库列表失败，可直接填写名称后保存'
    messageType.value = 'warning'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  emit('layout', { maxWidth: '56rem' })
  loadOptions()
})

/** 宿主配置弹窗走标准 save 事件，由宿主写配置并弹出保存通知。 */
function submitToHost(payload) {
  emit('save', { ...(props.initialConfig || {}), ...payload })
}
</script>

<template>
  <div class="gme-config">
    <div class="d-flex align-center justify-space-between mb-2">
      <div class="text-subtitle-1">
        剧集管家设置
        <span class="text-caption gme-muted ml-2">保存后由系统写入配置并重新加载插件</span>
      </div>
      <VBtn v-if="!loading" icon="mdi-refresh" variant="text" size="small" @click="loadOptions" />
    </div>
    <VAlert v-if="message" :type="messageType" variant="tonal" density="compact" class="mb-2">
      {{ message }}
    </VAlert>
    <VProgressLinear v-if="loading" indeterminate color="primary" />
    <SettingsPanel
      v-else
      :api="api"
      :plugin-id="pluginId"
      :config="options.config"
      :library-options="options.library_options"
      :mediaserver-options="options.mediaserver_options"
      submit-mode="host"
      @submit="submitToHost"
      @message="payload => { message = payload.text; messageType = payload.color === 'error' ? 'error' : 'success' }"
      @close="$emit('close')"
    />
  </div>
</template>

<style scoped>
.gme-config {
  width: 100%;
}
.gme-muted {
  opacity: 0.7;
}
</style>
