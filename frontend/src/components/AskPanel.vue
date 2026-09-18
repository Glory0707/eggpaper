<script setup>
/* 提问面板：按 DeepSeek / 豆包网页版的对话来做的。
 *
 * 那几个产品的共同点（也是它们好用的全部原因）：
 *   ① 回答逐字吐出来，中途能停——等 20 秒才砸一大段是没人愿意用的；
 *   ② 一摊对话一个上下文，"新对话"是一等公民，不是"清空历史"；
 *   ③ 每条回答底下有一排动作：复制、重新生成、删除；
 *   ④ 输入框能长高，Enter 发送、Shift+Enter 换行（中文输入法按 Enter 选字不能误发）。
 * 这些都不加文案、不加颜色，符合本子自己的调子。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, askStream, store, toast, jumpTo, paraByIdx } from '../store'
import { confirmBox, inputBox } from '../dialog'
import MdLite from './MdLite.vue'

const props = defineProps({ quick: { type: Array, default: () => [] } })

const pid = computed(() => store.currentId)

function jumpPara(idx) {
  const p = paraByIdx.value[idx]
  if (p) jumpTo(p.page, p.bbox.y0, p.bbox.y1)
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

// ---------- 载入 ----------
async function loadConvs(keep = false) {
  if (!pid.value) return
  try {
    convs.value = await api.conversations(pid.value)
  } catch { return }
  if (!keep && !convs.value.some(c => c.id === convId.value)) convId.value = convs.value[0]?.id ?? null
}
async function loadMsgs() {
  if (!pid.value || !convId.value) { msgs.value = []; return }
  // 屏幕上的 msgs 只能有一个作者：要么流式在写，要么历史在写。生成中载入历史会把刚推
  // 上去的那问一答换掉，而回答还在往一个不在列表里的对象里吐（症状：发了没反应）。
  if (busy.value) return
  loading.value = true
  try {
    const r = await api.qaHistory(pid.value, convId.value)
    msgs.value = r.messages || []
  } catch { msgs.value = [] }
  loading.value = false
  await nextTick()
  scrollBottom(false)
}
// 换会话 = 放弃这一摊正在跑的回答：先停下来（半截由 send 的尾巴落库），再读新历史
async function reload() { stop(true); await loadConvs(); await loadMsgs() }

watch(() => store.currentId, async () => { convId.value = null; await reload() })
watch(convId, (n, o) => { if (n !== o) { stop(true); loadMsgs() } })

// ---------- 滚动 ----------
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
// 流式输出时只在用户本来就在底部时跟着走；用户翻上去看别处，就别抢他的滚动条
function follow() { if (atBottom.value) { const el = scrollEl.value; if (el) el.scrollTop = el.scrollHeight } }

// ---------- 发送 / 停止 ----------
async function send(q) {
  q = (q ?? text.value).trim()
  if (!q || busy.value || !pid.value) return
  // 会话还没载入完就问（预填抢跑）：先把会话取回来，别让服务端替我们猜是哪一摊。
  // 猜错的后果是问题落进另一摊对话，而这个面板显示的又不是那一摊。
  if (!convId.value) await loadConvs()
  text.value = ''
  await nextTick(); autoGrow(); inputEl.value?.focus()
  const um = reactive({ role: 'user', content: q })
  msgs.value.push(um)
  const m = reactive({ role: 'assistant', content: '', citations: [], streaming: true, error: '' })
  msgs.value.push(m)
  busy.value = true
  gotDone = false
  await nextTick(); scrollBottom(false)
  const id = convId.value
  // 服务端把落库的 id 顺着流送回来，挂到本地这两条上——
  // 不然"删除这条"只能删掉屏幕上的，刷新一下它又回来了
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
  const h = askStream(pid.value, body, ev => {
    if (ev.type === 'delta') queue(ev.text)
    else if (ev.type === 'done') { flush(); gotDone = true; m.citations = ev.citations || []; applyIds(ev) }
    else if (ev.type === 'error') {
      // 出错也把已经吐出来的留着，末尾接一行提示——和服务端存下来的内容保持一致
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
  // 中途停下的那半截由前端存：服务端在客户端断开时不保证还能把生成器走完
  if (!gotDone && m.content.trim() && !m.error) {
    api.qaSave(pid.value, { conv_id: id, content: m.content })
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

async function regen(i) {
  if (busy.value) return
  try {
    const r = await api.qaRegenerate(pid.value, convId.value)
    // 服务端撤掉了这一问一答，本地同步弹出，再重问一遍
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
  try {
    await navigator.clipboard.writeText(m.content || '')
    toast('已复制')
  } catch { toast('复制失败，手动选吧') }
}

// ---------- 会话 ----------
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
  const t = await inputBox({ title: '重命名会话', value: c.title, placeholder: '会话名称', ok: '改名' })
  if (t == null) return
  try { await api.convRename(c.id, t.trim() || '新对话'); await loadConvs(true) } catch (e) { toast(e.message) }
}
async function delConv() {
  const c = curConv.value
  if (!c) return
  const yes = await confirmBox({
    title: '删除会话', danger: true, ok: '删除',
    body: `「${c.title}」里的问答会一起删掉，论文与批注不受影响。`,
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

// ---------- 跨文献引用：/ 拉起选择器，勾选的篇挂在输入框上 ----------
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
const citeListEl = ref(null)
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
  // 「全部」打头，其余按分类；未分类的归进「未分类」
  const used = new Set()
  const gs = []
  for (const c of libColls.value) {
    const items = filtered.value.filter(p => (libMap.value[p.id] || []).includes(c.id))
    if (!items.length) continue
    items.forEach(p => used.add(p.id))
    gs.push({ name: c.name, items })
  }
  const rest = filtered.value.filter(p => !used.has(p.id))
  if (rest.length) gs.push({ name: '未分类', items: rest })
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
const citeTitle = computed(() => '已引用：' + (picked.value.slice(0, 5).join('、') + (picked.value.length > 5 ? '…' : '')))
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

// ---------- 输入框 ----------
function autoGrow() {
  const t = inputEl.value
  if (!t) return
  t.style.height = 'auto'
  t.style.height = Math.min(168, Math.max(30, t.scrollHeight)) + 'px'
}
function onKey(e) {
  // isComposing：中文输入法按 Enter 是在选字，不能当发送
  if (e.isComposing || e.keyCode === 229) return
  if (citeOpen.value) {
    if (e.key === 'Escape') { e.preventDefault(); closeCite() }
    else if (e.key === 'Enter') { e.preventDefault(); pickFirst() }
    return
  }
  // 输入框里按 / = 引用其他论文（和 Codex/ZCode 提 @ 一个肌肉记忆）
  if (e.key === '/') { e.preventDefault(); openCite(); return }
  if (e.key !== 'Enter') return
  if (e.shiftKey) return
  e.preventDefault()
  send()
}
watch(text, () => nextTick(autoGrow))

// ---------- 外部预填（划词提问 / ¶提问 / 快捷键 / 「问题」页签的「去问」） ----------
function applyPrefill(pf) {
  // 三种来源：给定问题原文（「问题」页签的"去问"）、给定段落号、给定一段原文
  text.value = pf.question
    || (pf.paraIdx ? `¶${pf.paraIdx} 这段在说什么？` : `这段在说什么：「${pf.text}」？`)
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
  const inChips = e.target && e.target.closest && e.target.closest('.cite-chips')
  if (pop && !pop.contains(e.target) && !inChips) closeCite()
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
    <!-- 会话栏：切换 / 新建 / 改名 / 删掉。文案只有动作，没有注解。
         三个图标是同一套线条 SVG（一个细 ＋、一个实心 ✎、一个彩色 emoji 🗑 混在一起
         看着像三个人画的），删除键悬停才转朱红 -->
    <div class="cv-bar">
      <select class="cv-pick" :value="convId ?? ''"
              @change="e => (convId = Number(e.target.value))">
        <option v-for="c in shownConvs" :key="c.id" :value="c.id">{{ c.title }}</option>
        <option v-if="!shownConvs.length" :value="''">新对话</option>
      </select>
      <button class="cv-btn" title="新建会话" @click="newConv">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round"><path d="M12 5.5v13M5.5 12h13" /></svg>
      </button>
      <button class="cv-btn" title="重命名" :disabled="!curConv" @click="renameConv">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M16.5 3.5a2.6 2.6 0 013.7 3.7L8 19.4l-4.6 1.1L4.5 16z" />
        </svg>
      </button>
      <button class="cv-btn danger" title="删除会话" :disabled="!curConv" @click="delConv">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M3.5 6.5h17M9 6.5V4.2h6v2.3M6.2 6.5l.9 13.3h9.8l.9-13.3" />
        </svg>
      </button>
    </div>

    <div class="qa-scroll" ref="scrollEl" @scroll.passive="onScroll">
      <!-- 较早的对话被压成摘要后，说一句"它还在"，点开能看 -->
      <div v-if="curConv?.summary" class="qa-fold" :title="curConv.summary">更早的对话已存为摘要</div>

      <div v-if="empty" class="qa-empty">
        <div class="qa-quick">
          <button v-for="q in props.quick" :key="q" :title="q" @click="send(q)">{{ q }}</button>
        </div>
      </div>

      <div v-for="(m, i) in msgs" :key="m.id || 'm' + i" class="qa-msg" :class="m.role">
        <div class="q-role">{{ m.role === 'user' ? '你' : 'EGGPAPER' }}</div>
        <template v-if="m.role === 'assistant'">
          <!-- 思考型模型要先想 30~60 秒才吐第一个字。这段空等不写出来的话，
               "正在想"和"发了没反应"在屏幕上长得一模一样。 -->
          <div v-if="m.streaming && !m.content" class="q-body md qa-wait">正在想<span class="r-dots">…</span></div>
          <MdLite v-else class="q-body md" :text="m.content || ' '" @cite="jumpPara" />
        </template>
        <div class="q-body" v-else>{{ m.content }}</div>
        <span v-if="m.streaming" class="qa-caret"></span>
        <!-- 这里原来还有一行「依据 ¶1 ¶5 ¶6…」。删了：它列的就是正文里那些已经可点的 ¶，
             一字不差地再说一遍（后端 cites_of 就是从答案正文里正则抓的）。 -->
        <div class="qa-acts" v-if="!m.streaming">
          <button @click="copy(m)">复制</button>
          <button v-if="m.role === 'assistant' && i === msgs.length - 1" @click="regen(i)"
                  :disabled="busy">重新生成</button>
          <button @click="delMsg(i)">删除</button>
        </div>
      </div>

      <Transition name="fade">
        <button class="qa-tobottom" v-if="!atBottom && msgs.length" @click="scrollBottom()">回到底部 ↓</button>
      </Transition>
    </div>

    <!-- 输入区：能长高，Enter 发送 / Shift+Enter 换行；/ 拉起跨文献引用 -->
    <div class="qa-input">
      <Transition name="fade">
        <div v-if="citeOpen" class="cite-pop">
          <input ref="citeFilterEl" v-model="citeQ" class="cite-filter" placeholder="筛选标题…（Esc 关闭，Enter 选第一个）" @keydown.esc.stop="closeCite">
          <div class="cite-list" ref="citeListEl">
            <button class="cite-item lib" :class="{ on: libAll }" @click="toggleLibAll">
              <span class="box">
                <svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="3.4"
                     stroke-linecap="round"><path d="M4.5 12.5l5 5.5 10-12" /></svg>
              </span>
              <span class="t">整个文库</span>
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
                <span class="tag" v-if="p.analysis_status === 'done'">已析读</span>
                <span class="tag" v-else>未析读</span>
              </button>
            </template>
            <div v-if="!groups.length" class="cite-group">没有匹配的论文</div>
          </div>
        </div>
      </Transition>
      <button v-if="citeCount" class="cite-inline" :title="citeTitle" @click="openCite">
        引用 {{ citeCount }}
      </button>
      <textarea ref="inputEl" v-model="text" rows="1" class="qa-ta"
                placeholder="基于这篇论文提问…（按 / 引用其他论文）"
                @keydown="onKey"></textarea>
      <button v-if="busy" class="qa-send stop" @click="stop()" title="停止生成">■</button>
      <button v-else class="primary qa-send" @click="send()" :disabled="!text.trim()" title="发送（Enter）">↑</button>
    </div>
  </div>
</template>
