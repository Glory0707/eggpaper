<script setup>
import { onMounted, reactive, ref } from 'vue'
import { api, store, FS_SCALE, toast, checkUpdate, lsGet, lsSet } from '../store'
import { engInst, startEngineInstall, onEngineReady, watchEngine } from '../engine'
import { t, ui, setLang, setDark, isEn } from '../i18n'
import { confirmBox } from '../dialog'
import { vDrag } from '../drag'
import { modalFocus } from '../modalFocus'

const emit = defineEmits(['close', 'save', 'quit'])
const maskEl = ref(null)
modalFocus(maskEl)

/* store.settings 可能是 null（启动时 /api/settings 还没回来或失败），而弹窗随时会被点开：
   兜一个默认值，让弹窗永远打得开——读不到就显示成空。 */
const S = store.settings || { provider: {}, pdf2zh: {}, update: {} }

const f = reactive({
  base_url: (S.provider || {}).base_url || '',
  model: (S.provider || {}).model || '',
  api_key: (S.provider || {}).key_masked || '',
  vision: !!(S.provider || {}).vision_model,   // 勾上 = 就用上面这个模型做视觉问答
  mock: !!S.mock,
  service: (S.pdf2zh || {}).service || 'bing',
  engine_path: (S.pdf2zh || {}).path || '',
  deepl_key: (S.pdf2zh || {}).deepl_key || '',
  auto_check: (S.update || {}).auto_check !== false,
  shot_save: S.shot_save !== false,
  layers: { ...store.viewer.layers },
  data_dir: (S.data_dir || '').replace(/\$/, ''),
})
const savingData = ref(false)
const picking = ref(false)
/* 迁移 = 弹 Windows 自带目录选择框，选中即迁——不填路径、不加浏览按钮。 */
async function moveData() {
  picking.value = true
  let target = ''
  try {
    const r = await api.dataPick()
    target = (r.path || '').trim()
  } catch { picking.value = false; return }
  picking.value = false
  if (!target) return                            // 用户取消了选择框
  if (target.replace(/[\\/]+$/, '') === f.data_dir.replace(/[\\/]+$/, '')) return
  const yes = await confirmBox({
    title: t('迁移数据目录'), ok: t('迁移'), danger: false,
    body: t('把文库、批注、配置整体迁到：{p}。重启后生效。', { p: target }),
  })
  if (!yes) return
  savingData.value = true
  try {
    await api.setDataLocation(target)
    toast(t('已迁移：重启 eggpaper 后生效'))
    f.data_dir = target
  } catch (e) { toast(e.message) }
  savingData.value = false
}

/* 护眼底纹：豆沙绿 / 浅青绿 / 米黄是三个公认的经典护眼色。
   点一下立刻生效（选颜色不看效果等于没选），所以不进「保存」，直接改 store。 */
const CARES = [
  { k: 'off', zh: '纯白', bg: '#ffffff' },
  { k: 'mung', zh: '豆沙绿', bg: '#c7edcc' },
  { k: 'cyan', zh: '浅青绿', bg: '#cce8e8' },
  { k: 'sand', zh: '米黄', bg: '#f5f5dc' },
]
function pickCare(k) { store.viewer.care = k; if (ui.dark) setDark(false) }   // 点了具体底纹=要亮色背景

/* 字号：四档，乘在 <html> 的 --fs-scale 上。点一下立刻生效，不进「保存」。 */
const FSS = [
  { k: 'sm', zh: '小' },
  { k: 'std', zh: '标准' },
  { k: 'lg', zh: '大' },
  { k: 'xl', zh: '特大' },
]
function pickFs(k) { store.viewer.fs = k }

/* 界面语言：点一下立即生效（整棵组件树读 ui.lang，切过去就是英文），
   再把选择同步给后端——LLM 的产出语言跟着它走。 */
function toggleLang() {
  const next = isEn() ? 'zh' : 'en'
  setLang(next)
  api.saveSettings({ ui_lang: next }).then(r => { store.settings = r }).catch(() => { /* 后端没收到也不回切：下次启动再同步 */ })
}
const testing = ref(false)
const testMark = ref('')          // 'ok' | 'bad' | ''
const testDetail = ref('')

async function test() {
  testing.value = true
  testMark.value = ''
  try {
    await api.saveSettings({ provider: { base_url: f.base_url, model: f.model, api_key: f.api_key }, mock: f.mock })
    store.settings = await api.settings()
    const r = await api.testSettings()
    testMark.value = r.ok ? 'ok' : 'bad'
    testDetail.value = r.reply
  } catch (e) {
    testMark.value = 'bad'
    testDetail.value = e.message
  }
  testing.value = false
}

/* 检查更新：先把自动检查的开关存下来再查（测的必须是刚改过的设置） */
const checking = ref(false)
async function checkNow() {
  checking.value = true
  try {
    await api.saveSettings({ update: { auto_check: f.auto_check } })
    store.settings = await api.settings()
    const r = await checkUpdate(true, false)
    if (r?.has_update) { emit('close'); return }         // 有新版：把弹窗让给更新卡片
    toast(r?.ok ? t('已经是最新的（{v}）', { v: r.current }) : t('没读到更新源：{m}', { m: r?.reason || t('地址为空') }))
  } finally {
    checking.value = false                               // 失败也不能把按钮永远停在「检查中…」
  }
}

/* 在独立窗口打开：没有地址栏/标签页的一个窗口，任务栏里就是 eggpaper 自己。 */
async function openWindow() {
  try { await api.nativeWindow() }
  catch (e) { toast(e.message) }
}

/* 截图目录：资源管理器直接开（目录没建过后端会现建）。 */
async function openShots() {
  try { await api.screenshotFolder() }
  catch (e) { toast(e.message) }
}

function openGuide() { window.open('/guide', '_blank') }
function openModel() { window.open('/model', '_blank') }

/* 全文翻译引擎（pdf2zh_next）：不在安装包里（渠道上限 100MB，380MB 的引擎另装）。
   状态先显示上次的结果（localStorage），后台再刷新——打开设置不再闪"未安装"。
   安装进度看全局的 engInst（engine.js）：不管安装从哪里发起（这里手动、
   点「全文翻译」时自动），这一行和右下角等待卡看到的是同一份状态。 */
const eng = reactive({ busy: false, ok: false, path: '', why: '', version: '', checked: false })

async function checkEngine() {
  eng.busy = true
  try {
    const r = await api.pdf2zhEngine(f.engine_path.trim())
    Object.assign(eng, { ok: r.ok, path: r.path, why: r.why, version: r.version || '', checked: true })
    lsSet('engState', { ok: r.ok, path: r.path, why: r.why, version: r.version || '' })
  } catch (e) {
    Object.assign(eng, { ok: false, path: '', why: e.message, version: '', checked: true })
  }
  eng.busy = false
}

async function installEngine() {
  await startEngineInstall()
}

/* 安装完成的同步：engine.js 已经把 engState 缓存刷掉了，这里把它接过来，
   这一行立刻从「下载中 x%」变「可用（…）」——哪怕安装是从别处发起的。 */
onEngineReady(() => {
  const st = lsGet('engState', null)
  if (st) {
    Object.assign(eng, { ok: st.ok, path: st.path, why: st.why, checked: true })
    f.engine_path = st.path || f.engine_path
  }
})

/* 从本地 zip 装：网络到不了 GitHub 时的正路（下好一份跟安装包一起发）。
   进度与收尾统一交给 engine.js 的唯一轮询——上面引擎行渲染的 engInst 就是它。 */
const zipInput = ref(null)
async function installFromFile(ev) {
  const file = ev.target.files?.[0]
  ev.target.value = ''
  if (!file) return
  try {
    await api.pdf2zhInstallFromFile(file)
    watchEngine()
  } catch (e) {
    toast(e.message)
  }
}

onMounted(async () => {
  const cached = lsGet('engState', null)
  if (cached) Object.assign(eng, cached, { checked: true })   // 先显示上次的结论，不闪按钮
  checkEngine()
  try {
    const s = await api.pdf2zhInstallStatus()
    if (s.state === 'done' && !eng.ok) checkEngine()
  } catch { /* 无所谓 */ }
})

function save() {
  Object.assign(store.viewer.layers, f.layers)
  emit('save', { provider: { base_url: f.base_url, model: f.model, api_key: f.api_key,
                             vision_model: f.vision ? f.model : '' },
                 mock: f.mock, pdf2zh: { service: f.service, path: f.engine_path.trim(),
                                         deepl_key: f.deepl_key.trim() },
                 update: { auto_check: f.auto_check },
                 shot_save: f.shot_save })
}
</script>

<template>
    <div class="modal-mask" ref="maskEl">
    <Transition name="pop" appear>
    <div class="modal settings" v-drag>
      <div class="modal-head" data-drag>
        <h3>{{ t('设置') }}</h3>
        <span class="head-ver">{{ store.update.current }}</span>
        <button class="head-link" @click="openGuide">{{ t('使用指南') }}</button>
        <button class="modal-x" :title="t('关闭')" @click="emit('close')">×</button>
      </div>
      <div class="f-row">
        <label class="mono-label">BASE URL
          <button class="lnk" style="margin-left:6px" @click="openModel">{{ t('教程') }}</button>
        </label>
        <input type="text" v-model="f.base_url" placeholder="https://api.deepseek.com" />
      </div>
      <div class="f-row">
        <label class="mono-label">API KEY</label>
        <input type="text" v-model="f.api_key" placeholder="sk-…" />
      </div>
      <div class="f-row">
        <label class="mono-label">{{ t('模型') }}</label>
        <div class="model-row">
          <input type="text" v-model="f.model" placeholder="deepseek-flash / glm-4.7-flash / ..." />
          <label class="viz-ck"><input type="checkbox" v-model="f.vision" />{{ t('视觉') }}</label>
        </div>
        <div class="viz-sub">
          <button class="test-btn" :class="testMark" @click="test" :disabled="testing"
                  :title="testDetail">{{ testing ? t('测试中…') : t('测试连接') }}</button>
          <span v-if="testMark" class="tmark" :class="testMark" :title="testDetail">{{ testMark === 'ok' ? '✓' : '✗' }}</span>
        </div>
      </div>
      <div class="f-line">
        <span class="mono-label" style="margin:0">{{ t('图层') }}</span>
        <label class="ck"><input type="checkbox" v-model="f.layers.marginalia" />{{ t('AI 眉批') }}</label>
        <label class="ck" style="margin-left:14px"><input type="checkbox" v-model="f.layers.mine" />{{ t('我的眉批') }}</label>
        <label class="ck" style="margin-left:14px"><input type="checkbox" v-model="f.mock" />{{ t('演示模式') }}</label>
      </div>
      <div class="f-row">
        <label class="mono-label">{{ t('护眼底纹') }}</label>
        <div class="care-row">
          <button v-for="c in CARES" :key="c.k" class="care-chip" :class="{ on: store.viewer.care === c.k && !ui.dark }"
                  @click="pickCare(c.k)">
            <i :style="{ background: c.bg }"></i>{{ t(c.zh) }}
          </button>
          <button class="care-chip" :class="{ on: ui.dark }" @click="setDark(!ui.dark)">{{ t('暗色') }}</button>
        </div>
      </div>
      <div class="f-row">
        <label class="mono-label">{{ t('字号') }}</label>
        <div class="care-row">
          <button v-for="s in FSS" :key="s.k" class="care-chip fs-chip" :class="{ on: store.viewer.fs === s.k }"
                  @click="pickFs(s.k)">
            <i :style="{ fontSize: FS_SCALE[s.k] + 'em' }">A</i>{{ t(s.zh) }}
          </button>
        </div>
      </div>
      <div class="f-row">
        <label class="mono-label">{{ t('语言') }}</label>
        <div class="care-row">
          <button class="care-chip" :class="{ on: isEn() }" @click="toggleLang">English</button>
        </div>
      </div>
      <div class="f-row" v-if="!isEn()">
        <label class="mono-label">{{ t('全文翻译服务') }}</label>
        <select v-model="f.service">
          <option value="bing">{{ t('bing（免费）') }}</option>
          <option value="openai">{{ t('openai（用上面的模型与端点）') }}</option>
          <option v-if="f.service === 'deepseek'" value="deepseek">{{ t('deepseek（已不推荐，请换一个）') }}</option>
          <option value="google">google</option>
          <option value="deepl">deepl</option>
        </select>
      </div>
      <div class="f-row" v-if="!isEn() && f.service === 'deepl'">
        <label class="mono-label">{{ t('DeepL Key') }}</label>
        <input type="text" v-model="f.deepl_key" :placeholder="t('DeepL 的 AUTH_KEY（deepl.com/developers）')" />
      </div>
      <div class="f-line" v-if="!isEn() && !(eng.checked && eng.ok)">
        <span class="mono-label" style="margin:0">{{ t('翻译引擎') }}</span>
        <span class="eng-state" :class="{ bad: eng.checked && !eng.ok, ok: eng.ok }">
          <template v-if="engInst.state === 'downloading'">{{ t('下载中 {p}%', { p: engInst.pct }) }} · {{ engInst.src }}</template>
          <template v-else-if="engInst.state === 'unpacking'">{{ t('解压中…') }}</template>
          <template v-else-if="engInst.state === 'warming'">{{ t('引擎预热中（下载版面模型）…') }}</template>
          <template v-else-if="engInst.state === 'error'">{{ engInst.error }}</template>
          <template v-else-if="!eng.checked">…</template>
          <template v-else-if="eng.ok">{{ t('可用（{v}）', { v: eng.version || eng.why.replace('pdf2zh', '').trim() }) }}</template>
          <template v-else>{{ t('未安装') }}</template>
        </span>
        <button class="eng-check" style="margin-left:auto" @click="checkEngine" :disabled="eng.busy">
          {{ eng.busy ? '…' : t('检测') }}</button>
      </div>
      <div class="f-row">
        <label class="mono-label">{{ t('数据目录') }}
          <button class="eng-check" style="margin-left:8px" @click="api.revealUpdate(f.data_dir)">{{ t('打开') }}</button>
          <button class="eng-check" style="margin-left:4px" @click="moveData" :disabled="picking || savingData">
            {{ savingData ? t('迁移中…') : t('迁移') }}</button>
        </label>
        <div class="eng-state">{{ f.data_dir }}</div>
      </div>
      <div class="f-row" v-if="!isEn() && !eng.ok && !['downloading', 'unpacking', 'warming'].includes(engInst.state)">
        <div class="model-row">
          <input type="text" v-model="f.engine_path" :placeholder="t('pdf2zh.exe 路径（留空自动找）')" />
          <button class="eng-install" @click="installEngine">{{ t('下载安装约 600MB') }}</button>
          <button class="eng-file" @click="zipInput?.click()">{{ t('选 zip 安装') }}</button>
        </div>
        <input ref="zipInput" type="file" accept=".zip" hidden @change="installFromFile" />
      </div>
      <div class="f-line">
        <span class="mono-label" style="margin:0">{{ t('截图') }}</span>
        <label class="ck" style="margin-left:14px"><input type="checkbox" v-model="f.shot_save" />{{ t('保存到本地') }}</label>
        <button style="margin-left:auto;padding:2px 10px;font-size:var(--fs-sm)" @click="openShots">{{ t('打开目录') }}</button>
      </div>
      <div class="f-line">
        <span class="mono-label" style="margin:0">{{ t('更新') }}</span>
        <label class="ck"><input type="checkbox" v-model="f.auto_check" />{{ t('打开时自动检查') }}</label>
        <button style="margin-left:auto;padding:2px 10px;font-size:var(--fs-sm)"
                @click="checkNow" :disabled="checking">{{ checking ? t('检查中…') : t('立即检查更新') }}</button>
      </div>
      <div class="f-line">
        <span class="mono-label" style="margin:0">{{ t('窗口') }}</span>
        <button style="margin-left:auto;padding:2px 10px;font-size:var(--fs-sm)" @click="openWindow">{{ t('在独立窗口打开') }}</button>
      </div>
      <div class="f-actions">
        <button v-if="store.update.packaged" class="quit-btn"
                @click="emit('quit')">{{ t('退出 eggpaper') }}</button>
        <button class="primary" @click="save">{{ t('保存') }}</button>
      </div>
    </div>
    </Transition>
  </div>
</template>
