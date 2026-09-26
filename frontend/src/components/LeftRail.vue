<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api, store, toast, refreshPapers, refreshCollections, openPaper, jumpPara, goHome, lsRemove } from '../store'
import { confirmBox } from '../dialog'
import { t } from '../i18n'
import { useEdgeResize } from '../edgeResize'
import ZoteroDialog from './ZoteroDialog.vue'
import CompareOverlay from './CompareOverlay.vue'
import CiteExport from './CiteExport.vue'

const emit = defineEmits(['import', 'close', 'splitMany'])
const fileInput = ref(null)
const over = ref(false)
const zotOpen = ref(false)
const cmpOpen = ref(false)
const cmpIds = ref([])

/* ---------- 分栏宽度：和右栏同一个手势（edgeResize.js） ---------- */
const LIB_MIN = 236, LIB_MAX = 560, LIB_DEF = 300
const lib = useEdgeResize({
  get: () => store.viewer.libW, set: w => { store.viewer.libW = w },
  min: LIB_MIN, max: LIB_MAX, def: LIB_DEF,
})
onBeforeUnmount(() => lib.end())

/* ---------- 列表：分类过滤 → 搜索 → 排序 ---------- */
const q = ref('')
const sort = computed({
  get: () => store.lib.sort,
  set: v => (store.lib.sort = v),
})
const colls = computed(() => store.lib.colls)
const collOf = pid => store.lib.map[pid] || []
const SEL = computed(() => store.lib.coll)   // 'all' | 'none' | <分类 id>

const shown = computed(() => {
  const kw = q.value.trim().toLowerCase()
  let list = store.papers.filter(p => {
    if (SEL.value === 'none') return collOf(p.id).length === 0
    if (typeof SEL.value === 'number') return collOf(p.id).includes(SEL.value)
    return true
  })
  let extra = []
  if (kw) {
    list = list.filter(p => (p.title || p.filename || '').toLowerCase().includes(kw))
    const inCat = id => SEL.value === 'all' || (SEL.value === 'none' ? !collOf(id).length : collOf(id).includes(SEL.value))
    const titleIds = new Set(list.map(p => p.id))
    extra = contentHits.value.filter(h => !titleIds.has(h.id) && inCat(h.id))
  }
  const s = store.lib.sort
  const title = p => (p.title || p.filename || '').toLowerCase()
  const sorted = [...list].sort((a, b) =>
    s === 'title' ? title(a).localeCompare(title(b))
    : s === 'year' ? (parseInt(b.year) || 0) - (parseInt(a.year) || 0)
    : s === 'read' ? String(b.last_read_at || b.created_at || '').localeCompare(String(a.last_read_at || a.created_at || ''))
    : String(b.created_at || '').localeCompare(String(a.created_at || '')))
  return [...sorted, ...extra]
})
const hitOf = pid => contentHits.value.find(h => h.id === pid)
const WHERE_KEYS = { glance: '一眼卡', seven: '七问', claims: '核心主张', qa: '问答', body: '正文' }

/* 全库内容检索：标题过滤是即时的（打一个字列表就收），内容命中防抖问后端——
   两路结果一个列表：标题命中的照旧排前，只在内容里命中的追加在后、带一行出处。 */
const contentHits = ref([])
let searchT = null
let searchSeq = 0
watch(q, v => {
  clearTimeout(searchT)
  const my = ++searchSeq
  const kw = v.trim()
  if (!kw) { contentHits.value = []; return }
  searchT = setTimeout(async () => {
    try {
      const r = await api.search(kw)
      if (my === searchSeq) contentHits.value = r
    } catch { if (my === searchSeq) contentHits.value = [] }
  }, 350)
})
onBeforeUnmount(() => clearTimeout(searchT))
const nUnfiled = computed(() => store.papers.filter(p => !collOf(p.id).length).length)

function pickColl(v) { store.lib.coll = v }

/* ---------- 分类的增改删 ---------- */
const adding = ref(false)
const newName = ref('')
const editId = ref(null)
const editName = ref('')
const menuFor = ref(null)        // 打开"归入分类"面板的那篇
const menuPicked = ref(new Set())  // 勾选暂存：点分类只改这里，点「完成」才一次性提交
const menuXY = ref({ x: 0, y: 0, up: false })
function openMenu(p, ev) {
  if (menuFor.value === p.id) { menuFor.value = null; return }
  const r = ev.currentTarget.getBoundingClientRect()
  const h = Math.min(64 + colls.value.length * 26, 320)
  const up = r.bottom + h > window.innerHeight - 8 && r.top - h > 8
  menuXY.value = {
    x: Math.min(r.left, window.innerWidth - 232),
    y: up ? r.top - h - 4 : r.bottom + 4,
    up,
  }
  menuFor.value = p.id
  menuPicked.value = new Set(collOf(p.id))
}
function flipPick(cid) {
  menuPicked.value.has(cid) ? menuPicked.value.delete(cid) : menuPicked.value.add(cid)
}
async function menuDone() {
  const pid = menuFor.value
  menuFor.value = null
  if (!pid) return
  const want = [...menuPicked.value]
  const cur = collOf(pid)
  if (want.length === cur.length && want.every(c => cur.includes(c))) return   // 没改就不发请求
  try {
    await api.paperColls(pid, want)
    await refreshCollections()
  } catch (e) { await collFail(e) }
}
/* 菜单挂在 body 上（防列表裁剪），点外面/按 Esc 收起 */
let menuArmT = null
watch(menuFor, (v, was) => {
  if (v && !was) {
    clearTimeout(menuArmT)
    menuArmT = setTimeout(() => document.addEventListener('click', closeMenu, { capture: true }), 0)
    document.addEventListener('keydown', escMenu, { capture: true })
  } else if (!v) {
    document.removeEventListener('click', closeMenu, { capture: true })
    document.removeEventListener('keydown', escMenu, { capture: true })
  }
})
function closeMenu(e) {
  // ＋ 按钮的点击交给 openMenu 自己的开/关切换（capture 监听先于 @click 触发）；
  // 菜单内部同理——这里在 capture 阶段，.stop 拦不住它，点菜单里先置空 menuFor
  // 会让随后的 menuDone 拿到 null，发出 /papers/null/collections（「论文不存在」的真凶）。
  if (e?.target?.closest?.('.p-tag, .coll-menu')) return
  menuFor.value = null
}
function escMenu(e) { if (e.key === 'Escape') closeMenu() }
onBeforeUnmount(() => {
  clearTimeout(menuArmT)   // 组件先卸、定时器后补挂监听 = 永久泄漏，一并掐掉
  document.removeEventListener('click', closeMenu, { capture: true })
  document.removeEventListener('keydown', escMenu, { capture: true })
})

async function createColl() {
  const n = newName.value.trim()
  if (!n) { adding.value = false; return }
  try {
    const r = await api.collAdd(n)
    await refreshCollections()
    store.lib.coll = r.id        // 新建完直接切过去，符合"我建它就是要往里放东西"
  } catch (e) { toast(e.message) }
  newName.value = ''
  adding.value = false
}
function startRename(c) { editId.value = c.id; editName.value = c.name; nextTick(() => document.querySelector('.coll-edit')?.select()) }
async function doRename(c) {
  const n = editName.value.trim()
  editId.value = null
  if (!n || n === c.name) return
  try { await api.collRename(c.id, n); await refreshCollections() } catch (e) { toast(e.message) }
}
async function delColl(c) {
  const yes = await confirmBox({
    title: t('删除分类'), ok: t('删除'), danger: true,
    body: t('里面的文献不会被删。'),
  })
  if (!yes) return
  try {
    await api.collDelete(c.id)
    if (store.lib.coll === c.id) store.lib.coll = 'all'
    await refreshCollections()
  } catch (e) { toast(e.message) }
}

/* ---------- 归入分类：勾选 + 拖拽两条路 ---------- */
/* 归类失败若是"论文不存在"：列表八成是陈旧的（别的窗口删过）——刷新掉，别让用户对着死行反复撞墙 */
async function collFail(e) {
  if (String(e.message || '').includes('不存在')) {
    await refreshPapers()
    toast(t('这篇已被删除'))
  } else toast(e.message)
}
const dragPid = ref(null)
async function dropOn(cid) {
  const pid = dragPid.value
  dragPid.value = null
  if (pid == null) return
  const cur = collOf(pid)
  if (cur.includes(cid)) return
  const c = colls.value.find(x => x.id === cid)
  try {
    await api.paperColls(pid, [...cur, cid])
    await refreshCollections()
    if (c) toast(t('已归入「{name}」', { name: c.name }))
  } catch (e) { await collFail(e) }
  menuFor.value = null
}

/* ---------- 导入 / 删除 ---------- */
function onFile(e) {
  emit('import', Array.from(e.target.files || []))
  e.target.value = ''
}
function onDrop(e) {
  over.value = false
  emit('import', Array.from(e.dataTransfer?.files || []))
}
async function _removePids(pids) {
  for (const pid of pids) {
    try {
      await api.deletePaper(pid)
    } catch (e) { toast(t('删除失败：{m}', { m: e.message })); continue }
    selSet.value.delete(pid)     // 勾选里摘掉：残留死 id 会让下一次批量操作 404「论文不存在」
    lsRemove('pos:' + pid)     // 阅读位置也别留在浏览器里（键的拼法只认 ls.js 这一处前缀）
    store.openIds = store.openIds.filter(x => x !== pid)   // 同屏窗格里也摘掉这一篇
    if (store.currentId === pid) {
      const next = store.openIds[0]
      if (next) {
        await store.activatePaper(next, false)             // 同屏还有别的篇：切过去
      } else {
        goHome()          // 清场走 store 的正主：epoch/summaryErr/lastPaper 一个不漏
      }
    }
  }
  await refreshPapers()
  await refreshCollections()
}
async function del(pid, name) {
  const yes = await confirmBox({
    title: t('删除文献'), ok: t('删除'), danger: true,
    body: t('《{name}》及其批注、析读、问答将一并删除。', { name: name.slice(0, 40) }),
  })
  if (!yes) return
  await _removePids([pid])
}
function touch(p) { if (p.id !== store.currentId) openPaper(p.id) }

/* ---------- 多选：勾几篇，批量做事（同屏/对比/分类/删除） ---------- */
const selMode = ref(false)
const selSet = ref(new Set())
const selN = computed(() => selSet.value.size)
const canSplit = computed(() => selN.value >= 2 && selN.value <= 4)
const canCompare = computed(() => selN.value >= 2 && selN.value <= 5)
const selMenu = ref(false)
function toggleSelMode() {
  selMode.value = !selMode.value
  selSet.value = new Set()
  selMenu.value = false
}
function toggleSel(pid) {
  const s = new Set(selSet.value)
  s.has(pid) ? s.delete(pid) : s.add(pid)
  selSet.value = s
}
/* Esc 先退选择模式，再轮到别的 Esc 语义 */
function escSel(e) {
  if (e.key !== 'Escape') return
  e.preventDefault(); e.stopPropagation()
  if (selMenu.value) { selMenu.value = false; return }
  toggleSelMode()
}
watch(selMode, v => {
  if (v) document.addEventListener('keydown', escSel, true)
  else document.removeEventListener('keydown', escSel, true)
})
onBeforeUnmount(() => document.removeEventListener('keydown', escSel, true))

function doSplit() {
  if (!canSplit.value) return
  emit('splitMany', [...selSet.value])
  toggleSelMode()
}
function doCompare() {
  if (!canCompare.value) return
  cmpIds.value = [...selSet.value]
  cmpOpen.value = true          // 覆盖层关掉后回选择模式，勾选还在，方便接着调
}
/* 批量引用表格：弹格式选择（带示例），选好直接出 CSV */
const citeOpen = ref(false)
const citeIds = ref([])
function doCite() {
  if (!selN.value) return
  citeIds.value = [...selSet.value]
  citeOpen.value = true
}
async function doDelete() {
  if (!selN.value) return
  const yes = await confirmBox({
    title: t('删除文献'), ok: t('删除'), danger: true,
    body: t('这 {n} 篇及其批注、析读、问答将一并删除。', { n: selN.value }),
  })
  if (!yes) return
  const pids = [...selSet.value]
  toggleSelMode()
  await _removePids(pids)
}
/* 批量归类：点分类名就整批归入并收起——不搞复选框三态（半选的横杠没人看得懂）；
   右侧的 ✓ 只是状态标记：这批已经全在这个分类里 */
function selCollState(cid) {
  const hits = [...selSet.value].filter(pid => collOf(pid).includes(cid)).length
  return hits === 0 ? 'none' : hits === selN.value ? 'all' : 'some'
}
async function selAddColl(cid) {
  for (const pid of selSet.value) {
    const cur = collOf(pid)
    if (cur.includes(cid)) continue
    try { await api.paperColls(pid, [...cur, cid]) } catch { selSet.value.delete(pid) }   // 一篇失败不拖垮整批；失败的多半已被删，勾选里摘掉
  }
  await refreshCollections()
  selMenu.value = false
  toast(t('已把 {n} 篇归入该分类', { n: selN.value }))
}
/* 「未分类」= 从所有分类里移出（它本来就是"不属于任何分类"的别名） */
async function selToUncategorized() {
  for (const pid of selSet.value) {
    if (!collOf(pid).length) continue
    try { await api.paperColls(pid, []) } catch { selSet.value.delete(pid) }
  }
  await refreshCollections()
  selMenu.value = false
  toast(t('已把 {n} 篇移出所有分类', { n: selN.value }))
}

/* ---------- 长按拖放区 = 从 Zotero 导入（点击仍是选文件） ---------- */
const zotArmed = ref(false)
let pressTimer = null
function hintDown(e) {
  if (e.button !== 0) return
  zotArmed.value = false
  pressTimer = setTimeout(() => {
    pressTimer = null
    zotArmed.value = true      // 长按已成：随后那次 click（松开鼠标）不再开文件框
    zotOpen.value = true
  }, 600)
}
function hintUp() {
  if (pressTimer) { clearTimeout(pressTimer); pressTimer = null }
}
function hintClick() {
  if (zotArmed.value) { zotArmed.value = false; return }
  fileInput.value?.click()      // ref 不会在函数里自动解包——普通点击导入就是这条路
}
function onZotImported() {
  refreshPapers()
  refreshCollections()
}
function onCmpGoto(c) {
  openPaper(c.pid).then(() => jumpPara(c.n))
}
</script>

<template>
  <div class="lib-panel" :style="{ '--lib-w': store.viewer.libW + 'px' }" @keydown.esc="emit('close')">
    <div class="rail-grip lib-grip"
         @mousedown="lib.start" @dblclick="lib.reset"></div>

    <div class="rail-head">
            <span class="mono-label">{{ t('文库') }}</span>
      <button class="ghost head-x" :title="t('收起文库')" @click="emit('close')">‹</button>
    </div>

    <div class="lib-tools">
      <input type="text" v-model="q" :placeholder="t('搜索标题或全文…')" class="lib-search" />
      <select v-model="sort" class="lib-sort">
        <option value="added">{{ t('最近导入') }}</option>
        <option value="read">{{ t('最近阅读') }}</option>
        <option value="year">{{ t('发表时间') }}</option>
        <option value="title">{{ t('标题') }}</option>
      </select>
      <button class="lib-multi" :class="{ on: selMode }" :title="t('多选')"
              @click="toggleSelMode">{{ t('多选') }}</button>
    </div>

        <div class="coll-list">
      <div class="coll-row" :class="{ on: SEL === 'all' }" @click="pickColl('all')">
        <span class="c-name">{{ t('全部') }}</span><b>{{ store.papers.length }}</b>
      </div>
      <div class="coll-row" :class="{ on: SEL === 'none' }" @click="pickColl('none')">
        <span class="c-name">{{ t('未分类') }}</span><b>{{ nUnfiled }}</b>
      </div>
      <div v-for="c in colls" :key="c.id" class="coll-row" :class="{ on: SEL === c.id }"
           @click="pickColl(c.id)" @dragover.prevent @drop.prevent="dropOn(c.id)"
           @dblclick.stop="startRename(c)">
        <span class="c-dot"></span>
        <input v-if="editId === c.id" class="coll-edit" v-model="editName" @click.stop
               @keydown.enter="doRename(c)" @keydown.esc="editId = null" @blur="doRename(c)" />
        <span v-else class="c-name" :title="c.name">{{ c.name }}</span>
        <b>{{ c.n }}</b>
        <button class="c-x" :title="t('删除分类')" @click.stop="delColl(c)">×</button>
      </div>
      <div v-if="adding" class="coll-row">
        <input class="coll-edit" v-model="newName" :placeholder="t('分类名')" autofocus
               @keydown.enter="createColl" @keydown.esc="adding = false; newName = ''" @blur="createColl" />
      </div>
      <button v-else class="coll-add" @click="adding = true">＋ {{ t('新建分类') }}</button>
    </div>

    <div class="paper-list">
      <TransitionGroup name="plist">
      <div v-for="p in shown" :key="p.id" class="paper-item" :class="{ on: p.id === store.currentId, sel: selSet.has(p.id) }"
           :draggable="!selMode" @dragstart="dragPid = p.id" @dragend="dragPid = null"
           @click="selMode ? toggleSel(p.id) : touch(p)">
        <i class="p-check" v-if="selMode" :class="{ on: selSet.has(p.id) }">
          <svg viewBox="0 0 12 12" width="10" height="10"><path d="M2 6.2 4.8 9 10 3.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </i>
        <button class="p-del" :title="t('删除')" @click.stop="del(p.id, p.title || p.filename)">×</button>
        <button class="p-tag" v-if="!selMode" :title="t('归入分类')"
                @click.stop="openMenu(p, $event)">+</button>
        <div class="fn" :title="p.title || p.filename">{{ p.title || p.filename }}</div>
        <div class="p-author" v-if="p.authors || p.year">{{ p.authors }}<template v-if="p.authors && p.year"> · </template>{{ p.year }}</div>
        <div class="p-hit" v-if="hitOf(p.id)" :title="`${t(WHERE_KEYS[hitOf(p.id).where])} · ${hitOf(p.id).n} 处\n${hitOf(p.id).snippet}`">
          <b>{{ t(WHERE_KEYS[hitOf(p.id).where]) }}</b>{{ hitOf(p.id).snippet }}
        </div>
                <div class="p-state" v-if="p.analysis_status === 'queued'">{{ t('排队通读中…') }}</div>
        <div class="p-state busy" v-else-if="p.analysis_status === 'running'">{{ t('正在通读…') }}</div>
        <div class="p-state" v-else-if="p.analysis_status === 'error'">{{ t('通读失败') }}</div>

      </div>
      </TransitionGroup>
      <Teleport to="body">
        <Transition name="pop">
        <div class="coll-menu" v-if="menuFor" @click.stop
             :style="{ left: menuXY.x + 'px', top: menuXY.y + 'px' }">
          <div class="cm-head">{{ t('归入分类') }}</div>
          <label v-for="c in colls" :key="c.id" class="cm-row">
            <input type="checkbox" :checked="menuPicked.has(c.id)" @change="flipPick(c.id)" />
            <span>{{ c.name }}</span>
          </label>
          <div v-if="!colls.length" class="cm-empty">{{ t('还没有分类') }}</div>
          <button class="cm-done" @click="menuDone">{{ t('完成') }}</button>
        </div>
        </Transition>
      </Teleport>
      <div v-if="!shown.length" class="p-empty">
        <template v-if="!store.papers.length">{{ t('文库是空的') }}</template>
        <template v-else-if="q.trim()">{{ t('没有匹配「{q}」的文献', { q: q.trim() }) }}</template>
        <template v-else-if="typeof SEL === 'number'">{{ t('这个分类还没有文献') }}</template>
        <template v-else-if="SEL === 'none'">{{ t('每一篇都归类了') }}</template>
        <template v-else>{{ t('没有符合条件的结果') }}</template>
      </div>
    </div>

    <Transition name="pop">
    <div class="sel-bar" v-if="selMode">
      <span class="s-n" :class="{ zero: !selN }">{{ t('已选 {n}', { n: selN }) }}</span>
      <button class="s-done" @click="toggleSelMode">{{ t('完成') }}</button>
      <span class="s-actions">
        <button :disabled="!canSplit" :title="canSplit ? '' : t('同屏要选 2~4 篇')" @click="doSplit">{{ t('同屏阅读') }}</button>
        <button :disabled="!canCompare" :title="canCompare ? '' : t('对比要选 2~5 篇')" @click="doCompare">{{ t('数据对比') }}</button>
        <button :disabled="!selN" @click="doCite">{{ t('引用') }}</button>
        <button :disabled="!selN" @click="selMenu = !selMenu">{{ t('分类') }}</button>
        <button :disabled="!selN" class="s-danger" @click="doDelete">{{ t('删除') }}</button>
      </span>
      <div class="coll-menu sel-coll" v-if="selMenu" @click.stop>
        <div class="cm-head">{{ t('归入分类 · {n} 篇', { n: selN }) }}</div>
        <button v-for="c in colls" :key="c.id" class="cm-row cm-act" @click="selAddColl(c.id)">
          <span class="cm-name">{{ c.name }}</span>
          <svg v-if="selCollState(c.id) === 'all'" viewBox="0 0 12 12" width="11" height="11"><path d="M2 6.2 4.8 9 10 3.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <button class="cm-row cm-act cm-uncat" @click="selToUncategorized">{{ t('未分类') }}</button>
        <div v-if="!colls.length" class="cm-empty">{{ t('还没有分类') }}</div>
      </div>
    </div>
    </Transition>
    <div class="drop-hint" :class="{ over, hot: zotArmed }" v-show="!selMode"
         @mousedown="hintDown" @mouseup="hintUp" @mouseleave="hintUp" @click="hintClick"
         @dragover.prevent="over = true" @dragleave="over = false" @drop.prevent="onDrop">
      {{ t('拖入 PDF 或点击导入') }}
    </div>
    <input ref="fileInput" type="file" accept="application/pdf" multiple hidden @change="onFile" />
    <ZoteroDialog v-model:open="zotOpen" @imported="onZotImported" />
    <CompareOverlay :open="cmpOpen" :ids="cmpIds" @close="cmpOpen = false" @goto="onCmpGoto" />
    <CiteExport v-model:open="citeOpen" :ids="citeIds" />
  </div>
</template>
