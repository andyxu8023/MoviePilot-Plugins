import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { E as EpisodeManager } from './EpisodeManager-Cxtpagiz.js';

const {openBlock:_openBlock,createBlock:_createBlock} = await importShared('vue');


const _sfc_main = {
  __name: 'AppPage',
  props: {
  api: { type: Object, default: () => ({}) },
  navKey: { type: String, default: 'main' },
  pluginId: { type: String, default: 'GetMissingEpisodes' },
},
  emits: ['action'],
  setup(__props) {





return (_ctx, _cache) => {
  return (_openBlock(), _createBlock(EpisodeManager, {
    api: __props.api,
    "plugin-id": __props.pluginId,
    "nav-key": __props.navKey,
    onAction: _cache[0] || (_cache[0] = $event => (_ctx.$emit('action')))
  }, null, 8, ["api", "plugin-id", "nav-key"]))
}
}

};

export { _sfc_main as default };
