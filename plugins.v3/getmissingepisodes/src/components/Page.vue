<script setup>
import EpisodeManager from './EpisodeManager.vue'

defineProps({
  api: { type: Object, default: () => ({}) },
})

defineEmits(['action', 'close'])
</script>

<template>
  <EpisodeManager
    :api="api"
    plugin-id="GetMissingEpisodes"
    compact
    show-close
    @action="$emit('action')"
    @close="$emit('close')"
  />
</template>
