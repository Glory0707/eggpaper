<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, store, toast, refreshPapers, refreshCollections, openPaper, refreshAnalysis,
         reloadSummary } from './store'
import PdfViewer from './components/PdfViewer.vue'
import LibPanel from './components/LeftRail.vue'
import RightRail from './components/RightRail.vue'
import SettingsModal from './components/SettingsModal.vue'
import Dialog from './components/Dialog.vue'
import { dlg, dlgCancel } from './dialog'
import EggMark from './components/EggMark.vue'

const showSettings = ref(false)
const dragOver = ref(false)
const roll = ref(false)          // 完成一个动作时，印章滚一下（借蛋仔的"动作"，不借它的配色）
const gPending = ref(false)
const VARIANTS = ['original', 'mono', 'dual']
let pollTimer = null

function rollOnce(ms = 700) {
  roll.value = false
  requestAnimationFrame(() => { roll.value = true })
  clearTimeout(rollOnce._t)
  rollOnce._t = setTimeout(() => (roll.value = false), ms)
}

const tranReady = computed(() => store.paper?.translate_status === 'done')

/* ---------------- 拖入导入 ----------------
   之前只挂 dragover/dragleave，所以浮层「进得来、出不去」：dragleave 会为每个子元素
   都触发一次（指针在论文上移动就疯狂开关），而真正离开窗口、或者松手落在窗口外时，
   事件根本不落到 .app 上，没人把它清掉。三条边界都得自己补：
   ① 只有真拖着文件才算——拖选中的文字、拖页面里的图片不该弹「放到书桌上」；
   ② 用 relatedTarget 为空判断「真的离开窗口」，别被子元素的 dragleave 骗到；
   ③ dragend / 窗口失焦也清，因为松手可能落在窗口之外。 */
function hasFiles(e) { return Array.from(e.dataTransfer?.types || []).includes('Files') }
function onDragEnter(e) { if (hasFiles(e)) dragOver.value = true }
function onDragOver(e) { if (hasFiles(e)) e.preventDefault() }   // 不 preventDefault 就不许 drop
function onDragLeave(e) { if (hasFiles(e) && e.relatedTarget == null) dragOver.value = false }
function onDrop(e) {
  e.preventDefault()
  dragOver.value = false
  onPickFile(e.dataTransfer?.files?.[0])
}
function endDrag() { dragOver.value = false }

onMounted(async () => {
  store.settings = await api.settings()
  await refreshPapers()
  await refreshCollections()
  if (store.papers.length) openPaper(store.papers[0].id)
  pollTimer = setInterval(poll, 3000)
  window.addEventListener('keydown', onKey)
  window.addEventListener('dragend', endDrag)
  window.addEventListener('blur', endDrag)
})
onUnmounted(() => {
  clearInterval(pollTimer)
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('dragend', endDrag)
  window.removeEventListener('blur', endDrag)
})

async function poll() {
  if (!store.currentId) return
  if (store.analysis.status === 'running') await refreshAnalysis()
  if (store.marginalia.status === 'running') await refreshMarginalia()
  const p = store.papers.find(x => x.id === store.currentId)
  if (p && p.translate_status === 'running') {
    const j = await api.translateStatus(store.currentId)
    if (j.status === 'done') { await refreshPapers(); rollOnce(); toast('双语已生成，切「译文」或「双语」查看') }
    if (j.status === 'error') { await refreshPapers(); toast('整本翻译失败：' + (j.error || '').slice(0, 80)) }
  }
}

/* 窄窗：右栏改浮层，进窄窗时自动收起一次，把宽度还给论文
   （只在跨过门槛那一拍动手，否则用户手动展开会被反复关掉） */
watch(() => store.narrow, (n, o) => { if (n && !o) store.viewer.railUser = false })

watch(() => store.analysis.status, (n, o) => {
  // 析读把一眼卡一起作废了（服务端清了缓存），所以这里要重新取一次
  if (o === 'running' && n === 'done') { rollOnce(); reloadSummary() }
})

async function doAnalyze() {
  if (!store.currentId) return
  await api.analyze(store.currentId)
  await refreshAnalysis()
}

async function onOverride({ idx, role }) {
  try {
    await api.overrideRole(store.currentId, idx, role)
    await refreshAnalysis()
    toast(role ? '已改判' : '已回到推断')
  } catch (e) { toast('改判失败：' + e.message) }
}

async function doMarginalia() {
  if (!store.currentId) return
  await api.marginaliaStart(store.currentId)
  await refreshMarginalia()
}

async function doTranslateFull() {
  if (!store.currentId) return
  try {
    await api.translateFull(store.currentId)
    await refreshPapers()
    toast('整本翻译已启动，完成后自动提示')
  } catch (e) { toast('启动失败：' + e.message) }
}

async function onPickFile(file) {
  if (!file) return
  toast('已导入，正在后台通读…')
  try {
    const r = await api.upload(file)
    await refreshPapers()
    // 正在看某个分类时导入的，就顺手归到那个分类里——Zotero 的"导入到分类"一个意思
    const c = store.lib.coll
    if (typeof c === 'number') {
      try { await api.paperColls(r.paper.id, [c]); await refreshCollections() } catch { /* 归类失败不影响导入 */ }
    }
    await openPaper(r.paper.id)
    store.viewer.libOpen = false
    if (r.no_text) toast('这份 PDF 没有可提取的文字（可能是扫描件），只有阅读功能可用')
    else if (r.n_paragraphs && r.n_paragraphs < 5) toast('这份 PDF 只认出 ' + r.n_paragraphs + ' 段，析读结果可能很粗')
  } catch (e) { toast('导入失败：' + e.message) }
}

async function saveSettings(body) {
  store.settings = await api.saveSettings(body)
  showSettings.value = false
  toast('设置已保存（仅本机）')
  if (store.currentId) refreshAnalysis()
}

const tranSt = computed(() => store.papers.find(x => x.id === store.currentId)?.translate_status || 'none')

/* ---------------- 键盘流 ---------------- */
function onKey(e) {
  const t = e.target
  // 对话框是最上面一层：Esc 先关它，别的浮层这一拍都别动
  // （否则"删分类"弹窗开着按 Esc，会把整个文库也一起收掉）
  if (dlg.open) { if (e.key === 'Escape') { e.preventDefault(); dlgCancel() } return }
  if (t && (t.matches?.('input, textarea, select') || t.isContentEditable)) return
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') { e.preventDefault(); store.viewerApi?.openSearch(); return }
  if (e.altKey && e.key === 'ArrowLeft') { store.viewerApi?.jumpBack(); e.preventDefault(); return }
  if (e.key === 'Escape') {
    store.viewer.libOpen = false
    store.shortcutCard = false
    showSettings.value = false
    dragOver.value = false
    store.viewer.frame = false     // 框选模式永远能一键退出
    store.escTick++                // PDF 侧的划词/框选/角色卡/查找浮层收起
    return
  }
  if (gPending.value) {
    gPending.value = false
    if (e.key === 'l') { store.viewer.libOpen = true; e.preventDefault() }
    return
  }
  if (!store.paper) return
  switch (e.key) {
    case 'j': e.preventDefault(); store.viewerApi?.step(1); break
    case 'k': e.preventDefault(); store.viewerApi?.step(-1); break
    case 'PageDown': e.preventDefault(); store.viewerApi?.stepPage(1); break
    case 'PageUp': e.preventDefault(); store.viewerApi?.stepPage(-1); break
    case 't': store.viewerApi?.translateCurrent(); break
    case 's': store.viewerApi?.translateSelectionKey(); break
    case 'f': store.viewer.layers.skim = !store.viewer.layers.skim; break
    case 'r': store.viewer.frame = !store.viewer.frame; break
    case '1': store.viewer.variant = 'original'; break
    case '2': if (tranSt.value === 'done') store.viewer.variant = 'mono'; break
    case '3': if (tranSt.value === 'done') store.viewer.variant = 'dual'; break
    case '/': e.preventDefault(); store.askFocusTick++; break
    case 'x': store.viewer.railUser = !store.viewer.railUser; break
    case 'g': gPending.value = true; setTimeout(() => (gPending.value = false), 700); break
    case '?': store.shortcutCard = !store.shortcutCard; break
  }
}
</script>

<template>
  <div class="app" @dragenter="onDragEnter" @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop">
    <header class="topbar">
      <div class="wordmark" title="eggpaper">
        <EggMark class="egg" compact :class="{ roll }" />
        <span class="name">eggpaper</span>
      </div>
      <div class="doc-head" v-if="store.paper">
        <div class="t">{{ store.paper.title || store.paper.filename }}</div>
      </div>
      <div class="doc-head" v-else>
        <div class="t">本地文献批注台</div>
      </div>
      <div class="actions" v-if="store.paper">
        <div class="segmented" :style="{ '--n': 3, '--i': VARIANTS.indexOf(store.viewer.variant) }">
          <span class="seg-thumb" />
          <button :class="{ on: store.viewer.variant === 'original' }" @click="store.viewer.variant = 'original'">原文</button>
          <button :class="{ on: store.viewer.variant === 'mono' }" :disabled="tranSt !== 'done'" @click="store.viewer.variant = 'mono'">译文</button>
          <button :class="{ on: store.viewer.variant === 'dual' }" :disabled="tranSt !== 'done'" @click="store.viewer.variant = 'dual'">双语</button>
        </div>
        <Transition name="fade">
          <div class="segmented mini" v-if="store.viewer.variant === 'dual'"
               :style="{ '--n': 2, '--i': store.viewer.spread === 'spread' ? 0 : 1 }">
            <span class="seg-thumb" />
            <button :class="{ on: store.viewer.spread === 'spread' }" @click="store.viewer.spread = 'spread'">对开</button>
            <button :class="{ on: store.viewer.spread === 'interleave' }" @click="store.viewer.spread = 'interleave'">交替</button>
          </div>
        </Transition>
        <button class="toggle" :class="{ on: store.viewer.layers.skim }" @click="store.viewer.layers.skim = !store.viewer.layers.skim">略读</button>
        <button class="toggle" :class="{ on: store.viewer.frame }" title="框选问 AI（r）"
                @click="store.viewer.frame = !store.viewer.frame">框选</button>
        <!-- 整本翻译：把 PDF 整篇译成第二份文档（奇页原文偶页译文），译文/双语两个模式靠它。
             译完就没必要再露出来了——留一个永远点不动的按钮只会让人猜它还能干什么。 -->
        <button v-if="tranSt !== 'done'" @click="doTranslateFull" :disabled="tranSt === 'running'"
                title="用 pdf2zh 把整篇译成第二份 PDF，译文/双语两个模式靠它">
          {{ tranSt === 'running' ? '翻译中…' : '整本翻译' }}
        </button>
        <button class="primary" @click="doAnalyze" :disabled="store.analysis.status === 'running'">
          {{ store.analysis.status === 'running' ? '通读中…' : (store.analysis.status === 'done' ? '重新析读' : '析读') }}
        </button>
      </div>
      <div class="actions">
        <button class="ghost" @click="showSettings = true" title="设置">⚙</button>
      </div>
    </header>

    <div class="main">
      <!-- 左：图标条 -->
      <div class="left-strip">
        <button class="strip-btn" :class="{ on: store.viewer.libOpen }" title="文库 · g l"
                @click="store.viewer.libOpen = !store.viewer.libOpen">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M4 4h6v16H4zM14 4h6v16h-6z" />
            <path d="M7 8h.01M7 12h.01M17 8h.01M17 12h.01" stroke-linecap="round" stroke-width="2.4" />
          </svg>
          <span class="badge" v-if="store.papers.length">{{ store.papers.length }}</span>
        </button>
        <div class="strip-sep"></div>
      </div>

      <!-- 中：书桌。drop 不拦在这里：让它冒到 .app 统一收，拖到纸上也能导入 -->
      <main class="desk">
        <div class="empty" v-if="!store.paper">
          <EggMark class="egg-big" :class="{ hop: dragOver }" />
          <div class="e-title">一篇读透，胜过扫过十篇</div>
          <div class="e-sub">把 PDF 拖进来，或按 g l 打开文库</div>
          <div class="stamp">EGGPAPER · LOCAL-FIRST</div>
        </div>
        <PdfViewer v-else :key="store.currentId" @override="onOverride" />
      </main>

      <!-- 右栏折叠把手 -->
      <button class="rail-tab" v-if="store.paper && !store.railRight" title="展开右栏 · x"
              @click="store.viewer.railUser = true">◂</button>
      <!-- 没有论文就没有右栏：一个只有页签的空栏目会让人以为它坏了，
           而且里面的问答会对着一个不存在的 paper_id 发请求 -->
      <div class="rail-wrap" v-if="store.paper" :class="{ collapsed: !store.railRight }"
           :style="{ '--rail-w': store.viewer.railW + 'px' }">
        <RightRail @analyze="doAnalyze" @marginalia="doMarginalia" />
      </div>
    </div>

    <Transition name="fade">
      <div class="lib-mask" v-if="store.viewer.libOpen" @click="store.viewer.libOpen = false"></div>
    </Transition>
    <Transition name="slide-l">
      <LibPanel v-if="store.viewer.libOpen" @import="onPickFile" @close="store.viewer.libOpen = false" />
    </Transition>
    <Transition name="fade">
      <SettingsModal v-if="showSettings" @close="showSettings = false" @save="saveSettings" />
    </Transition>
    <!-- 全局唯一的应用内对话框：别处 await confirmBox / inputBox 就行。
         别放进上面那个 Transition——Transition 只允许一个子节点，多一个就编译不过 -->
    <Dialog />

    <!-- 键盘卡 -->
    <Transition name="pop">
    <div class="keys-card" v-if="store.shortcutCard" @click="store.shortcutCard = false">
      <div class="mono-label" style="margin-bottom:8px">键盘</div>
      <div class="k-row"><span>下一段 / 上一段（略读时仅核心段）</span><kbd>j / k</kbd></div>
      <div class="k-row"><span>译当前段并钉页边</span><kbd>t</kbd></div>
      <div class="k-row"><span>翻译划选</span><kbd>s</kbd></div>
      <div class="k-row"><span>略读</span><kbd>f</kbd></div>
      <div class="k-row"><span>框选问 AI（Esc 退出）</span><kbd>r</kbd></div>
      <div class="k-row"><span>原文 / 译文 / 双语</span><kbd>1 / 2 / 3</kbd></div>
      <div class="k-row"><span>聚焦提问</span><kbd>/</kbd></div>
      <div class="k-row"><span>折叠右栏</span><kbd>x</kbd></div>
      <div class="k-row"><span>文库</span><kbd>g l</kbd></div>
      <div class="k-row"><span>返回原位</span><kbd>Alt + ←</kbd></div>
      <div class="k-row"><span>收起所有浮层 / 退出框选</span><kbd>Esc</kbd></div>
    </div>
    </Transition>

    <Transition name="pop">
      <div class="toast" v-if="store.toast">{{ store.toast }}</div>
    </Transition>
    <Transition name="fade">
    <div class="modal-mask" v-if="dragOver && store.paper" style="pointer-events:none; background:rgba(29,27,23,.22)">
      <div class="modal" style="text-align:center">
        <div style="font-size:var(--fs-xl);font-weight:650">松手，放到书桌上</div>
      </div>
    </div>
    </Transition>
  </div>
</template>
