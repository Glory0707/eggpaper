<script setup>
import { reactive, ref } from 'vue'
import { api, store, FS_SCALE, toast, checkUpdate } from '../store'
import { vDrag } from '../drag'

const emit = defineEmits(['close', 'save'])

/* store.settings 可能是 null（启动时 /api/settings 还没回来或失败），而弹窗随时会被点开：
   兜一个默认值，让弹窗永远打得开——读不到就显示成空。 */
const S = store.settings || { provider: {}, pdf2zh: {}, update: {} }

const f = reactive({
  base_url: (S.provider || {}).base_url || '',
  model: (S.provider || {}).model || '',
  // 这一栏原来没初始化：表单读的是 undefined，于是**配置里明明有 vision_model，
  // 弹窗里也永远是空的**（看着像没保存上，重填一遍也填不进去）。
  vision_model: (S.provider || {}).vision_model || '',
  api_key: (S.provider || {}).key_masked || '',
  mock: !!S.mock,
  service: (S.pdf2zh || {}).service || 'bing',
  feed: (S.update || {}).feed_url || '',
  auto_check: (S.update || {}).auto_check !== false,
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

/* 检查更新：先把更新源存下来再查（和"测试连接"一个道理，测的必须是刚填的东西） */
const checking = ref(false)
async function checkNow() {
  checking.value = true
  await api.saveSettings({ update: { feed_url: f.feed, auto_check: f.auto_check } })
  store.settings = await api.settings()
  const r = await checkUpdate(true, false)
  checking.value = false
  if (r?.has_update) { emit('close'); return }         // 有新版：把弹窗让给更新卡片
  toast(r?.ok ? `已经是最新的（${r.current}）` : '没读到更新源：' + (r?.reason || '地址为空'))
}

/* 在独立窗口打开：没有地址栏/标签页的一个窗口，任务栏里就是 eggpaper 自己。
   实现是 Edge/Chrome 的应用模式——同一个引擎，不用背 WebView 运行时。 */
async function openWindow() {
  try { const r = await api.nativeWindow(); toast('已用' + r.how + '打开独立窗口') }
  catch (e) { toast(e.message) }
}

/* 退出程序：打包版没有控制台窗口，用户需要一个"关掉它"的地方 */
async function quitApp() {
  try { await api.quit(); toast('正在退出…') } catch (e) { toast(e.message) }
}

function save() {
  Object.assign(store.viewer.layers, f.layers)
  emit('save', { provider: { base_url: f.base_url, model: f.model, api_key: f.api_key, vision_model: f.vision_model },
                 mock: f.mock, pdf2zh: { service: f.service },
                 update: { feed_url: f.feed, auto_check: f.auto_check } })
}
</script>

<template>
  <!-- 这一层**不接点击关闭**：设置里可能填了一半（base_url、key、模型号），
       点到窗外就丢掉是最气人的那种"手一滑"。出口只有两个：右上角 × 和「保存」。
       （Esc 也在 App 的全局键盘处理里专门排除了这一项。） -->
  <div class="modal-mask">
    <Transition name="pop" appear>
    <div class="modal" v-drag>
      <div class="modal-head" data-drag>
        <h3>设置</h3>
        <button class="modal-x" title="关闭" @click="emit('close')">×</button>
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
        <label class="ck" title="只管 AI 眉批；你自己钉的查译、批注不受它管，一直显示"><input type="checkbox" v-model="f.layers.marginalia" />AI 眉批</label>
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
          <option value="bing">bing（免费，不用填 key）</option>
          <option value="openai">openai（用上面填的模型与端点，按量计费）</option>
          <option value="deepseek">deepseek（用上面填的 key）</option>
          <option value="google">google（免费，国内多数网络连不通）</option>
          <option value="deepl">deepl（另需 DEEPL_AUTH_KEY 环境变量）</option>
        </select>
        </div>
      <div class="f-row">
        <label class="mono-label">更新源（静态目录地址，留空不检查）</label>
        <input type="text" v-model="f.feed" placeholder="http://192.168.1.5:8440 或 https://…/eggpaper" />
      </div>
      <div class="f-line">
        <span class="mono-label" style="margin:0">更新</span>
        <label class="ck"><input type="checkbox" v-model="f.auto_check" />打开时自动检查</label>
        <button style="margin-left:auto;padding:2px 10px;font-size:var(--fs-sm)"
                @click="checkNow" :disabled="checking">{{ checking ? '检查中…' : '立即检查更新' }}</button>
      </div>
      <div class="f-line">
        <span class="mono-label" style="margin:0">窗口</span>
        <span style="font-size:var(--fs-sm);color:var(--ink-3)">托盘图标里有「打开界面 / 检查更新 / 退出」</span>
        <button style="margin-left:auto;padding:2px 10px;font-size:var(--fs-sm)" @click="openWindow">在独立窗口打开</button>
      </div>
      <div class="f-line" v-if="store.update.packaged">
        <span class="mono-label" style="margin:0">版本</span>
        <span style="font-size:var(--fs-sm);color:var(--ink-2)">{{ store.update.current }}</span>
        <button class="danger" style="margin-left:auto;padding:2px 10px;font-size:var(--fs-sm)"
                @click="quitApp" title="关掉后台服务（打包版靠这个退出）">退出 eggpaper</button>
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
