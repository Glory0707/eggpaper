<script setup>
import { reactive, ref } from 'vue'
import { api, store, FS_SCALE } from '../store'
import { vDrag } from '../drag'

const emit = defineEmits(['close', 'save'])

const f = reactive({
  base_url: store.settings.provider.base_url,
  model: store.settings.provider.model,
  api_key: store.settings.provider.key_masked || '',
  mock: store.settings.mock,
  service: store.settings.pdf2zh.service,
  layers: { ...store.viewer.layers },
})

/* 护眼底纹：豆沙绿 / 浅青绿 / 米黄是三个公认的经典护眼色。
   点一下立刻生效（选颜色不看效果等于没选），所以不进「保存」，直接改 store。 */
const CARES = [
  { k: 'off', zh: '纯白', bg: '#ffffff' },
  { k: 'mung', zh: '豆沙绿', bg: '#c7edcc' },
  { k: 'cyan', zh: '浅青绿', bg: '#cce8e8' },
  { k: 'sand', zh: '米黄', bg: '#f5f5dc' },
]
function pickCare(k) { store.viewer.care = k }

/* 字号：四档，乘在 <html> 的 --fs-scale 上。大小看到才知道合不合适，所以跟护眼底纹
   一样点一下立刻生效，不进「保存」。芯片里那个 A 的大小直接取真实倍率（em），
   不做"看起来差很多"的示意——图跟事实对不上就是骗人。 */
const FSS = [
  { k: 'sm', zh: '小' },
  { k: 'std', zh: '标准' },
  { k: 'lg', zh: '大' },
  { k: 'xl', zh: '特大' },
]
function pickFs(k) { store.viewer.fs = k }
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
    <div class="modal" v-drag>
      <div class="modal-head" data-drag>
        <h3>设置</h3>
        <button class="modal-x" title="关闭（Esc）" @click="emit('close')">×</button>
      </div>
      <div class="f-row">
        <label class="mono-label">LLM BASE URL</label>
        <input type="text" v-model="f.base_url" placeholder="https://api.deepseek.com/v1" />
      </div>
      <div class="f-row">
        <label class="mono-label">模型</label>
        <input type="text" v-model="f.model" placeholder="deepseek-chat / glm-4.7 / ..." />
      </div>
      <div class="f-row">
        <label class="mono-label">API KEY</label>
        <input type="text" v-model="f.api_key" placeholder="sk-…" />
      </div>
      <div class="f-row">
        <label class="mono-label">视觉模型（留空禁用）</label>
        <input type="text" v-model="f.vision_model" placeholder="glm-4.6v / gpt-4o-mini / ..." />
      </div>
      <!-- 图层 + 演示模式并到一行：原来两个复选框各占一整行，白吃版面 -->
      <div class="f-line">
        <span class="mono-label" style="margin:0">图层</span>
        <label class="ck"><input type="checkbox" v-model="f.layers.marginalia" />眉批（纸面页边）</label>
        <label class="ck" style="margin-left:14px"><input type="checkbox" v-model="f.mock" />演示模式</label>
      </div>
      <div class="f-row">
        <label class="mono-label">护眼底纹</label>
        <div class="care-row">
          <button v-for="c in CARES" :key="c.k" class="care-chip" :class="{ on: store.viewer.care === c.k }"
                  @click="pickCare(c.k)">
            <i :style="{ background: c.bg }"></i>{{ c.zh }}
          </button>
        </div>
      </div>
      <div class="f-row">
        <label class="mono-label">字号（论文正文不受影响）</label>
        <div class="care-row">
          <button v-for="s in FSS" :key="s.k" class="care-chip fs-chip" :class="{ on: store.viewer.fs === s.k }"
                  @click="pickFs(s.k)">
            <i :style="{ fontSize: FS_SCALE[s.k] + 'em' }">A</i>{{ s.zh }}
          </button>
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
