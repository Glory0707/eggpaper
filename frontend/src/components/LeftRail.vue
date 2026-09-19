<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api, store, toast, refreshPapers, refreshCollections, openPaper, goHome } from '../store'
import { confirmBox } from '../dialog'
import { t } from '../i18n'
import { lsGet, lsSet } from '../ls'
import { useEdgeResize } from '../edgeResize'

const emit = defineEmits(['import', 'close', 'split'])
const fileInput = ref(null)
const over = ref(false)

/* ---------- 分栏宽度：和右栏同一个手势（edgeResize.js） ---------- */
const LIB_MIN = 236, LIB_MAX = 560, LIB_DEF = 300
const libW = ref(lsGet('libW', null) ?? LIB_DEF)
const lib = useEdgeResize({
  get: () => libW.value, set: w => { libW.value = w },
  min: LIB_MIN, max: LIB_MAX, def: LIB_DEF, persist: w => lsSet('libW', w),
})

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
}
/* 菜单挂在 body 上（防列表裁剪），点外面/按 Esc 收起 */
watch(menuFor, (v, was) => {
  if (v && !was) {
    setTimeout(() => document.addEventListener('click', closeMenu, { capture: true }), 0)
    document.addEventListener('keydown', escMenu, { capture: true })
  } else if (!v) {
    document.removeEventListener('click', closeMenu, { capture: true })
    document.removeEventListener('keydown', escMenu, { capture: true })
  }
})
function closeMenu() { menuFor.value = null }
function escMenu(e) { if (e.key === 'Escape') closeMenu() }
onBeforeUnmount(() => {
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
    body: t('「{name}」里的文献不会被删，只是不再归在这一类。', { name: c.name }),
  })
  if (!yes) return
  try {
    await api.collDelete(c.id)
    if (store.lib.coll === c.id) store.lib.coll = 'all'
    await refreshCollections()
  } catch (e) { toast(e.message) }
}

/* ---------- 归入分类：勾选 + 拖拽两条路 ---------- */
async function toggleIn(pid, cid) {
  const cur = collOf(pid)
  const c = colls.value.find(x => x.id === cid)
  const on = cur.includes(cid)
  const next = on ? cur.filter(x => x !== cid) : [...cur, cid]
  try {
    await api.paperColls(pid, next)
    await refreshCollections()
    if (c) toast(on ? t('已移出「{name}」', { name: c.name }) : t('已归入「{name}」', { name: c.name }))
    menuFor.value = null
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
    if (c) toast(t('已归入「{name}」', { name: c.name }))
  } catch (e) { toast(e.message) }
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
async function del(pid, name) {
  const yes = await confirmBox({
    title: t('删除文献'), ok: t('删除'), danger: true,
    body: t('《{name}》以及它的批注、析读、问答会一起从本机删掉。', { name: name.slice(0, 40) }),
  })
  if (!yes) return
  try {
    await api.deletePaper(pid)
  } catch (e) { toast(t('删除失败：{m}', { m: e.message })); return }
  localStorage.removeItem('eggpaper:pos:' + pid)     // 阅读位置也别留在浏览器里
  store.openIds = store.openIds.filter(x => x !== pid)   // 同屏窗格里也摘掉这一篇
  if (store.currentId === pid) {
    const next = store.openIds[0]
    if (next) {
      await store.activatePaper(next, false)             // 同屏还有别的篇：切过去
    } else {
      goHome()          // 清场走 store 的正主：epoch/summaryErr/lastPaper 一个不漏
    }
  }
  await refreshPapers()
  await refreshCollections()
}
function touch(p) { if (p.id !== store.currentId) openPaper(p.id) }
</script>

<template>
  <div class="lib-panel" :style="{ '--lib-w': libW + 'px' }" @keydown.esc="emit('close')">
    <div class="rail-grip lib-grip" :title="t('拖动改宽度 · 双击复位')"
         @mousedown="lib.start" @dblclick="lib.reset"></div>

    <div class="rail-head">
            <span class="mono-label">{{ t('文库') }}</span>
      <button class="ghost head-x" :title="t('收起文库')" @click="emit('close')">‹</button>
    </div>

    <div class="lib-tools">
      <input type="text" v-model="q" :placeholder="t('搜标题 / 文件名…')" class="lib-search" />
      <select v-model="sort" class="lib-sort">
        <option value="added">{{ t('最近导入') }}</option>
        <option value="read">{{ t('最近阅读') }}</option>
        <option value="title">{{ t('标题') }}</option>
      </select>
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
      <div v-for="p in shown" :key="p.id" class="paper-item" :class="{ on: p.id === store.currentId }"
           draggable="true" @dragstart="dragPid = p.id" @dragend="dragPid = null" @click="touch(p)">
        <button class="p-split" :title="t('加入同屏阅读（最多 4 篇）')" @click.stop="emit('split', p.id)">⧉</button>
        <button class="p-del" :title="t('删除')" @click.stop="del(p.id, p.title || p.filename)">×</button>
        <button class="p-tag" :title="t('归入分类')"
                @click.stop="openMenu(p, $event)">＋</button>
        <div class="fn" :title="p.title || p.filename">{{ p.title || p.filename }}</div>
        <div class="p-author" v-if="p.authors">{{ p.authors }}</div>
                <div class="p-state" v-if="p.analysis_status === 'queued'">{{ t('排队通读中…') }}</div>
        <div class="p-state busy" v-else-if="p.analysis_status === 'running'">{{ t('正在通读…') }}</div>
        <div class="p-state" v-else-if="p.analysis_status === 'error'">{{ t('通读失败，可重试') }}</div>

      </div>
      </TransitionGroup>
      <Teleport to="body">
        <Transition name="pop">
        <div class="coll-menu" v-if="menuFor" @click.stop
             :style="{ left: menuXY.x + 'px', top: menuXY.y + 'px' }">
          <div class="cm-head">{{ t('归入分类') }}</div>
          <label v-for="c in colls" :key="c.id" class="cm-row">
            <input type="checkbox" :checked="collOf(menuFor).includes(c.id)" @change="toggleIn(menuFor, c.id)" />
            <span>{{ c.name }}</span>
          </label>
          <div v-if="!colls.length" class="cm-empty">{{ t('还没有分类，先在上面新建一个') }}</div>
          <button class="cm-done" @click="menuFor = null">{{ t('完成') }}</button>
        </div>
        </Transition>
      </Teleport>
      <div v-if="!shown.length" class="p-empty">
        <template v-if="!store.papers.length">{{ t('文库是空的') }}</template>
        <template v-else-if="q.trim()">{{ t('没有匹配「{q}」的文献。', { q: q.trim() }) }}</template>
        <template v-else-if="typeof SEL === 'number'">{{ t('这个分类还没有文献。') }}</template>
        <template v-else-if="SEL === 'none'">{{ t('每一篇都归类了。') }}</template>
        <template v-else>{{ t('没有符合条件的文献。') }}</template>
      </div>
    </div>

    <div class="drop-hint" :class="{ over }" @click="fileInput.click()"
         @dragover.prevent="over = true" @dragleave="over = false" @drop.prevent="onDrop">
      {{ t('拖入 PDF 或点击导入') }}
    </div>
    <input ref="fileInput" type="file" accept="application/pdf" multiple hidden @change="onFile" />
  </div>
</template>
