<script setup>
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, store, toast, refreshPapers, openPaper, refreshAnalysis, refreshMarginalia } from './store'
import PdfViewer from './components/PdfViewer.vue'
import LeftRail from './components/LeftRail.vue'
import RightRail from './components/RightRail.vue'
import SettingsModal from './components/SettingsModal.vue'
import EggMark from './components/EggMark.vue'

const showSettings = ref(false)
const dragOver = ref(false)
let pollTimer = null

onMounted(async () => {
  store.settings = await api.settings()
  await refreshPapers()
  if (store.papers.length) openPaper(store.papers[0].id)
  pollTimer = setInterval(poll, 3000)
})
onUnmounted(() => clearInterval(pollTimer))

async function poll() {
  if (!store.currentId) return
  if (store.analysis.status === 'running') await refreshAnalysis()
  if (store.marginalia.status === 'running') await refreshMarginalia()
  const p = store.papers.find(x => x.id === store.currentId)
  if (p && p.translate_status === 'running') {
    const j = await api.translateStatus(store.currentId)
    if (j.status === 'done') { await refreshPapers(); toast('双语全文已生成，切换「双语」查看') }
    if (j.status === 'error') { await refreshPapers(); toast('整本翻译失败：' + (j.error || '').slice(0, 80)) }
  }
}

async function doAnalyze() {
  if (!store.currentId) return
  await api.analyze(store.currentId)
  await refreshAnalysis()
  toast('正在通读全文，从作者的视角找骨架…')
}

async function doMarginalia() {
  if (!store.currentId) return
  await api.marginaliaStart(store.currentId)
  await refreshMarginalia()
  toast('正在逐句写眉批：找凑字数、妥协与 AI 痕迹…')
}

async function doTranslateFull() {
  if (!store.currentId) return
  try {
    await api.translateFull(store.currentId)
    await refreshPapers()
    toast('整本翻译已启动（pdf2zh），完成后右上角会提示')
  } catch (e) {
    toast('启动失败：' + e.message)
  }
}

async function onPickFile(file) {
  if (!file) return
  try {
    toast('正在导入并解析…')
    const r = await api.upload(file)
    await refreshPapers()
    await openPaper(r.paper.id)
    toast(`解析完成：${r.n_paragraphs} 个正文段落`)
  } catch (e) {
    toast('导入失败：' + e.message)
  }
}

function onDrop(e) {
  dragOver.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f) onPickFile(f)
}

async function saveSettings(body) {
  store.settings = await api.saveSettings(body)
  showSettings.value = false
  toast('设置已保存（本地 config.yaml，不入库）')
  if (store.currentId) refreshAnalysis()
}

function currentTranslateStatus() {
  return store.papers.find(x => x.id === store.currentId)?.translate_status || 'none'
}

watch(() => store.currentId, () => {})
</script>

<template>
  <div class="app" @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="onDrop">
    <!-- 刊头 -->
    <header class="topbar">
      <div class="wordmark">
        <EggMark class="egg" />
        <span class="name">eggpaper</span>
        <span class="vol">VOL.0.1 · 阅读过默</span>
      </div>
      <div class="doc-head" v-if="store.paper">
        <div class="t">{{ store.paper.title || store.paper.filename }}</div>
        <div class="m">{{ store.paper.n_pages }} 页 · {{ store.paras.length }} 个正文段落 · {{ store.paper.filename }}</div>
      </div>
      <div class="doc-head" v-else>
        <div class="t">本地文献批注台</div>
        <div class="m">LOCAL-FIRST · 无账号 · 文献不出本机</div>
      </div>
      <div class="actions" v-if="store.paper">
        <div class="toggles">
          <button class="toggle" :class="{ on: store.viewer.layers.skeleton }" @click="store.viewer.layers.skeleton = !store.viewer.layers.skeleton">骨架</button>
          <button class="toggle" :class="{ on: store.viewer.layers.marginalia }" @click="store.viewer.layers.marginalia = !store.viewer.layers.marginalia">眉批</button>
          <button class="toggle" :class="{ on: store.viewer.layers.skim }" @click="store.viewer.layers.skim = !store.viewer.layers.skim">略读</button>
        </div>
        <div class="segmented">
          <button :class="{ on: store.viewer.variant === 'original' }" @click="store.viewer.variant = 'original'">原文</button>
          <button :class="{ on: store.viewer.variant === 'dual' }" :disabled="currentTranslateStatus() !== 'done'"
                  @click="store.viewer.variant = 'dual'">双语</button>
        </div>
        <button @click="doTranslateFull" :disabled="currentTranslateStatus() === 'running'">整本翻译</button>
        <button v-if="store.analysis.status === 'done' && store.marginalia.status !== 'done' && store.marginalia.status !== 'running'"
                @click="doMarginalia">写眉批</button>
        <button class="primary" @click="doAnalyze" :disabled="store.analysis.status === 'running'">
          {{ store.analysis.status === 'running' ? '通读中…' : (store.analysis.status === 'done' ? '重新析读' : '析读全文') }}
        </button>
      </div>
      <div class="actions">
        <span class="status-dot" :class="store.mock ? 'warn' : 'ok'" :title="store.mock ? '演示模式' : 'LLM 已配置'"></span>
        <button class="ghost" @click="showSettings = true" title="设置">⚙</button>
      </div>
    </header>

    <div class="main">
      <LeftRail @pick="openPaper" @import="onPickFile" />

      <main class="desk" @drop.stop>
        <div class="empty" v-if="!store.paper">
          <svg class="egg-big" viewBox="0 0 100 125" fill="none">
            <path d="M50 6 C26 30 12 62 12 82 a38 40 0 0 0 76 0 C88 62 74 30 50 6 Z" stroke="#8d8066" stroke-width="2.5" fill="#fffdf6" />
            <circle cx="50" cy="82" r="17" fill="#d08a1c" opacity="0.85" />
            <path d="M32 44 C36 36 42 28 50 20" stroke="#d08a1c" stroke-width="2.5" stroke-linecap="round" />
          </svg>
          <div class="e-title">剥开论文的壳，读论证的芯</div>
          <div class="e-sub">把 PDF 拖到这里，或从左侧文库选择一篇</div>
          <div class="stamp">EGGPAPER · V0.1 · LOCAL-FIRST</div>
        </div>
        <PdfViewer v-else :key="store.currentId + store.viewer.variant" />
        <div class="colophon" v-if="store.paper">EGGPAPER · 骨架与眉批皆为推断，请回到原文核验 · 文献不出本机</div>
      </main>

      <RightRail @analyze="doAnalyze" @marginalia="doMarginalia" />
    </div>

    <SettingsModal v-if="showSettings" @close="showSettings = false" @save="saveSettings" />
    <div class="toast" v-if="store.toast">{{ store.toast }}</div>
    <div class="modal-mask" v-if="dragOver" style="pointer-events: none; background: rgba(38,32,21,.25)">
      <div class="modal" style="text-align:center">
        <div class="serif" style="font-size:18px">松手，把这篇论文放进书桌</div>
      </div>
    </div>
  </div>
</template>
