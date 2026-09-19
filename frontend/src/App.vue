<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { goHome, lsGet, api, store, toast, refreshPapers, refreshCollections, openPaper, refreshAnalysis, refreshMarginalia, reloadSummary, checkUpdate, loadVersion } from './store'
import PdfViewer from './components/PdfViewer.vue'
import LibPanel from './components/LeftRail.vue'
import CalendarPanel from './components/CalendarPanel.vue'
import TOCPanel from './components/TOCPanel.vue'
import RightRail from './components/RightRail.vue'
import SettingsModal from './components/SettingsModal.vue'
import Dialog from './components/Dialog.vue'
import CiteCard from './components/CiteCard.vue'
import UpdateCard from './components/UpdateCard.vue'
import { dlg, dlgCancel } from './dialog'
import EggMark from './components/EggMark.vue'
import { t, isEn, setLang } from './i18n'

const showSettings = ref(false)
const dragOver = ref(false)
const gPending = ref(false)
const VARIANTS = ['original', 'mono', 'dual']
let pollTimer = null
let marginFastTimer = null     // 眉批运行时那条 1 秒快轮询（跑完即停）
let _lastErrToast = { msg: '', at: 0 }   // 全局兜底报错的限流记号

/* ---- 蛋宠物：顶栏一枚、书桌空态一枚大的，同一套脾气的两个实例。
   戳一下晃一下；3 秒内戳满三下翻滚一圈；光标压在蛋上滚滚轮，它跟着转，停手一拍后
   带弹性摆回正（转角折进 ±180°——回正永远走最近那半圈）；把 PDF 精准丢在蛋身上，
   论文缩成小纸片翻着跟头飞进蛋里、鼓一下咽下——吃只是仪式，导入照走 onImport。
   活物有破绽：24 下里有 1 下不是常规晃动，是打喷嚏或打趔趄——不预告、不收集，
   只能被撞见。打盹/深夜/干活/庆祝是共享的时辰，两只蛋一起受；戳、搓、馋、咽各归各。
   状态优先级（styles.css 的定义顺序决定谁盖谁）：
   hungry < busy(含字条流过) < doze < wobble < 喷嚏/趔趄 < sleep < gulp/stuff < roll < cheer；
   sleep-poke 专盖 sleep；spin 是行内 transform，只在没有任何动画类时可见。
   JS 侧守卫：睡着不馋/不搓/不庆祝，被戳只抖不醒；干活时不搓；搓着时不接戳。 */
function makePet() {
  const pet = reactive({
    el: null,
    wobbling: false, surprise: '', sleepPoke: false, roll: false,
    spinDeg: 0, spinning: false, spinFree: false, hungry: false, gulping: '',
  })
  const timers = {}
  let pokes = 0, pokeReset = 0, spinTimer = 0
  function flash(key, ms, val = true) {
    const off = typeof val === 'string' ? '' : false
    pet[key] = off
    requestAnimationFrame(() => { pet[key] = val })
    clearTimeout(timers[key])
    timers[key] = setTimeout(() => (pet[key] = off), ms)
  }
  function poke() {
    if (pet.spinning) return            // 搓着的时候不接戳：一只手只做一件事
    pokes++
    clearTimeout(pokeReset)
    if (pokes >= 3) { pokes = 0; flash('roll', 700); return }
    pokeReset = setTimeout(() => (pokes = 0), 3000)
    if (sleepEgg.value) { flash('sleepPoke', 650); return }
    if (Math.random() < 1 / 24) {
      const kind = Math.random() < 0.5 ? 'sneeze' : 'stumble'
      flash('surprise', kind === 'sneeze' ? 650 : 1000, kind)
      return
    }
    flash('wobbling', 450)
  }
  function wheel(e) {
    if (e.ctrlKey) return               // Ctrl+滚轮是页面缩放，不抢浏览器的
    if (sleepEgg.value || eggBusy.value || pet.hungry || pet.gulping) return
    e.preventDefault()
    pet.spinFree = false
    pet.spinning = true
    pet.spinDeg = ((pet.spinDeg + e.deltaY * 0.18 + 180) % 360 + 360) % 360 - 180
    clearTimeout(spinTimer)
    spinTimer = setTimeout(() => {
      pet.spinFree = true
      pet.spinDeg = 0
      setTimeout(() => { pet.spinning = pet.spinFree = false }, 700)
    }, 160)
  }
  function enter(e) { if (hasFiles(e)) pet.hungry = true }
  function over(e) { if (hasFiles(e)) e.preventDefault() }
  function leave(e) {
    if (hasFiles(e) && !e.currentTarget.contains(e.relatedTarget)) pet.hungry = false
  }
  function drop(e) {
    if (!hasFiles(e)) return            // 拖文字之类的不归它管，照走默认
    e.preventDefault()
    e.stopPropagation()                 // 别再冒给 .app 的 onDrop——一次导入只做一遍
    pet.hungry = false
    dragOver.value = false              // .app 的 onDrop 收不到这一拍了，浮层自己收
    const files = Array.from(e.dataTransfer?.files || []).filter(f => f && f.name)
    if (!files.length) return
    feed(e.clientX, e.clientY, files.length)
    onImport(files)
  }
  function feed(x, y, n) {
    if (sleepEgg.value) return          // 它睡着了：饭照收（导入照跑），仪式免了
    const r = pet.el?.getBoundingClientRect?.()
    if (!r) return
    ghosts.value = Array.from({ length: Math.min(n, 4) }, (_, i) => ({
      id: ++ghostId,
      x: x + (i ? Math.random() * 36 - 18 : 0),
      y: y + (i ? Math.random() * 24 - 12 : 0),
      tx: r.left + r.width / 2, ty: r.top + r.height / 2,
      delay: i * 90,
    }))
    clearTimeout(timers.feed)
    timers.feed = setTimeout(() => {
      ghosts.value = []
      flash('gulping', n >= 3 ? 1350 : 850, n >= 3 ? 'stuff' : 'gulp')
    }, 420 + (Math.min(n, 4) - 1) * 90)
  }
  return Object.assign(pet, {
    poke, wheel, enter, over, leave, drop,
    rollOnce: () => flash('roll', 700),
    wobbleOnce: () => flash('wobbling', 450),
  })
}
const ghosts = ref([])           // 飞行途中的纸片（两只蛋共用一条渲染通道）
let ghostId = 0
const topPet = makePet()         // 顶栏那枚；析读完成/整本译完的动作滚跳也归它
const deskPet = makePet()        // 书桌空态那枚大的
function petCls(p, extra) {
  return [{ hungry: p.hungry }, { roll: p.roll }, { wobble: p.wobbling }, p.surprise,
    { sleep: sleepEgg.value }, { 'sleep-poke': p.sleepPoke }, { busy: eggBusy.value }, p.gulping,
    { cheer: eggCheer.value }, { doze: dozing.value }, { spun: p.spinning }, { free: p.spinFree },
    ...(extra ? [extra] : [])]
}
function spinStyle(p) {
  return p.spinning ? { transform: `rotate(${p.spinDeg}deg) scale(${p.spinFree ? 1 : 0.94})` } : null
}

/* 干活与庆祝：整本翻译跑着的时候，蛋轻轻晃着埋头干（eggBusy；深夜它睡了，睡觉优先）。
   这一篇译完跳两下（cheerEgg，pollTranslate 的完成拍调用）——整本书翻完比析读完
   更有分量，跳两下；析读完成仍是滚一圈。轮询只看当前论文，人不在场就不庆祝。 */
const eggBusy = computed(() => tranSt.value === 'running' && !sleepEgg.value)
const eggCheer = ref(false)
function cheerEgg() {
  if (sleepEgg.value) return          // 深夜它睡着干的活，不吵醒它庆祝
  eggCheer.value = true
  clearTimeout(cheerEgg._t)
  cheerEgg._t = setTimeout(() => (eggCheer.value = false), 1250)
}

/* 发呆打盹：3 分钟没有键鼠/滚轮的动静，蛋歪着头打盹（没有 z——z 是深夜专属）；
   一动就惊醒（晃一下）。翻页、滚动都算「动」——真正一动不动盯着一页读时才打盹。
   深夜它另有躺平睡觉、翻译时它在埋头干活，这两种状态不打盹。 */
const dozing = ref(false)
const IDLE_MS = 180000
let idleTimer = 0
function napCheck() {
  if (sleepEgg.value || tranSt.value === 'running') return
  dozing.value = true
}
function wakeEgg() {
  clearTimeout(idleTimer)
  idleTimer = setTimeout(napCheck, IDLE_MS)
  if (dozing.value) {
    dozing.value = false
    topPet.wobbleOnce(); deskPet.wobbleOnce()
  }
}

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
  onImport(Array.from(e.dataTransfer?.files || []))     // 一次拖进来的全收下
}
function endDrag() {
  dragOver.value = false        // 中途在窗口外松手也得收：否则浮层/两只蛋一直馋着
  topPet.hungry = false
  deskPet.hungry = false
}

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('dragend', endDrag)
  window.addEventListener('blur', endDrag)
  window.addEventListener('unhandledrejection', ev => {
    const msg = String(ev.reason?.message || ev.reason || t('未知错误'))
    const now = Date.now()
    if (msg === _lastErrToast.msg && now - _lastErrToast.at < 30000) return
    _lastErrToast = { msg, at: now }
    console.error('[eggpaper] 未处理的失败：', ev.reason)
    toast(t('出错了：{m}', { m: msg.slice(0, 120) }), 5000)
  })
  pollTimer = setInterval(poll, 3000)
  sleepGreet()               // 深夜开着 eggpaper：蛋先睡下，问候随后
  window.addEventListener('pointermove', wakeEgg, { passive: true })
  window.addEventListener('pointerdown', wakeEgg, { passive: true })
  window.addEventListener('keydown', wakeEgg)
  window.addEventListener('wheel', wakeEgg, { passive: true })
  wakeEgg()                  // 先把打盹的表立起来
  try {
    store.settings = await api.settings()
    if (store.settings?.ui_lang) setLang(store.settings.ui_lang)
    await refreshPapers()
    await refreshCollections()
    const lastId = lsGet('lastPaper', '')
    const last = store.papers.find(p => p.id === lastId)
    if (last) openPaper(last.id)
  } catch (e) {
    toast(t('初始化失败：{m}', { m: e.message }), 6000)
  }
  loadVersion().then(() => {
    bootVersion = store.update.current
    if (store.settings?.update?.auto_check !== false) {
      setTimeout(() => checkUpdate(false, true), 6000)
    }
  }).catch(() => {})
})
onUnmounted(() => {
  clearInterval(pollTimer)
  clearTimeout(idleTimer)
  clearTimeout(spinTimer)
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('dragend', endDrag)
  window.removeEventListener('blur', endDrag)
  window.removeEventListener('pointermove', wakeEgg)
  window.removeEventListener('pointerdown', wakeEgg)
  window.removeEventListener('keydown', wakeEgg)
  window.removeEventListener('wheel', wakeEgg)
})

/* 升级自检：这个标签页是哪个版本的界面。静默升级后旧标签页还活着、跑的还是旧 JS，
   而版本号是实时查后端的——于是出现最迷惑人的那种现象：**设置里的版本号更新了，
   别的修改一点没生效**。刷新是无损的（阅读位置存在本地），所以直接替他刷。 */
let bootVersion = ''
let verTick = 0
let verHintShown = false

async function poll() {
  sleepGreet()
  try {
    const r = await api.openRequest()
    if (r?.quitting) { pageQuit(); return }
    if (r?.pid && r.pid !== store.currentId) {
      await refreshPapers()
      await openPaper(r.pid)
    }
  } catch { /* 轮询里的失败不打扰用户 */ }
  if (bootVersion && !verHintShown && ++verTick % 5 === 0) {
    try {
      const v = await api.version()
      if (v.version && v.version !== bootVersion) {
        verHintShown = true
        toast(t('eggpaper 已更新到 {v}，正在刷新界面…', { v: v.version }), 6000)
        setTimeout(() => window.location.reload(), 1200)
      }
    } catch { /* 下个 15 秒再问 */ }
  }
  if (!store.currentId) return
  try { if (anaBusy.value) await refreshAnalysis() } catch { /* 下一个 3 秒再试 */ }
  try {
    if (store.marginalia.status === 'running') {
      await refreshMarginalia()
      startMarginFast()      // 刷新/换篇回来时也接上快轮询（否则只能等 3 秒那条）
    }
  } catch { /* 同上 */ }
  try {
    const p = store.papers.find(x => x.id === store.currentId)
    if (p && p.translate_status === 'running') await pollTranslate()
  } catch { /* 同上 */ }
}

/* 文库与论文日历同侧互斥：开一个收另一个，永远只有一层抽屉。 */
function toggleLib() {
  store.viewer.libOpen = !store.viewer.libOpen
  if (store.viewer.libOpen) { store.viewer.calOpen = false; store.viewer.tocOpen = false }
}
function toggleCal() {
  store.viewer.calOpen = !store.viewer.calOpen
  if (store.viewer.calOpen) { store.viewer.libOpen = false; store.viewer.tocOpen = false }
}
function toggleToc() {
  store.viewer.tocOpen = !store.viewer.tocOpen
  if (store.viewer.tocOpen) { store.viewer.libOpen = false; store.viewer.calOpen = false }
}

/* 窄窗：右栏改浮层，进窄窗时自动收起一次，把宽度还给论文
   （只在跨过门槛那一拍动手，否则用户手动展开会被反复关掉） */
watch(() => store.narrow, (n, o) => { if (n && !o) store.viewer.railUser = false })

/* 「退出 eggpaper」的页面侧收尾：独立窗口是浏览器 --app 模式，window.close() 有效；
   普通浏览器标签页浏览器不许脚本关（安全模型），关不掉就亮一层兜底遮罩，
   别让用户对着一个后端已死的页面发愣。后端会多等一拍轮询才真正退出。 */
const quitMask = ref(false)
function pageQuit() {
  if (quitMask.value) return      // 收尾只做一次：反复 window.close() 只会刷浏览器警告
  quitMask.value = true
  window.close()
}

/* 设置里的「退出 eggpaper」：请后台退出并顺手关掉自己这个窗口/标签页；
   其余打开着的页面在下一拍轮询里收到 quitting 各自关闭（后端等一拍才退）。 */
async function onQuitApp() {
  try { await api.quit({ reason: 'user' }) } catch (e) { toast(e.message); return }
  pageQuit()
}

/* 深夜彩蛋：0–5 点 eggpaper 还开着，蛋就躺下睡了（纯 CSS 躺倒，印章几何不变），
   问候一晚只说一次（本地记日期）。开着跨进零点的那一拍由 3 秒轮询接住；天亮自己醒。 */
const sleepEgg = ref(false)
const _deepNight = () => new Date().getHours() < 5
function sleepGreet() {
  if (_deepNight()) {
    sleepEgg.value = true
    const today = new Date().toDateString()
    if (localStorage.getItem('egg:sleep-greet') !== today) {
      localStorage.setItem('egg:sleep-greet', today)
      toast(t('蛋都睡了，你还在读。'), 8000)
    }
  } else if (sleepEgg.value) {
    sleepEgg.value = false
  }
}

/* 本会话点过「析读」的凭据（哪篇、几点点的）：秒完的演示析读第一次拉状态就直接是
   done（前一拍还是 none），只看 running/queued 会漏掉这条路径，所以留一份记录。
   绑篇目：点了 A 的析读后转头去开旧论文 B，不该把 B 的右栏也强行撑开。 */
let analyzeReq = { id: '', at: 0 }

watch(() => store.analysis.status, (n, o) => {
  if (n === 'done' && o && o !== 'done' && o !== 'none') {
    topPet.rollOnce(); reloadSummary()
    const mine = analyzeReq.id === store.currentId && Date.now() - analyzeReq.at < 600000
    if (o === 'running' || o === 'queued' || mine) store.viewer.railUser = true
  }
})

async function doAnalyze() {
  if (!store.currentId) return
  analyzeReq = { id: store.currentId, at: Date.now() }
  await api.analyze(store.currentId)
  await refreshAnalysis()
  if (store.analysis.status === 'done') {
    topPet.rollOnce(); reloadSummary()
    store.viewer.railUser = true
  }
}

async function doMarginalia() {
  if (!store.currentId) return
  await api.marginaliaStart(store.currentId)
  await refreshMarginalia()
  startMarginFast()
}

/* 眉批是**唯一**会持续几十秒到几分钟的任务。全局那条 3 秒轮询在它跑完那一刻最多还要
   再等 3 秒才把结果取回来——"明明好了却还显示在写"就是这么来的。只给它一条 1 秒的
   快轮询，跑完立刻停；其他功能照旧 3 秒，互不影响。 */
function startMarginFast() {
  if (marginFastTimer) return
  marginFastTimer = setInterval(async () => {
    await refreshMarginalia()
    if (store.marginalia.status !== 'running') {
      clearInterval(marginFastTimer)
      marginFastTimer = null
    }
  }, 1000)
}
onUnmounted(() => clearInterval(marginFastTimer))

async function doTranslateFull() {
  if (!store.currentId) return
  const again = tranSt.value === 'done'
  try {
    const r = await api.translateFull(store.currentId, again)
    tranProg.value = { done: 0, total: 0, svc: r.service || '', started: Date.now() / 1000 | 0 }
    await refreshPapers()
    toast(r.note || (again ? t('已开始重新整本翻译') : t('整本翻译已开始')))
  } catch (e) { toast(t('启动失败：{m}', { m: e.message })) }
}

/* 整本翻译的进度：pdf2zh 用 tqdm 打 `11%|██ | 2/18`，后端逐行抠出页数。
   完成这一拍也在这里接——完成通知与「译文/双语」的解锁都看它。 */
async function pollTranslate() {
  if (!store.currentId) return
  const j = await api.translateStatus(store.currentId)
  if (j.pages && j.pages[1]) tranProg.value = { done: j.pages[0], total: j.pages[1], svc: j.service || '' }
  if (j.status === 'done') {
    tranProg.value = { done: 0, total: 0, svc: '' }
    await refreshPapers()                 // 译文/双语两个按钮看的是 papers 里的 translate_status
    cheerEgg()                            // 整本书翻完了：跳两下（析读完成才是滚一圈）
    toast(t('整本翻译完成'))   // 盘上只落译文版，双语首次点开才派生——"双语已生成"是假话
  } else if (j.status === 'error') {
    tranProg.value = { done: 0, total: 0, svc: '' }
    await refreshPapers()
    toast(t('整本翻译失败：{m}', { m: (j.error || '').slice(0, 100) }), 6000)
  }
}

/* 应用级的选择入口：空态那一屏（整屏可点）、以及任何不在文库面板里的时候都用它。
   文库面板里另有一个自己的 input（面板收起时那个 input 会随组件卸载，靠不住）。 */
const appFile = ref(null)
function pickFiles() { appFile.value?.click() }
function onAppFile(e) {
  onImport(Array.from(e.target.files || []))
  e.target.value = ''                     // 同一个文件连选两次也要能再触发
}

/* 导入：可以一次给多篇。**逐篇上传**而不是并发——
   每篇的解析在服务端是几秒钟的活，并发只会让服务端更忙、提示也更乱；
   逐篇还能说清"正在导入第 2/5 篇"，并且**第一篇一到就打开**，不用等全部传完。
   其余的在后台排队通读（服务端是一条串行队列），列表里能看到谁在排队、谁在读。 */
async function onImport(list) {
  const files = (Array.isArray(list) ? list : [list]).filter(f => f && f.name)
  if (!files.length) return
  const many = files.length > 1
  const ok = []
  for (let i = 0; i < files.length; i++) {
    const f = files[i]
    toast(many ? t('正在导入 {i}/{n}：{name}', { i: i + 1, n: files.length, name: f.name.slice(0, 24) })
                    : t('已导入，正在后台通读…'))
    try {
      const r = await api.upload(f)
      ok.push(r)
      if (r.duplicate) toast(t('库里已有这篇——直接打开原来那份'))
      const c = store.lib.coll
      if (typeof c === 'number') {
        try { await api.paperColls(r.paper.id, [c]); await refreshCollections() } catch { /* 归类失败不影响导入 */ }
      }
      if (i === 0) {                    // 只打开第一篇：剩下的别把界面抢过去
        await openPaper(r.paper.id)
        store.viewer.libOpen = false
      }
      await refreshPapers()
    } catch (e) {
      toast(t('《{name}》导入失败：{m}', { name: f.name.slice(0, 20), m: e.message }))
    }
  }
  const first = ok[0]
  if (!first) return
  if (many && ok.length > 1) {
    toast(t('已导入 {n} 篇，其余在后台排队通读', { n: ok.length }))
  } else if (first.no_text) {
    toast(t('扫描件：只能读，析读与眉批用不了'))
  } else if (first.n_paragraphs && first.n_paragraphs < 5) {
    toast(t('只认出 {n} 段，析读会比较粗', { n: first.n_paragraphs }))
  }
}

async function saveSettings(body) {
  store.settings = await api.saveSettings(body)
  showSettings.value = false
  if (store.currentId) refreshAnalysis()
}

const anaBusy = computed(() => ['running', 'queued'].includes(store.analysis.status))
/* 日历图标上的小点：今天已经读过点什么——轻提醒，不弹任何东西 */
const readToday = computed(() => {
  const d = new Date()
  const day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return store.papers.some(p => (p.last_read_at || '').startsWith(day))
})
const tranSt = computed(() => store.papers.find(x => x.id === store.currentId)?.translate_status || 'none')
const tranProg = ref({ done: 0, total: 0, svc: '', started: 0 })
const tranPct = computed(() => tranProg.value.total
  ? Math.round(tranProg.value.done * 100 / tranProg.value.total) : 0)
const tranTick = ref(0)
let tranTimer = null
watch(tranSt, s => {
  clearInterval(tranTimer)
  if (s === 'running') tranTimer = setInterval(() => { tranTick.value++ }, 1000)
}, { immediate: true })
onUnmounted(() => clearInterval(tranTimer))
const tranElapsed = computed(() => {
  tranTick.value
  if (!tranProg.value.started) return ''
  const t = Math.max(0, Math.round(Date.now() / 1000 - tranProg.value.started))
  return t >= 90 ? t('{m} 分 {s} 秒', { m: Math.floor(t / 60), s: t % 60 }) : t('{s} 秒', { s: t })
})
const tranLabel = computed(() => {
  if (tranSt.value === 'done') return t('重新整本翻译')
  if (tranSt.value !== 'running') return t('整本翻译')
  const n = tranProg.value.total ? ` ${tranProg.value.done}/${tranProg.value.total}` : '…'
  return t('翻译中') + n
})
const tranTip = computed(() => {
  if (tranSt.value === 'running') {
    return t('正在译{svc} · 已用 {t}', { svc: tranProg.value.svc ? `（${tranProg.value.svc}）` : '',
                                         t: tranElapsed.value || t('刚刚') })
  }
  return t('译出第二份 PDF，供「译文 / 双语」')
})

/* ---------------- 键盘流 ---------------- */
function onKey(e) {
  const t = e.target
  if (dlg.open) { if (e.key === 'Escape') { e.preventDefault(); dlgCancel() } return }
  if (t && (t.matches?.('input, textarea, select') || t.isContentEditable)) return
  if (showSettings.value) return
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') { e.preventDefault(); store.viewerApi?.openSearch(); return }
  if (e.altKey && e.key === 'ArrowLeft') { store.viewerApi?.jumpBack(); e.preventDefault(); return }
  if (e.key === 'Escape') {
    store.viewer.libOpen = false
    store.viewer.calOpen = false
    store.viewer.tocOpen = false
    store.shortcutCard = false
    store.cite.open = false
    dragOver.value = false
    store.viewer.frame = false     // 框选模式永远能一键退出
    store.escTick++                // PDF 侧的划词/框选/查找浮层收起
    return
  }
  if (gPending.value) {
    gPending.value = false
    if (e.key === 'l') { store.viewer.libOpen = true; store.viewer.calOpen = false; e.preventDefault() }
    if (e.key === 'c') { store.viewer.calOpen = true; store.viewer.libOpen = false; store.viewer.tocOpen = false; e.preventDefault() }
    if (e.key === 'o') { store.viewer.tocOpen = true; store.viewer.libOpen = false; store.viewer.calOpen = false; e.preventDefault() }
    if (e.key === 'h') { goHome(); e.preventDefault() }
    return
  }
  if (!store.paper) return
  if (isEn() && ['t', 's', '2', '3'].includes(e.key)) return
  switch (e.key) {
    case 'j': e.preventDefault(); store.viewerApi?.step(1); break
    case 'k': e.preventDefault(); store.viewerApi?.step(-1); break
    case 'PageDown': e.preventDefault(); store.viewerApi?.stepPage(1); break
    case 'PageUp': e.preventDefault(); store.viewerApi?.stepPage(-1); break
    case 't': store.viewerApi?.translateCurrent(); break
    case 's': store.viewerApi?.translateSelectionKey(); break
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
      <div class="wordmark" :title="t('回书桌')" @click="goHome">
        <span class="egg-wrap" :ref="r => (topPet.el = r)" @click.stop="topPet.poke" @wheel="topPet.wheel"
              @dragenter="topPet.enter" @dragover="topPet.over" @dragleave="topPet.leave" @drop="topPet.drop">
          <EggMark class="egg pet" :class="petCls(topPet)" :style="spinStyle(topPet)" />
          <span class="egg-z" v-if="sleepEgg" aria-hidden="true"><i>z</i><i>z</i></span>
        </span>
        <span class="name">eggpaper</span>
      </div>
      <div class="doc-head" v-if="store.paper">
        <div class="t-row">
          <div class="t">{{ store.paper.title || store.paper.filename }}</div>
                    <button class="cite-btn" @click="store.cite.open = true">{{ t('引用') }}</button>
        </div>
      </div>
      <div class="actions" v-if="store.paper">
        <div class="segmented" v-if="!isEn()" :style="{ '--n': 3, '--i': VARIANTS.indexOf(store.viewer.variant) }">
          <span class="seg-thumb" />
          <button :class="{ on: store.viewer.variant === 'original' }" @click="store.viewer.variant = 'original'">{{ t('原文') }}</button>
          <button :class="{ on: store.viewer.variant === 'mono' }" :disabled="tranSt !== 'done'" @click="store.viewer.variant = 'mono'">{{ t('译文') }}</button>
          <button :class="{ on: store.viewer.variant === 'dual' }" :disabled="tranSt !== 'done'" @click="store.viewer.variant = 'dual'">{{ t('双语') }}</button>
        </div>
        <Transition name="fade">
          <div class="segmented mini" v-if="store.viewer.variant === 'dual'"
               :style="{ '--n': 2, '--i': store.viewer.spread === 'spread' ? 0 : 1 }">
            <span class="seg-thumb" />
            <button :class="{ on: store.viewer.spread === 'spread' }" @click="store.viewer.spread = 'spread'">{{ t('对开') }}</button>
            <button :class="{ on: store.viewer.spread === 'interleave' }" @click="store.viewer.spread = 'interleave'">{{ t('交替') }}</button>
          </div>
        </Transition>
                <button class="toggle" :class="{ on: store.viewer.frame }" :title="t('框选问 AI（r）')"
                @click="store.viewer.frame = !store.viewer.frame">{{ t('框选') }}</button>
                        <button v-if="!isEn()" @click="doTranslateFull" :disabled="tranSt === 'running'"
                :title="tranTip">
          {{ tranLabel }}
        </button>
        <button class="primary" @click="doAnalyze" :disabled="anaBusy">
          {{ store.analysis.status === 'queued' ? t('排队中…') : (store.analysis.status === 'running' ? t('通读中…')
             : (store.analysis.status === 'done' ? t('重新析读') : t('析读'))) }}
        </button>
      </div>
      <div class="actions">
        <button class="ghost" @click="showSettings = true" :title="t('设置')">⚙</button>
      </div>
            <div class="tran-line" v-if="tranSt === 'running' && !isEn()">
        <i :class="{ det: tranPct > 0 }" :style="tranPct > 0 ? { width: tranPct + '%' } : null"></i>
      </div>
    </header>

    <div class="main">
            <div class="left-strip">
        <button class="strip-btn" :class="{ on: store.viewer.libOpen }" :title="t('文库 · g l')"
                @click="toggleLib">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M4 4h6v16H4zM14 4h6v16h-6z" />
            <path d="M7 8h.01M7 12h.01M17 8h.01M17 12h.01" stroke-linecap="round" stroke-width="2.4" />
          </svg>
          <span class="badge" v-if="store.papers.length">{{ store.papers.length }}</span>
        </button>
        <button class="strip-btn" :class="{ on: store.viewer.calOpen }" :title="t('论文日历 · g c')"
                @click="toggleCal">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <rect x="4" y="5.5" width="16" height="14.5" rx="1.5" />
            <path d="M4 10.5h16M8.5 3.5v3.5M15.5 3.5v3.5" stroke-linecap="round" />
          </svg>
          <i class="strip-dot" v-if="readToday && !store.viewer.calOpen"></i>
        </button>
        <button class="strip-btn" :class="{ on: store.viewer.tocOpen }" :title="t('目录 · g o')"
                @click="toggleToc">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
            <path d="M5 6h14M5 11.5h9M5 17h5M17.5 14.5v6M14.5 17.5h6" />
          </svg>
        </button>
        <div class="strip-sep"></div>
      </div>

            <main class="desk">
                <div class="empty" v-if="!store.paper">
          <span class="egg-wrap" :ref="r => (deskPet.el = r)" @click.stop="deskPet.poke" @wheel="deskPet.wheel"
                @dragenter="deskPet.enter" @dragover="deskPet.over" @dragleave="deskPet.leave" @drop="deskPet.drop">
            <EggMark class="egg-big pet" :class="petCls(deskPet, { hop: dragOver })" :style="spinStyle(deskPet)" />
            <span class="egg-z" v-if="sleepEgg" aria-hidden="true"><i>z</i><i>z</i></span>
          </span>
          <div class="e-title" role="button" tabindex="0" @click="pickFiles"
               @keydown.enter.prevent="pickFiles" @keydown.space.prevent="pickFiles">{{ t('论文，启动！') }}</div>
          <div class="stamp" role="button" tabindex="0" @click="pickFiles"
               @keydown.enter.prevent="pickFiles" @keydown.space.prevent="pickFiles">EGGPAPER · LOCAL-FIRST</div>
          <div class="desk-hint">{{ t('拖入PDF或点击论文启动选择文件') }}</div>
        </div>
        <PdfViewer v-else :key="store.currentId" />
      </main>

            <button class="rail-tab" v-if="store.paper && !store.railRight" :title="t('展开右栏 · x')"
              @click="store.viewer.railUser = true">◂</button>
            <div class="rail-wrap" v-if="store.paper" :class="{ collapsed: !store.railRight }"
           :style="{ '--rail-w': store.viewer.railW + 'px' }">
        <RightRail @analyze="doAnalyze" @marginalia="doMarginalia" />
      </div>
    </div>

    <Transition name="fade">
      <div class="lib-mask" v-if="store.viewer.libOpen || store.viewer.calOpen || store.viewer.tocOpen"
           @click="store.viewer.libOpen = store.viewer.calOpen = store.viewer.tocOpen = false"></div>
    </Transition>
    <Transition name="slide-l">
      <LibPanel v-if="store.viewer.libOpen" @import="onImport" @close="store.viewer.libOpen = false" />
    </Transition>
    <Transition name="slide-l">
      <CalendarPanel v-if="store.viewer.calOpen" />
    </Transition>
    <Transition name="slide-l">
      <TOCPanel v-if="store.viewer.tocOpen" />
    </Transition>
    <Transition name="fade">
      <SettingsModal v-if="showSettings" @close="showSettings = false" @save="saveSettings" @quit="onQuitApp" />
    </Transition>
        <Dialog />
    <CiteCard />
    <UpdateCard />
        <div class="quit-mask" v-if="quitMask">{{ t('eggpaper 已退出，这个页面可以关掉了。') }}</div>
        <input ref="appFile" type="file" accept="application/pdf" multiple hidden @change="onAppFile" />

        <Transition name="pop">
    <div class="keys-card" v-if="store.shortcutCard" @click="store.shortcutCard = false">
      <div class="mono-label" style="margin-bottom:8px">{{ t('键盘') }}</div>
      <div class="k-row"><span>{{ t('下一段 / 上一段') }}</span><kbd>j / k</kbd></div>
      <div class="k-row" v-if="!isEn()"><span>{{ t('译当前段并钉页边') }}</span><kbd>t</kbd></div>
      <div class="k-row" v-if="!isEn()"><span>{{ t('翻译划选') }}</span><kbd>s</kbd></div>
      <div class="k-row"><span>{{ t('框选问 AI（Esc 退出）') }}</span><kbd>r</kbd></div>
      <div class="k-row" v-if="!isEn()"><span>{{ t('原文 / 译文 / 双语') }}</span><kbd>1 / 2 / 3</kbd></div>
      <div class="k-row"><span>{{ t('聚焦提问') }}</span><kbd>/</kbd></div>
      <div class="k-row"><span>{{ t('折叠右栏') }}</span><kbd>x</kbd></div>
      <div class="k-row"><span>{{ t('文库 / 日历 / 目录') }}</span><kbd>g l / g c / g o</kbd></div>
      <div class="k-row"><span>{{ t('回书桌') }}</span><kbd>g h</kbd></div>
      <div class="k-row"><span>{{ t('返回原位') }}</span><kbd>Alt + ←</kbd></div>
      <div class="k-row"><span>{{ t('收起所有浮层 / 退出框选') }}</span><kbd>Esc</kbd></div>
    </div>
    </Transition>

    <Transition name="pop">
      <div class="toast" v-if="store.toast">{{ store.toast }}</div>
    </Transition>
        <i class="feed-ghost" v-for="g in ghosts" :key="g.id" aria-hidden="true"
       :style="{ left: g.x + 'px', top: g.y + 'px', '--dx': (g.tx - g.x) + 'px', '--dy': (g.ty - g.y) + 'px', animationDelay: g.delay + 'ms' }" />
    <Transition name="fade">
    <div class="modal-mask" v-if="dragOver && store.paper" style="pointer-events:none; background:rgba(29,27,23,.22)">
      <div class="modal" style="text-align:center">
        <div style="font-size:var(--fs-xl);font-weight:650">{{ t('松手，放到书桌上') }}</div>
      </div>
    </div>
    </Transition>
  </div>
</template>
