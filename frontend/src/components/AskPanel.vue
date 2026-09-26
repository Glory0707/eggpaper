<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, askStream, store, toast, jumpPara, openPaper } from '../store'
import { confirmBox, inputBox } from '../dialog'
import { copyWithToast } from '../clip'
import { t } from '../i18n'
import MdLite from './MdLite.vue'
import CompareOverlay from './CompareOverlay.vue'

const props = defineProps({ quick: { type: Array, default: () => [] } })

const pid = computed(() => store.currentId)

/* 回答里的 ¶n 是本篇的直接跳；《别篇》¶n（模型按《标题》¶n 的口径写跨篇引用）
   先按标题在库里认出那篇——模型可能截断长标题，用双向包含兜住——
   认得出就打开它再跳段（位置记忆管回来），认不出就诚实降级成纯文本，不给一个点了没反应的按钮。 */
function matchPaper(title) {
  const t = String(title || '').trim().toLowerCase()
  if (!t) return null
  const ps = store.papers || []
  const ti = p => (p.title || '').trim().toLowerCase()
  return ps.find(p => ti(p) === t)
      || ps.find(p => { const x = ti(p); return x && (x.includes(t) || t.includes(x)) })
}
const citeOk = r => !r.ref || !!matchPaper(r.ref)
async function onCite(r) {
  if (!r || !r.ref) return jumpPara(r && r.n)
  const p = matchPaper(r.ref)
  if (!p) return
  if (p.id !== store.currentId) await openPaper(p.id)
  jumpPara(r.n)
}

const convs = ref([])
const convId = ref(null)
const msgs = ref([])
const text = ref('')
const busy = ref(false)          // 正在生成
const loading = ref(false)       // 正在拉历史
const inputEl = ref(null)
const scrollEl = ref(null)
const atBottom = ref(true)
let ctl = null                   // 当前流：{abort, done}
let gotDone = false

const curConv = computed(() => convs.value.find(c => c.id === convId.value) || null)
/* 选择器里只列"问过话的"会话 + 当前这一摊。
   点几下 ＋ 又没问的空会话都叫"新对话"，全挂在列表里既是噪音、又像是丢了内容；
   当前这一摊永远留着（刚点 ＋ 就是它，标题就是"新对话"）。 */
const shownConvs = computed(() => convs.value.filter(c => c.id === convId.value || c.n > 0))
const empty = computed(() => !msgs.value.length && !loading.value)

async function loadConvs(keep = false) {
  if (!pid.value) return
  try {
    convs.value = await api.conversations(pid.value)
  } catch { return }
  if (!keep && !convs.value.some(c => c.id === convId.value)) convId.value = convs.value[0]?.id ?? null
}
async function loadMsgs() {
  if (!pid.value || !convId.value) { msgs.value = []; return }
  if (busy.value) return
  const reqPid = pid.value
  const reqCid = convId.value
  loading.value = true
  try {
    const r = await api.qaHistory(reqPid, reqCid)
    // 等待期间换了篇/换了会话：旧历史不落到新地方
    if (pid.value !== reqPid || convId.value !== reqCid) return
    msgs.value = r.messages || []
  } catch { msgs.value = [] }
  loading.value = false
  await nextTick()
  scrollBottom(false)
}
async function reload() { stop(true); await loadConvs(); await loadMsgs() }

watch(() => store.currentId, async () => { convId.value = null; await reload() })
watch(convId, (n, o) => { if (n !== o) { stop(true); loadMsgs() } })

function onScroll() {
  const el = scrollEl.value
  if (!el) return
  atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 64
}
function scrollBottom(smooth = true) {
  const el = scrollEl.value
  if (!el) return
  el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  atBottom.value = true
}
function follow() { if (atBottom.value) { const el = scrollEl.value; if (el) el.scrollTop = el.scrollHeight } }

async function send(q) {
  q = (q ?? text.value).trim()
  if (!q || busy.value || !pid.value) return
  const reqPid = pid.value            // 钉住提问时的 pid：流式中途换篇，半截答案得存回原论文
  if (!convId.value) await loadConvs()
  text.value = ''
  await nextTick(); autoGrow(); inputEl.value?.focus()
  const um = reactive({ role: 'user', content: q })
  msgs.value.push(um)
  const m = reactive({ role: 'assistant', content: '', streaming: true, error: '' })
  msgs.value.push(m)
  busy.value = true
  gotDone = false
  await nextTick(); scrollBottom(false)
  const id = convId.value
  const applyIds = ev => {
    if (ev.user_id) um.id = ev.user_id
    if (ev.assistant_id) m.id = ev.assistant_id
  }
  /* 流式的增量按**帧**合并再落进 DOM。每个 token 写一次响应式状态，就等于每个 token
     重排一次整条 Markdown、再读一次 scrollHeight 决定跟不跟滚——短回答看不出来，
     长回答（几百字以上）就是肉眼可见的卡。合并成"一帧一次"之后它只是更快，字一个不少。 */
  let buf = '', raf = 0
  const flush = () => {
    raf = 0
    if (!buf) return
    m.content += buf
    buf = ''
    follow()
  }
  const queue = t => {
    buf += t
    if (!raf) raf = requestAnimationFrame(flush)
  }
  const body = picked.value.length
    ? { question: q, conv_id: id, refs: picked.value }
    : { question: q, conv_id: id }
  store.egg.nod++                      // 蛋注意到你在提问，歪头看一眼
  const h = askStream(pid.value, body, ev => {
    if (ev.type === 'delta') queue(ev.text)
    else if (ev.type === 'done') { flush(); gotDone = true; applyIds(ev) }
    else if (ev.type === 'error') {
      flush()
      applyIds(ev)
      m.content = (m.content.trim() ? m.content + '\n\n' : '') + '⚠ ' + ev.message
      m.error = ev.message
    }
  })
  ctl = h
  try {
    await h.done
  } catch (e) {
    if (e.name !== 'AbortError') {
      m.content = (m.content.trim() ? m.content + '\n\n' : '') + '⚠ ' + e.message
      m.error = e.message
    }
  }
  flush()                       // 停在中途时，最后不满一帧的那几个字也得留下
  ctl = null
  m.streaming = false
  busy.value = false
  if (!gotDone && m.content.trim() && !m.error) {
    api.qaSave(reqPid, { conv_id: id, content: m.content })
       .then(r => { if (r?.assistant_id) applyIds({ user_id: r.user_id, assistant_id: r.assistant_id }) })
       .catch(() => {})
  }
  loadConvs(true)      // 标题和更新时间会变
}
function stop(silent = false) {
  const m = msgs.value[msgs.value.length - 1]
  if (ctl) { ctl.abort(); ctl = null }
  if (m?.streaming) m.streaming = false
  busy.value = false
  if (!silent) loadConvs(true)
}

async function regen() {
  if (busy.value) return
  try {
    const r = await api.qaRegenerate(pid.value, convId.value)
    while (msgs.value.length && msgs.value[msgs.value.length - 1].role !== 'user') msgs.value.pop()
    msgs.value.pop()
    await nextTick()
    send(r.question)
  } catch (e) { toast(e.message) }
}
async function delMsg(i) {
  const m = msgs.value[i]
  if (!m) return
  if (m.id) { try { await api.qaDeleteOne(convId.value, m.id) } catch (e) { toast(e.message); return } }
  msgs.value.splice(i, 1)
}
async function copy(m) {
  await copyWithToast(m.content || '', t('已复制'))
}

async function newConv() {
  try {
    const r = await api.convNew(pid.value)
    await loadConvs(true)
    convId.value = r.id
    msgs.value = []
    await nextTick(); inputEl.value?.focus()
  } catch (e) { toast(e.message) }
}
async function renameConv() {
  const c = curConv.value
  if (!c) return
  const name = await inputBox({ title: t('重命名会话'), value: c.title, placeholder: t('会话名称'), ok: t('改名') })
  if (name == null) return
  try { await api.convRename(c.id, name.trim() || t('新对话')); await loadConvs(true) } catch (e) { toast(e.message) }
}
async function delConv() {
  const c = curConv.value
  if (!c) return
  const yes = await confirmBox({
    title: t('删除会话'), danger: true, ok: t('删除'),
    body: t('「{t}」的问答将删除，论文与批注不受影响。', { t: c.title }),
  })
  if (!yes) return
  try {
    stop(true)
    await api.convDelete(c.id)
    convId.value = null
    await loadConvs()
    await loadMsgs()
  } catch (e) { toast(e.message) }
}

/* 只带每篇的析读摘要（几百字），不搬全文——两篇全文就顶到上下文天花板了。
   范围两种：手选几篇；或「全库·自动挑相关」（后端先用标题清单侦察出最相关的 2~4 篇）。 */
const citeOpen = ref(false)
const citeQ = ref('')
const libPapers = ref([])
const libColls = ref([])
const libMap = ref({})
const picked = ref([])          // ['标题', …]——整个文库/分类全选/手选，都是这一份集合
const allTitles = computed(() => libPapers.value.map(p => (p.title || p.filename || '').trim()).filter(Boolean))
const libAll = computed(() => allTitles.value.length > 0 && allTitles.value.every(t => picked.value.includes(t)))
const citeFilterEl = ref(null)

async function loadLib() {
  try {
    const r = await api.libOverview()
    libPapers.value = r.papers || []
    libColls.value = r.colls || []
    libMap.value = r.map || {}
  } catch { /* 选择器数据拿不到就只影响跨篇，单篇照常 */ }
}
const filtered = computed(() => {
  const q = citeQ.value.trim().toLowerCase()
  return libPapers.value.filter(p => !q || (p.title || '').toLowerCase().includes(q) || (p.filename || '').toLowerCase().includes(q))
})
const groups = computed(() => {
  const used = new Set()
  const gs = []
  for (const c of libColls.value) {
    const items = filtered.value.filter(p => (libMap.value[p.id] || []).includes(c.id))
    if (!items.length) continue
    items.forEach(p => used.add(p.id))
    gs.push({ name: c.name, items })
  }
  const rest = filtered.value.filter(p => !used.has(p.id))
  if (rest.length) gs.push({ name: t('未分类'), items: rest })
  return gs
})
function isOn(t) { return picked.value.includes(t) }
function toggleTitle(t) {
  const i = picked.value.indexOf(t)
  if (i >= 0) picked.value.splice(i, 1)
  else picked.value.push(t)
}
function toggleLibAll() {
  picked.value = libAll.value ? [] : [...allTitles.value]
}
function groupTitles(g) { return g.items.map(p => (p.title || p.filename || '').trim()).filter(Boolean) }
function groupAll(g) { const ts = groupTitles(g); return ts.length > 0 && ts.every(t => picked.value.includes(t)) }
function toggleGroup(g) {
  const ts = groupTitles(g)
  const all = ts.every(t => picked.value.includes(t))
  if (all) picked.value = picked.value.filter(t => !ts.includes(t))
  else for (const t of ts) if (!picked.value.includes(t)) picked.value.push(t)
}
function pickFirst() {
  const first = groups.value[0]?.items?.[0]
  if (first) toggleTitle(first.title || first.filename)
}
const citeCount = computed(() => picked.value.length)
const citeTitle = computed(() => t('已引用：') + picked.value.slice(0, 5).join(', ') + (picked.value.length > 5 ? '…' : ''))
/* 引用了别的论文时，一键把「当前篇 + 引用篇」送进数据对比（CompareOverlay）——
   对照表只有这一个家：六维度可选、逐格带 ¶ 锚点。这里不给它再造第二张表。 */
const cmpIds = computed(() => {
  const ids = [pid.value]
  for (const t of picked.value) {
    const p = matchPaper(t)
    if (p && p.id !== pid.value && !ids.includes(p.id)) ids.push(p.id)
  }
  return ids.slice(0, 5)
})
const cmpOpen = ref(false)
function openCompare() {
  if (cmpIds.value.length >= 2) cmpOpen.value = true
}
function onCmpGoto(c) {
  openPaper(c.pid).then(() => jumpPara(c.n))
}
function pickFromPop(p) {
  toggleTitle((p.title || p.filename || '').trim())
  nextTick(() => citeFilterEl.value?.focus())
}
function openCite() {
  citeQ.value = ''
  citeOpen.value = true
  loadLib()
  nextTick(() => citeFilterEl.value?.focus())
}
function closeCite() { citeOpen.value = false; nextTick(() => inputEl.value?.focus()) }

function autoGrow() {
  const t = inputEl.value
  if (!t) return
  t.style.height = 'auto'
  t.style.height = Math.min(168, Math.max(30, t.scrollHeight)) + 'px'
}
function onKey(e) {
  if (e.isComposing || e.keyCode === 229) return
  if (citeOpen.value) {
    if (e.key === 'Escape') { e.preventDefault(); closeCite() }
    else if (e.key === 'Enter') { e.preventDefault(); pickFirst() }
    return
  }
  if (e.key === '/') { e.preventDefault(); openCite(); return }
  if (e.key !== 'Enter') return
  if (e.shiftKey) return
  e.preventDefault()
  send()
}
watch(text, () => nextTick(autoGrow))

function applyPrefill(pf) {
  text.value = pf.question
    || (pf.paraIdx ? t('¶{n} 这段在说什么？', { n: pf.paraIdx }) : t('这段在说什么：「{t}」？', { t: pf.text }))
  nextTick(() => { autoGrow(); inputEl.value?.focus() })
  if (pf.send) nextTick(() => send())      // 「去问」= 直接问出去，别再让人按一次回车
}
/* 首次载入（会话列表 + 历史）没走完之前，预填一律先存着。
   抢跑的后果是实测过的：send() 拿不到会话号 → 服务端按"最近一摊"落库 →
   会话号随后被 loadConvs 补上 → 触发 loadMsgs → 刚推上去的那问一答被历史整个换掉，
   屏幕上只剩旧消息，而回答正往一个已经不在列表里的对象里吐。 */
let ready = false
let pending = null
function takePrefill(pf) {
  if (!ready) { pending = pf; return }
  applyPrefill(pf)
}
watch(() => store.askPrefill, pf => { if (pf) { takePrefill(pf); store.askPrefill = null } })
watch(() => store.askFocusTick, () => nextTick(() => inputEl.value?.focus({ preventScroll: true })))

function onDocKey(e) {
  if (e.key === 'Escape' && citeOpen.value) { e.preventDefault(); closeCite() }
}
function onDocPointer(e) {
  if (!citeOpen.value) return
  const pop = document.querySelector('.cite-pop')
  if (pop && !pop.contains(e.target)) closeCite()
}
onMounted(async () => {
  document.addEventListener('keydown', onDocKey)
  document.addEventListener('pointerdown', onDocPointer)
  if (store.askPrefill) { pending = store.askPrefill; store.askPrefill = null }
  await loadConvs()
  await loadMsgs()
  ready = true
  if (pending) { const pf = pending; pending = null; await nextTick(); applyPrefill(pf) }
  else nextTick(() => inputEl.value?.focus({ preventScroll: true }))
})
onUnmounted(() => { stop(true); document.removeEventListener('keydown', onDocKey); document.removeEventListener('pointerdown', onDocPointer) })
</script>

<template>
  <div class="ask-panel">
        <div class="cv-bar">
      <select class="cv-pick" :value="convId ?? ''"
              @change="e => (convId = Number(e.target.value) || null)">
        <!-- || null：空会话的占位 option 值是 ''，Number('') 是 0——falsy 但不是
             null，send 会拿 0 当 conv_id 发出去，后端建出一条没主的会话 -->
        <option v-for="c in shownConvs" :key="c.id" :value="c.id">{{ c.title }}</option>
        <option v-if="!shownConvs.length" :value="''">{{ t('新对话') }}</option>
      </select>
      <button class="cv-btn" :title="t('新建会话')" @click="newConv">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round"><path d="M12 5.5v13M5.5 12h13" /></svg>
      </button>
      <button class="cv-btn" :title="t('重命名')" :disabled="!curConv" @click="renameConv">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M16.5 3.5a2.6 2.6 0 013.7 3.7L8 19.4l-4.6 1.1L4.5 16z" />
        </svg>
      </button>
      <button class="cv-btn danger" :title="t('删除会话')" :disabled="!curConv" @click="delConv">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M3.5 6.5h17M9 6.5V4.2h6v2.3M6.2 6.5l.9 13.3h9.8l.9-13.3" />
        </svg>
      </button>
    </div>

    <div class="qa-scroll" ref="scrollEl" @scroll.passive="onScroll">
            <div v-if="curConv?.summary" class="qa-fold" :title="curConv.summary">{{ t('更早的对话已存为摘要') }}</div>

      <div v-if="empty" class="qa-empty">
        <div class="qa-quick">
          <button v-for="q in props.quick" :key="q" :title="q" @click="send(q)">{{ t(q) }}</button>
        </div>
      </div>

      <div v-for="(m, i) in msgs" :key="m.id || 'm' + i" class="qa-msg" :class="m.role">
        <div class="q-role">{{ m.role === 'user' ? t('你') : 'EGGPAPER' }}</div>
        <template v-if="m.role === 'assistant'">
                    <div v-if="m.streaming && !m.content" class="q-body md qa-wait">{{ t('正在想…') }}</div>
          <MdLite v-else class="q-body md" :text="m.content || ' '" :cite-ok="citeOk" @cite="onCite" />
        </template>
        <div class="q-body" v-else>{{ m.content }}</div>
        <span v-if="m.streaming" class="qa-caret"></span>
                <div class="qa-acts" v-if="!m.streaming">
          <button @click="copy(m)">{{ t('复制') }}</button>
          <button v-if="m.role === 'assistant' && i === msgs.length - 1" @click="regen()"
                  :disabled="busy">{{ t('重新生成') }}</button>
          <button @click="delMsg(i)">{{ t('删除') }}</button>
        </div>
      </div>

      <Transition name="fade">
        <button class="qa-tobottom" v-if="!atBottom && msgs.length" @click="scrollBottom()">{{ t('回到底部 ↓') }}</button>
      </Transition>
    </div>

        <div class="qa-input">
      <Transition name="fade">
        <div v-if="citeOpen" class="cite-pop">
          <input ref="citeFilterEl" v-model="citeQ" class="cite-filter" :placeholder="t('筛选标题…')" @keydown.esc.stop="closeCite">
          <div class="cite-list">
            <button class="cite-item lib" :class="{ on: libAll }" @click="toggleLibAll">
              <span class="box">
                <svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="3.4"
                     stroke-linecap="round"><path d="M4.5 12.5l5 5.5 10-12" /></svg>
              </span>
              <span class="t">{{ t('整个文库') }}</span>
            </button>
            <template v-for="g in groups" :key="g.name">
              <button class="cite-item ga" :class="{ on: groupAll(g) }" @click="toggleGroup(g)">
                <span class="box">
                  <svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="3.4"
                       stroke-linecap="round"><path d="M4.5 12.5l5 5.5 10-12" /></svg>
                </span>
                <span class="t">{{ g.name }}</span>
              </button>
              <button v-for="p in g.items" :key="p.id" class="cite-item" :class="{ on: isOn(p.title) }"
                      @click="pickFromPop(p)">
                <span class="box">
                  <svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="3.4"
                       stroke-linecap="round"><path d="M4.5 12.5l5 5.5 10-12" /></svg>
                </span>
                <span class="t">{{ p.title || p.filename }}</span>
                <span class="tag" v-if="p.analysis_status === 'done'">{{ t('已析读') }}</span>
                <span class="tag" v-else>{{ t('未析读') }}</span>
              </button>
            </template>
            <div v-if="!groups.length" class="cite-group">{{ t('没有匹配的论文') }}</div>
          </div>
        </div>
      </Transition>
      <button v-if="citeCount" class="cite-inline" :title="citeTitle" @click="openCite">
        {{ t('引用 {n}', { n: citeCount }) }}
      </button>
      <button v-if="citeCount && !busy && cmpIds.length >= 2" class="cite-inline"
              @click="openCompare">
        {{ t('数据对比') }}
      </button>
      <textarea ref="inputEl" v-model="text" rows="1" class="qa-ta"
                :placeholder="t('基于这篇论文提问…')"
                @keydown="onKey"></textarea>
      <button v-if="busy" class="qa-send stop" @click="stop()" :title="t('停止生成')">■</button>
      <button v-else class="primary qa-send" @click="send()" :disabled="!text.trim()" :title="t('发送（Enter）')">↑</button>
    </div>
    <CompareOverlay :open="cmpOpen" :ids="cmpIds" @close="cmpOpen = false" @goto="onCmpGoto" />
  </div>
</template>
