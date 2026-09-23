<script setup>
import EpisodeManager from './EpisodeManager.vue'

defineProps({
  api: { type: Object, default: () => ({}) },
  navKey: { type: String, default: 'main' },
  pluginId: { type: String, default: 'GetMissingEpisodes' },
})

defineEmits(['action'])
</script>

<template>
  <EpisodeManager
    :api="api"
    :plugin-id="pluginId"
    :nav-key="navKey"
    @action="$emit('action')"
  />
</template>
