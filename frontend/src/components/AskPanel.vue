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
import { api, askStream, store, toast, jumpTo } from '../store'
import MdLite from './MdLite.vue'

const props = defineProps({ quick: { type: Array, default: () => [] } })

const pid = computed(() => store.currentId)
const paraByIdx = computed(() => Object.fromEntries(store.paras.map(p => [p.idx, p])))

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
  loading.value = true
  try {
    const r = await api.qaHistory(pid.value, convId.value)
    msgs.value = r.messages || []
  } catch { msgs.value = [] }
  loading.value = false
  await nextTick()
  scrollBottom(false)
}
async function reload() { await loadConvs(); await loadMsgs() }

watch(() => store.currentId, async () => { stop(true); convId.value = null; await reload() })
watch(convId, (n, o) => { if (n !== o) loadMsgs() })

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
  const h = askStream(pid.value, { question: q, conv_id: id }, ev => {
    if (ev.type === 'delta') { m.content += ev.text; follow() }
    else if (ev.type === 'done') { gotDone = true; m.citations = ev.citations || []; applyIds(ev) }
    else if (ev.type === 'error') {
      // 出错也把已经吐出来的留着，末尾接一行提示——和服务端存下来的内容保持一致
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
  const t = prompt('这摊对话叫什么？', c.title)
  if (t == null) return
  try { await api.convRename(c.id, t.trim() || '新对话'); await loadConvs(true) } catch (e) { toast(e.message) }
}
async function delConv() {
  const c = curConv.value
  if (!c) return
  if (!confirm(`删掉这摊对话「${c.title}」？里面的问答会一起删掉。`)) return
  try {
    stop(true)
    await api.convDelete(c.id)
    convId.value = null
    await loadConvs()
    await loadMsgs()
  } catch (e) { toast(e.message) }
}

// ---------- 输入框 ----------
function autoGrow() {
  const t = inputEl.value
  if (!t) return
  t.style.height = 'auto'
  t.style.height = Math.min(168, Math.max(30, t.scrollHeight)) + 'px'
}
function onKey(e) {
  // isComposing：中文输入法按 Enter 是在选字，不能当发送
  if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return
  if (e.shiftKey) return
  e.preventDefault()
  send()
}
watch(text, () => nextTick(autoGrow))

// ---------- 外部预填（划词提问 / ¶提问 / 快捷键 /） ----------
function applyPrefill(pf) {
  text.value = pf.paraIdx ? `¶${pf.paraIdx} 这段在说什么？` : `这段在说什么：「${pf.text}」？`
  nextTick(() => { autoGrow(); inputEl.value?.focus() })
}
watch(() => store.askPrefill, pf => { if (pf) { applyPrefill(pf); store.askPrefill = null } })
watch(() => store.askFocusTick, () => nextTick(() => inputEl.value?.focus()))

onMounted(async () => {
  if (store.askPrefill) { applyPrefill(store.askPrefill); store.askPrefill = null }
  else nextTick(() => inputEl.value?.focus({ preventScroll: true }))
  await loadConvs()
  await loadMsgs()
})
onUnmounted(() => { stop(true) })
</script>

<template>
  <div class="ask-panel">
    <!-- 会话栏：切换 / 新建 / 改名 / 删掉 -->
    <div class="cv-bar">
      <select class="cv-pick" :value="convId ?? ''" title="切换对话"
              @change="e => (convId = Number(e.target.value))">
        <option v-for="c in convs" :key="c.id" :value="c.id">
          {{ c.title }}{{ c.n ? ` · ${c.n}` : '' }}
        </option>
        <option v-if="!convs.length" :value="''">新对话</option>
      </select>
      <button class="cv-btn" title="新对话（新开一摊，上下文不混）" @click="newConv">＋</button>
      <button class="cv-btn" title="重命名" @click="renameConv">✎</button>
      <button class="cv-btn" title="删掉这摊对话" :disabled="!curConv" @click="delConv">🗑</button>
    </div>

    <div class="qa-scroll" ref="scrollEl" @scroll.passive="onScroll">
      <div v-if="empty" class="qa-empty">
        <div class="qe-title">这一篇的问答</div>
        <div class="qe-sub">回答只依据这篇论文的原文，句尾的 ¶ 号可以点回原文。</div>
        <div class="qa-quick">
          <button v-for="q in props.quick" :key="q" :title="q" @click="send(q)">{{ q }}</button>
        </div>
      </div>

      <div v-for="(m, i) in msgs" :key="m.id || 'm' + i" class="qa-msg" :class="m.role">
        <div class="q-role">{{ m.role === 'user' ? '你' : 'EGGPAPER' }}</div>
        <MdLite v-if="m.role === 'assistant'" class="q-body md" :text="m.content || ' '"
                @cite="jumpPara" />
        <div class="q-body" v-else>{{ m.content }}</div>
        <span v-if="m.streaming" class="qa-caret"></span>
        <div class="qa-cites" v-if="m.citations?.length">
          <span class="qc-label">依据</span>
          <button v-for="c in m.citations" :key="c" @click="jumpPara(c)"
                  :title="paraByIdx[c] ? `跳到第 ${paraByIdx[c].page + 1} 页` : '原文没有这一段'">¶{{ c }}</button>
        </div>
        <div class="qa-acts" v-if="!m.streaming">
          <button @click="copy(m)" title="复制这条">复制</button>
          <button v-if="m.role === 'assistant' && i === msgs.length - 1" @click="regen(i)"
                  :disabled="busy" title="重新生成这条回答">重新生成</button>
          <button @click="delMsg(i)" title="删除这条">删除</button>
        </div>
      </div>

      <Transition name="fade">
        <button class="qa-tobottom" v-if="!atBottom && msgs.length" @click="scrollBottom()">回到底部 ↓</button>
      </Transition>
    </div>

    <!-- 输入区：能长高，Enter 发送 / Shift+Enter 换行 -->
    <div class="qa-input">
      <textarea ref="inputEl" v-model="text" rows="1" class="qa-ta"
                placeholder="基于这篇论文提问…（Enter 发送，Shift+Enter 换行）"
                @keydown="onKey"></textarea>
      <button v-if="busy" class="qa-send stop" @click="stop()" title="停止生成">■</button>
      <button v-else class="primary qa-send" @click="send()" :disabled="!text.trim()" title="发送（Enter）">↑</button>
    </div>
  </div>
</template>
