<script setup>
/* 文库：分类 + 搜索 + 排序（Zotero 的左栏那套，但只留真正用得上的）
 *
 * Zotero 的分类有两个要点值得抄：一篇文献可以同时属于多个分类（所以它像"标签"
 * 而不像文件夹）；分类不是唯一入口，随手能搜、能按最近读排序。其余（层级、保存的
 * 检索式、自动标签）在这个体量下都是负担，不做。
 */
import { computed, nextTick, ref } from 'vue'
import { api, store, toast, refreshPapers, refreshCollections, openPaper } from '../store'

const emit = defineEmits(['import', 'close'])
const fileInput = ref(null)
const over = ref(false)

/* ---------- 分栏宽度：和右栏一个手势 ---------- */
const LIB_MIN = 236, LIB_MAX = 560, LIB_DEF = 300
const libW = ref(JSON.parse(localStorage.getItem('eggpaper:libW') || 'null') ?? LIB_DEF)
let lStartX = 0, lStartW = 0
function startResize(e) {
  e.preventDefault()
  lStartX = e.clientX
  lStartW = libW.value
  document.body.classList.add('rail-resizing')
  document.addEventListener('mousemove', onResize)
  document.addEventListener('mouseup', endResize)
}
function onResize(e) {
  libW.value = Math.round(Math.min(LIB_MAX, Math.max(LIB_MIN, lStartW + (e.clientX - lStartX))))
}
function endResize() {
  document.body.classList.remove('rail-resizing')
  document.removeEventListener('mousemove', onResize)
  document.removeEventListener('mouseup', endResize)
  localStorage.setItem('eggpaper:libW', JSON.stringify(libW.value))
}

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
  if (kw) {
    list = list.filter(p => (p.title || p.filename || '').toLowerCase().includes(kw))
  }
  const s = store.lib.sort
  const title = p => (p.title || p.filename || '').toLowerCase()
  return [...list].sort((a, b) =>
    s === 'title' ? title(a).localeCompare(title(b))
    : s === 'read' ? String(b.last_read_at || b.created_at || '').localeCompare(String(a.last_read_at || a.created_at || ''))
    : String(b.created_at || '').localeCompare(String(a.created_at || '')))
})
const nUnfiled = computed(() => store.papers.filter(p => !collOf(p.id).length).length)

function pickColl(v) { store.lib.coll = v }

/* ---------- 分类的增改删 ---------- */
const adding = ref(false)
const newName = ref('')
const editId = ref(null)
const editName = ref('')
const menuFor = ref(null)        // 打开"归入分类"面板的那篇

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
  if (!confirm(`删掉分类「${c.name}」？（文献本身不会被删，只是不再归在这一类）`)) return
  try {
    await api.collDelete(c.id)
    if (store.lib.coll === c.id) store.lib.coll = 'all'
    await refreshCollections()
  } catch (e) { toast(e.message) }
}

/* ---------- 归入分类：勾选 + 拖拽两条路 ---------- */
// 行上只有一个"＋"，归没归进去不看行（那会变成一串看不懂的数字），
// 归的动作即时回一句 toast——反馈要给，但不留在界面上占地方
async function toggleIn(pid, cid) {
  const cur = collOf(pid)
  const c = colls.value.find(x => x.id === cid)
  const on = cur.includes(cid)
  const next = on ? cur.filter(x => x !== cid) : [...cur, cid]
  try {
    await api.paperColls(pid, next)
    await refreshCollections()
    if (c) toast(on ? `已移出「${c.name}」` : `已归入「${c.name}」`)
  } catch (e) { toast(e.message) }
}
const dragPid = ref(null)
async function dropOn(cid) {
  const pid = dragPid.value
  dragPid.value = null
  if (!pid) return
  const cur = collOf(pid)
  if (cur.includes(cid)) return
  const c = colls.value.find(x => x.id === cid)
  try {
    await api.paperColls(pid, [...cur, cid])
    await refreshCollections()
    if (c) toast(`已归入「${c.name}」`)
  } catch (e) { toast(e.message) }
  menuFor.value = null
}

/* ---------- 导入 / 删除 ---------- */
function onFile(e) {
  emit('import', e.target.files?.[0])
  e.target.value = ''
}
function onDrop(e) {
  over.value = false
  emit('import', e.dataTransfer?.files?.[0])
}
async function del(pid, name) {
  if (!confirm(`删除《${name.slice(0, 30)}…》及其全部批注？本地的骨架、眉批、问答会一起清掉。`)) return
  try {
    await api.deletePaper(pid)
  } catch (e) { toast('删除失败：' + e.message); return }
  localStorage.removeItem('eggpaper:pos:' + pid)     // 阅读位置也别留在浏览器里
  if (store.currentId === pid) {
    store.currentId = null
    store.paper = null
    store.paras = []
    store.analysis = { status: 'none', claims: [], annotations: {}, error: '' }
    store.marginalia = { status: 'none', notes: [] }
    store.summary = null
  }
  await refreshPapers()
  await refreshCollections()
  toast('已删除（本地数据一并清掉）')
}
function touch(p) { if (p.id !== store.currentId) openPaper(p.id) }
</script>

<template>
  <div class="lib-panel" :style="{ '--lib-w': libW + 'px' }" @keydown.esc="emit('close')">
    <div class="rail-grip lib-grip" title="拖动改宽度 · 双击复位"
         @mousedown="startResize" @dblclick="libW = LIB_DEF"></div>

    <div class="rail-head">
      <span class="mono-label">文库 · {{ store.papers.length }} 篇</span>
      <button class="ghost head-x" title="收起文库" @click="emit('close')">‹</button>
    </div>

    <div class="lib-tools">
      <input type="text" v-model="q" placeholder="搜标题 / 文件名…" class="lib-search" />
      <select v-model="sort" class="lib-sort" title="排序">
        <option value="added">最近导入</option>
        <option value="read">最近阅读</option>
        <option value="title">标题</option>
      </select>
    </div>

    <!-- 分类：一条"全部"、一条"未分类"，然后是用户建的 -->
    <div class="coll-list">
      <div class="coll-row" :class="{ on: SEL === 'all' }" @click="pickColl('all')">
        <span class="c-name">全部</span><b>{{ store.papers.length }}</b>
      </div>
      <div class="coll-row" :class="{ on: SEL === 'none' }" @click="pickColl('none')">
        <span class="c-name">未分类</span><b>{{ nUnfiled }}</b>
      </div>
      <div v-for="c in colls" :key="c.id" class="coll-row" :class="{ on: SEL === c.id }"
           @click="pickColl(c.id)" @dragover.prevent @drop.prevent="dropOn(c.id)"
           @dblclick.stop="startRename(c)">
        <span class="c-dot"></span>
        <input v-if="editId === c.id" class="coll-edit" v-model="editName" @click.stop
               @keydown.enter="doRename(c)" @keydown.esc="editId = null" @blur="doRename(c)" />
        <span v-else class="c-name" :title="c.name">{{ c.name }}</span>
        <b>{{ c.n }}</b>
        <button class="c-x" title="删掉这个分类" @click.stop="delColl(c)">×</button>
      </div>
      <div v-if="adding" class="coll-row">
        <input class="coll-edit" v-model="newName" placeholder="分类名" autofocus
               @keydown.enter="createColl" @keydown.esc="adding = false; newName = ''" @blur="createColl" />
      </div>
      <button v-else class="coll-add" @click="adding = true">＋ 新建分类</button>
    </div>

    <div class="paper-list">
      <div v-for="p in shown" :key="p.id" class="paper-item" :class="{ on: p.id === store.currentId }"
           draggable="true" @dragstart="dragPid = p.id" @dragend="dragPid = null" @click="touch(p)">
        <button class="p-del" title="删除" @click.stop="del(p.id, p.title || p.filename)">×</button>
        <button class="p-tag" title="归入分类"
                @click.stop="menuFor = menuFor === p.id ? null : p.id">＋</button>
        <div class="fn" :title="p.title || p.filename">{{ p.title || p.filename }}</div>
        <div class="p-author" v-if="p.authors">{{ p.authors }}</div>

        <!-- 归入分类：勾选即存，不用"确定" -->
        <div class="coll-menu" v-if="menuFor === p.id" @click.stop>
          <div class="cm-head">归入分类</div>
          <label v-for="c in colls" :key="c.id" class="cm-row">
            <input type="checkbox" :checked="collOf(p.id).includes(c.id)" @change="toggleIn(p.id, c.id)" />
            <span>{{ c.name }}</span>
          </label>
          <div v-if="!colls.length" class="cm-empty">还没有分类，先在上面新建一个</div>
          <button class="cm-done" @click="menuFor = null">完成</button>
        </div>
      </div>
      <div v-if="!shown.length" class="p-empty">
        <template v-if="!store.papers.length">文库是空的。拖一份 PDF 进来就开始。</template>
        <template v-else-if="q.trim()">没有匹配「{{ q.trim() }}」的文献。</template>
        <template v-else-if="typeof SEL === 'number'">
          这个分类下还没有文献。切到「全部」，把条目拖到左边的「{{ colls.find(c => c.id === SEL)?.name }}」，或者点条目左边的 ＋ 勾选。
        </template>
        <template v-else-if="SEL === 'none'">每一篇都归类了。</template>
        <template v-else>没有符合条件的文献。</template>
      </div>
    </div>

    <div class="drop-hint" :class="{ over }" @click="fileInput.click()"
         @dragover.prevent="over = true" @dragleave="over = false" @drop.prevent="onDrop">
      拖入 PDF · 或点击导入
    </div>
    <input ref="fileInput" type="file" accept="application/pdf" hidden @change="onFile" />
  </div>
</template>
