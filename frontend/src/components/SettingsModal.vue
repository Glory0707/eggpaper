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
  emit('save', { provider: { base_url: f.base_url, model: f.model, api_key: f.api_key }, mock: f.mock, pdf2zh: { service: f.service } })
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <h3>设置 · 只存本机</h3>
      <div class="f-row">
        <label class="mono-label">LLM BASE URL（任意 OpenAI 兼容端点）</label>
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
      <label class="mock-row">
        <input type="checkbox" v-model="f.mock" />
        演示模式（不调用 API，用假数据走通界面）
      </label>
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
  </div>
</template>
