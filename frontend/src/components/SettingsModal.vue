<script setup>
import { reactive, ref } from 'vue'
import { api, store } from '../store'

const emit = defineEmits(['close', 'save'])

const f = reactive({
  base_url: store.settings.provider.base_url,
  model: store.settings.provider.model,
  api_key: store.settings.provider.key_masked || '',
  mock: store.settings.mock,
  service: store.settings.pdf2zh.service,
  layers: { ...store.viewer.layers },
})
const testing = ref(false)
const reply = ref('')

async function test() {
  testing.value = true
  reply.value = ''
  // 先保存再测，保证测的是刚填的配置
  await api.saveSettings({ provider: { base_url: f.base_url, model: f.model, api_key: f.api_key }, mock: f.mock })
  store.settings = await api.settings()
  const r = await api.testSettings()
  reply.value = (r.ok ? '✓ ' : '✗ ') + r.reply
  testing.value = false
}

function save() {
  Object.assign(store.viewer.layers, f.layers)
  emit('save', { provider: { base_url: f.base_url, model: f.model, api_key: f.api_key, vision_model: f.vision_model }, mock: f.mock, pdf2zh: { service: f.service } })
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <Transition name="pop" appear>
    <div class="modal">
      <h3>设置 · 只存本机</h3>
      <div class="f-row">
        <label class="mono-label">LLM BASE URL</label>
        <input type="text" v-model="f.base_url" placeholder="https://api.deepseek.com/v1" />
      </div>
      <div class="f-row">
        <label class="mono-label">模型</label>
        <input type="text" v-model="f.model" placeholder="deepseek-chat / glm-4.7 / ..." />
      </div>
      <div class="f-row">
        <label class="mono-label">API KEY（写入本地 config.yaml，不入库不外传）</label>
        <input type="text" v-model="f.api_key" placeholder="sk-…" />
      </div>
      <div class="f-row">
        <label class="mono-label">视觉模型（留空禁用）</label>
        <input type="text" v-model="f.vision_model" placeholder="glm-4.6v / gpt-4o-mini / ..." />
      </div>
      <label class="mock-row">
        <input type="checkbox" v-model="f.mock" />
        演示模式（不调用 API）
      </label>
      <div class="f-row">
        <label class="mono-label">图层</label>
        <div class="mock-row" style="margin:0">
          <input type="checkbox" id="ly-skel" v-model="f.layers.skeleton" /><label for="ly-skel" style="margin:0">骨架标签</label>
          <input type="checkbox" id="ly-mg" v-model="f.layers.marginalia" /><label for="ly-mg" style="margin:0">眉批</label>
        </div>
      </div>
      <div class="f-row">
        <label class="mono-label">整本翻译服务（pdf2zh）</label>
        <select v-model="f.service">
          <option value="google">google</option>
          <option value="bing">bing</option>
          <option value="deepl">deepl</option>
          <option value="openai">openai（OpenAI 兼容）</option>
        </select>
      </div>
      <div class="f-actions">
        <span class="test-reply">{{ reply }}</span>
        <button @click="test" :disabled="testing">{{ testing ? '测试中…' : '测试连接' }}</button>
        <button class="primary" @click="save">保存</button>
      </div>
    </div>
    </Transition>
  </div>
</template>
