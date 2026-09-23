import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc, S as SettingsPanel, u as unwrapResponse } from './SettingsPanel-Dq6Itw-q.js';

const {createElementVNode:_createElementVNode,createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,toDisplayString:_toDisplayString,withCtx:_withCtx,createElementBlock:_createElementBlock} = await importShared('vue');


const _hoisted_1 = { class: "gme-config" };
const _hoisted_2 = { class: "d-flex align-center justify-space-between mb-2" };

const {computed,onMounted,ref} = await importShared('vue');


const _sfc_main = {
  __name: 'Config',
  props: {
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'GetMissingEpisodes' },
},
  emits: ['layout', 'close', 'save'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;

const base = computed(() => `plugin/${props.pluginId || 'GetMissingEpisodes'}`);
const loading = ref(true);
const options = ref({ library_options: [], mediaserver_options: [], config: props.initialConfig });
const message = ref('');
const messageType = ref('success');

/** 读取媒体库与媒体服务器候选项，失败时退回宿主下发的初始配置。 */
async function loadOptions() {
  loading.value = true;
  try {
    const data = unwrapResponse(await props.api.get(`${base.value}/status`)) || {};
    options.value = {
      library_options: data.library_options || [],
      mediaserver_options: data.mediaserver_options || [],
      config: data.config || props.initialConfig,
    };
  } catch (error) {
    message.value = error?.message || '获取媒体库列表失败，可直接填写名称后保存';
    messageType.value = 'warning';
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  emit('layout', { maxWidth: '56rem' });
  loadOptions();
});

/** 宿主配置弹窗走标准 save 事件，由宿主写配置并弹出保存通知。 */
function submitToHost(payload) {
  emit('save', { ...(props.initialConfig || {}), ...payload });
}

return (_ctx, _cache) => {
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VAlert = _resolveComponent("VAlert");
  const _component_VProgressLinear = _resolveComponent("VProgressLinear");

  return (_openBlock(), _createElementBlock("div", _hoisted_1, [
    _createElementVNode("div", _hoisted_2, [
      _cache[2] || (_cache[2] = _createElementVNode("div", { class: "text-subtitle-1" }, [
        _createTextVNode(" 剧集管家设置 "),
        _createElementVNode("span", { class: "text-caption gme-muted ml-2" }, "保存后由系统写入配置并重新加载插件")
      ], -1)),
      (!loading.value)
        ? (_openBlock(), _createBlock(_component_VBtn, {
            key: 0,
            icon: "mdi-refresh",
            variant: "text",
            size: "small",
            onClick: loadOptions
          }))
        : _createCommentVNode("", true)
    ]),
    (message.value)
      ? (_openBlock(), _createBlock(_component_VAlert, {
          key: 0,
          type: messageType.value,
          variant: "tonal",
          density: "compact",
          class: "mb-2"
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(message.value), 1)
          ]),
          _: 1
        }, 8, ["type"]))
      : _createCommentVNode("", true),
    (loading.value)
      ? (_openBlock(), _createBlock(_component_VProgressLinear, {
          key: 1,
          indeterminate: "",
          color: "primary"
        }))
      : (_openBlock(), _createBlock(SettingsPanel, {
          key: 2,
          api: __props.api,
          "plugin-id": __props.pluginId,
          config: options.value.config,
          "library-options": options.value.library_options,
          "mediaserver-options": options.value.mediaserver_options,
          "submit-mode": "host",
          onSubmit: submitToHost,
          onMessage: _cache[0] || (_cache[0] = payload => { message.value = payload.text; messageType.value = payload.color === 'error' ? 'error' : 'success'; }),
          onClose: _cache[1] || (_cache[1] = $event => (_ctx.$emit('close')))
        }, null, 8, ["api", "plugin-id", "config", "library-options", "mediaserver-options"]))
  ]))
}
}

};
const Config = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-045f808c"]]);

export { Config as default };
