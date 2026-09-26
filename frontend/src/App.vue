<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { goHome, lsGet, lsSet, api, store, toast, refreshPapers, refreshCollections, openPaper, refreshAnalysis, refreshMarginalia, reloadSummary, checkUpdate, loadVersion, startLibraryWatch } from './store'
import PdfViewer from './components/PdfViewer.vue'
import LibPanel from './components/LeftRail.vue'
import CalendarPanel from './components/CalendarPanel.vue'
import TOCPanel from './components/TOCPanel.vue'
import RightRail from './components/RightRail.vue'
import SettingsModal from './components/SettingsModal.vue'
import Dialog from './components/Dialog.vue'
import CiteCard from './components/CiteCard.vue'
import UpdateCard from './components/UpdateCard.vue'
import { dlg, dlgCancel, choiceBox } from './dialog'
import { engInst, watchEngine, hideEngineCard, closeEngineCard, cancelEngineInstall,
         startEngineInstall, onEngineReady, fmtMB } from './engine'
import EggMark from './components/EggMark.vue'
import { festivalSkin } from './festival'
import { t, isEn, setLang } from './i18n'

/* 节日换装：按日期给蛋换皮肤；localStorage 的 eggpaper:skin 可手动预览任意一套 */
const eggSkin = festivalSkin()

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
   idle(自发小动作) < hungry < busy(含字条流过) < doze < wobble < 喷嚏/趔趄 < sleep < gulp/stuff < roll < cheer；
   sleep-poke 专盖 sleep；spin 是行内 transform，只在没有任何动画类时可见。
   JS 侧守卫：睡着不馋/不搓/不庆祝，被戳只抖不醒；干活时不搓；搓着时不接戳。 */
function makePet() {
  const pet = reactive({
    el: null,
    wobbling: false, surprise: '', sleepPoke: false, roll: false,
    spinDeg: 0, spinning: false, spinFree: false, hungry: false, gulping: '',
    idle: '',            // 自发的小动作（'tilt' | 'stretch' | 'yawn' | 'shiver' | 'sway'）：没人戳时它自己也活着
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
    pokeTally(pet)
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
    feed(e.clientX, e.clientY, files.length, e.currentTarget)
    onImport(files)
  }
  function feed(x, y, n, targetEl) {
    if (sleepEgg.value) return          // 它睡着了：饭照收（导入照跑），仪式免了
    const r = (targetEl || pet.el)?.getBoundingClientRect?.()
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
    poke, wheel, enter, over, leave, drop, flash,
    rollOnce: () => flash('roll', 700),
    wobbleOnce: () => flash('wobbling', 450),
  })
}
const ghosts = ref([])           // 飞行途中的纸片（两只蛋共用一条渲染通道）
let ghostId = 0
/* 拖动蛋：长按 400ms 抓起（期间松手仍是戳，互不打架），可拖到屏幕任意位置安家；
   拖回左上角原位附近自动磁吸归位。位置记在本机，刷新后它还在原地。 */
const homeEl = ref(null)         // 顶栏原位的蛋占位（蛋被拖走后仍占着坑）
const petPos = ref(null)         // null = 原位；{x,y} = 浮动位（视口 CSS 像素）
const dragFloat = ref(false)     // 正被拖着走
let armT = 0, dragOff = { x: 0, y: 0 }, suppressClick = false
const FLOAT_W = 27

function clampPos(x, y) {
  return { x: Math.min(Math.max(8, x), window.innerWidth - FLOAT_W - 8),
           y: Math.min(Math.max(8, y), window.innerHeight - FLOAT_W - 8) }
}
function homeCenter() {
  const r = homeEl.value?.getBoundingClientRect?.()
  return r ? { x: r.left + r.width / 2, y: r.top + r.height / 2, left: r.left, top: r.top } : null
}
function petDown(e) {
  if (e.button !== 0 || topPet.spinning) return
  const r = e.currentTarget.getBoundingClientRect()
  dragOff = { x: e.clientX - r.left, y: e.clientY - r.top }
  clearTimeout(armT)
  suppressClick = false
  armT = setTimeout(() => {       // 按住 400ms：抓起来
    suppressClick = true
    dragFloat.value = true
    if (!petPos.value) petPos.value = clampPos(r.left, r.top)   // 从原位抓起的一瞬
  }, 400)
  const move = ev => {
    if (!dragFloat.value) {
      if (Math.hypot(ev.clientX - e.clientX, ev.clientY - e.clientY) > 10) {
        clearTimeout(armT)        // 没抓起来就滑走了：当普通划过
        cleanup()
      }
      return
    }
    ev.preventDefault()
    petPos.value = clampPos(ev.clientX - dragOff.x, ev.clientY - dragOff.y)
  }
  const cancel = () => { clearTimeout(armT); cleanup() }
  const up = () => {
    cleanup()
    clearTimeout(armT)
    if (!dragFloat.value) return
    dragFloat.value = false
    const c = petPos.value, home = homeCenter()
    if (c && home && Math.hypot(c.x + FLOAT_W / 2 - home.x, c.y + FLOAT_W / 2 - home.y) < 60) {
      petPos.value = null                     // 放回原位：晃一下算打招呼
      topPet.wobbleOnce()
      lsSet('pet-pos', null)
      return
    }
    if (c) lsSet('pet-pos', c)
    setTimeout(() => (suppressClick = false), 60)   // 让落位这一下 click 被抑制掉
  }
  const cleanup = () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    window.removeEventListener('blur', cancel)
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  window.addEventListener('blur', cancel)
}
function petClick() {
  if (suppressClick) { suppressClick = false; return }   // 抓起又放下的那一下不是戳
  topPet.poke()
}
function petReclamp() { if (petPos.value) petPos.value = clampPos(petPos.value.x, petPos.value.y) }
onMounted(() => {
  const saved = lsGet('pet-pos', null)
  if (saved && typeof saved.x === 'number') petPos.value = clampPos(saved.x, saved.y)
  window.addEventListener('resize', petReclamp)
})
/* 彩蛋：戳满随机 5–12 下，三条线亮出彩色；彩色时再点一下就回去，回去后重新抽签。
   不提示不庆祝，滚一圈就是全部动静；悬浮蛋上的「彩蛋」是唯一的暗示。 */
const rainbow = ref(lsGet('pet-rainbow', false))
let pokeTallyN = 0
let pokeGoal = 5 + Math.floor(Math.random() * 8)
function pokeTally(pet) {
  if (rainbow.value) {
    lsSet('pet-rainbow', false)
    rainbow.value = false
    pokeTallyN = 0
    pokeGoal = 5 + Math.floor(Math.random() * 8)
    return
  }
  pokeTallyN++
  if (pokeTallyN < pokeGoal) return
  lsSet('pet-rainbow', true)
  rainbow.value = true
  pokeTallyN = 0
  pet.rollOnce()
}
const topPet = makePet()         // 顶栏那枚；析读完成/全文译完的动作滚跳也归它
const deskPet = makePet()        // 书桌空态那枚大的

/* 陪伴节拍：没人戳的时候它自己也活着——约半小时随机做一个小动作（每 10 分钟看一眼时机，
   三分之一概率动），像真人一样没有准点。只挑闲着的蛋（被戳/被喂/在干活都让路）、
   只在页面看得见时动、优先在屏幕上那只。 */
const IDLE_POOL = [
  ['tilt', 1700], ['stretch', 2100], ['yawn', 2500], ['shiver', 700], ['sway', 1500],
]
function idleTick() {
  if (document.hidden || sleepEgg.value || dozing.value) return
  const free = [topPet, deskPet]
    .filter(p => !(p.idle || p.wobbling || p.surprise || p.gulping || p.roll || p.hungry || p.spinning))
  if (!free.length || Math.random() > 1 / 3) return
  const seen = free.filter(p => p.el)
  const pick = seen.length ? seen : free
  const pet = pick[Math.floor(Math.random() * pick.length)]
  const [act, ms] = IDLE_POOL[Math.floor(Math.random() * IDLE_POOL.length)]
  pet.flash('idle', ms, act)
}
watch(() => store.egg?.nod, () => {          // 提问时蛋歪头看你一眼：它在陪你思考
  if (sleepEgg.value) return
  const pet = deskPet.el ? deskPet : topPet
  if (pet.idle || pet.wobbling || pet.surprise || pet.gulping || pet.roll) return
  pet.flash('idle', 1400, 'tilt')
})
let idleTimerId = 0
function petCls(p, extra) {
  return [{ hungry: p.hungry }, { roll: p.roll }, { wobble: p.wobbling }, p.surprise,
    { sleep: sleepEgg.value }, { 'sleep-poke': p.sleepPoke }, { busy: eggBusy.value }, p.gulping,
    { cheer: eggCheer.value }, { doze: dozing.value }, { spun: p.spinning }, { free: p.spinFree },
    { rainbow: rainbow.value }, p.idle,
    ...(extra ? [extra] : [])]
}
function spinStyle(p) {
  return p.spinning ? { transform: `rotate(${p.spinDeg}deg) scale(${p.spinFree ? 1 : 0.94})` } : null
}

/* 干活与庆祝：全文翻译跑着、或析读在通读时，蛋轻轻晃着埋头干（eggBusy；深夜它睡了，睡觉优先）——
   你在等析读，它也在一起读。这一篇译完跳两下（cheerEgg，pollTranslate 的完成拍调用）——
   全文翻完比析读完更有分量，跳两下；析读完成仍是滚一圈。轮询只看当前论文，人不在场就不庆祝。 */
const eggBusy = computed(() =>
  (tranSt.value === 'running' || ['running', 'queued'].includes(store.analysis.status)) && !sleepEgg.value)
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
  startLibraryWatch()         // 文库对账：别的窗口导入/删了，这个页面的列表 5 秒内跟上
  idleTimerId = setInterval(idleTick, 600000)   // 陪伴节拍：平均半小时左右一个小动作
  greetBootT = setTimeout(maybeGreet, 2500)     // 开场问候：界面站稳后轻轻说一句
  sleepGreet()               // 深夜开着 eggpaper：蛋先睡下，问候随后
  window.addEventListener('pointermove', wakeEgg, { passive: true })
  window.addEventListener('pointerdown', wakeEgg, { passive: true })
  window.addEventListener('keydown', wakeEgg)
  window.addEventListener('wheel', wakeEgg, { passive: true })
  wakeEgg()                  // 先把打盹的表立起来
  try {
    store.settings = await api.settings()
    if (store.settings?.ui_lang) setLang(store.settings.ui_lang)
    await Promise.all([refreshPapers(), refreshCollections()])   // 两个列表互不依赖，并行走
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
  clearInterval(idleTimerId)
  clearTimeout(greetHideT)
  clearTimeout(greetBootT)
  clearTimeout(idleTimer)
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('dragend', endDrag)
  window.removeEventListener('blur', endDrag)
  window.removeEventListener('resize', petReclamp)
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
  try { if (store.papers.some(p => p.translate_status === 'running')) await bgTranslateTick() } catch { /* 同上 */ }
  if (bootVersion && !verHintShown && ++verTick % 5 === 0) {
    try {
      const v = await api.version()
      if (v.version && v.version !== bootVersion) {
        verHintShown = true
        toast(t('已更新到 {v}，刷新中…', { v: v.version }), 6000)
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

/* 左侧三只抽屉互斥：开一个收另一个，永远只有一层。按钮（可切换）和 g l/c/o（只开不关）
   都走这一个口，避免各写一份互斥清单后越改越漏。 */
function openDrawer(name) {
  store.viewer.libOpen = name === 'lib'
  store.viewer.calOpen = name === 'cal'
  store.viewer.tocOpen = name === 'toc'
}
const toggleLib = () => openDrawer(store.viewer.libOpen ? null : 'lib')
const toggleCal = () => openDrawer(store.viewer.calOpen ? null : 'cal')
const toggleToc = () => openDrawer(store.viewer.tocOpen ? null : 'toc')

/* 窄窗：右栏改浮层，进窄窗时自动收起一次，把宽度还给论文
   （只在跨过门槛那一拍动手，否则用户手动展开会被反复关掉）。
   去抖 300ms：视口宽度的瞬时抖动（窗口恢复、嵌入容器重排）不算数——
   只认"持续停在窄窗"的跨入，否则一次抖动就把用户展开的右栏永久收走。 */
let narrowDeb = 0
watch(() => store.narrow, (n, o) => {
  clearTimeout(narrowDeb)
  if (n && !o) narrowDeb = setTimeout(() => { if (store.narrow) store.viewer.railUser = false }, 300)
})

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
   每天一晚说一句晚安（统一的时段文字框）。开着跨进零点的那一拍由 3 秒轮询接住；天亮自己醒。 */
const sleepEgg = ref(false)
const _deepNight = () => new Date().getHours() < 5
function sleepGreet() {
  if (_deepNight()) {
    sleepEgg.value = true
    maybeGreet()
  } else if (sleepEgg.value) {
    sleepEgg.value = false
  }
}

/* 时段问候：固定几个有情绪的时刻，打开软件时轻轻说一句话。
   每个时段每天最多一次（本地记日期）；出现与消失是同一个文字框（取景框纸卡），
   pointer-events none 不挡操作。文案每次启动随机抽一条。 */
const GREET_POOL = {
  midnight: [
    '怎么还没睡？照顾好身体。',
    '再读一篇就睡——这话你昨晚也说过。',
    '凌晨的文献，自带荧光标记。',
    '黑眼圈和 impact factor，总得涨一个。',
    '我是eggpaper，补觉啦。',
    '读到这个点，敬自己一杯。',
  ],
  dawn: [
    '这么早就打卡啦！？',
    '早起的人，先拿捏今天的 DDL。',
    '你是懂卷的。',
    '水灵灵地，就开始读文献了。',
    '晨读 buff 已上线。',
    '你起这么早，DDL 知道吗？',
  ],
  morning: [
    '打起精神！',
    '文献都醒了，就等你了。',
    '咖啡就位，精神状态已拉满。',
    '打开大佬的新论文，纯属误闯天家。',
    '具身智能，说的就是我。',
    '趁导师没醒，多读两篇。',
    '我和参考文献是青梅牛马。',
  ],
  noon: [
    '你先读着，我眯一会。',
    '预制的午饭，配现读的文献。',
    '读完你的读你的。',
    '趁午休读文献，你的胆子真是肥嘟嘟的。',
    '科研的尽头是野生狗奶。',
    '人家养龙虾，你养参考文献。',
  ],
  afternoon: [
    '论文还是摸鱼，这是一个问题。',
    '摸鱼一时爽，DDL 火葬场。',
    '偷感很重地开始卷文献。',
    '说好的从从容容读三篇，现在是连滚带爬。',
    '数据太漂亮了，我要验牌。',
    '读不完？如何呢，又能怎。',
  ],
  night: [
    '生活不止眼前的苟且，还有诗和论文。',
    '今晚的月亮，和 deadline 一起加班。',
    '晚上读文献，一读一个不吱声。',
    '文献读得好，accept 来得早。',
    '今天的文献，牌没有问题。',
    '读论文给我读好的呀。',
  ],
}
function greetSlot() {
  const h = new Date().getHours()
  if (h < 5) return 'midnight'
  if (h < 8) return 'dawn'
  if (h < 12) return 'morning'
  if (h < 14) return 'noon'
  if (h < 19) return 'afternoon'
  return 'night'
}
const greetKey = ref('')
/* 文案存键、显示时才过 t()：开场问候比启动设置先到，界面语言随后切过去时卡片跟着换，
   不然纯英文用户开场会看到一句中文。 */
const greetText = computed(() => t(greetKey.value))
/* 日历/目录抽屉与文库同宽：文库面板自己内联 --lib-w，这里是给另外两个抽屉的全局兜底。
   模板表达式拿不到 localStorage 全局，读法收在这里。 */
const libWCss = () => (store.viewer.libW ?? 300) + 'px'   // 响应式：拖宽后另两只抽屉立刻跟宽
const greetShow = ref(false)
const greetShown = new Set()     // 本次运行里已经问候过的时段：冷启动清零，跨时段会再问候
let greetHideT = 0
let greetBootT = 0
function maybeGreet() {
  const slot = greetSlot()
  if (!slot || greetShow.value || greetShown.has(slot)) return
  greetShown.add(slot)
  const pool = GREET_POOL[slot]
  greetKey.value = pool[Math.floor(Math.random() * pool.length)]
  greetShow.value = true
  clearTimeout(greetHideT)
  greetHideT = setTimeout(() => (greetShow.value = false), 5600)
}
/* 论文一打开问候就让位：它悬在页面正中，不能压在正文上。 */
watch(() => store.currentId, id => { if (id && greetShow.value) { clearTimeout(greetHideT); greetShow.value = false } })

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

let anaStarting = false
async function doAnalyze() {
  if (!store.currentId || anaBusy.value || anaStarting) return
  anaStarting = true        // POST 在途、状态还没翻成 running 的窗口里，双击不该再发一条
  try {
    analyzeReq = { id: store.currentId, at: Date.now() }
    await api.analyze(store.currentId)
    await refreshAnalysis()
    if (store.analysis.status === 'done') {
      topPet.rollOnce(); reloadSummary()
      store.viewer.railUser = true
    }
  } finally { anaStarting = false }
}

let margStarting = false
async function doMarginalia() {
  if (!store.currentId || store.marginalia.status === 'running' || margStarting) return
  margStarting = true
  try {
    await api.marginaliaStart(store.currentId)
    await refreshMarginalia()
    startMarginFast()
  } finally { margStarting = false }
}

/* 长任务的「停止」：后端是协作式取消，析读在阶段边界收手（已生成的部分保留），
   全文翻译直接掐 pdf2zh 进程（已译好的页留着，下次接着译）。 */
async function stopAnalyze() {
  try { await api.analysisCancel(store.currentId); toast(t('正在停止…')) } catch { /* 不打扰 */ }
}
async function stopTranslate() {
  try { await api.translateCancel(store.currentId); toast(t('已停止')); await refreshPapers() } catch { /* 同上 */ }
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

let tranStarting = false
async function doTranslateFull() {
  if (!store.currentId || tranSt.value === 'running' || tranStarting) return
  const again = tranSt.value === 'done'
  tranStarting = true
  try {
    const r = await api.translateFull(store.currentId, again)
    tranProg.value = { done: 0, total: 0, svc: r.service || '', started: Date.now() / 1000 | 0, cur: [] }
    await refreshPapers()
    toast(r.note || t('全文翻译已开始'))
  } catch (e) {
    // 缺引擎时后端已经自己在后台装了（多源自动换源+续传+校验）。见到"下载中"就弹等待卡，
    // 装完 onEngineReady 会把这篇的全文翻译自动续上——用户不需要进设置。
    const st = await api.pdf2zhInstallStatus().catch(() => null)
    if (st && ['downloading', 'unpacking', 'warming'].includes(st.state)) {
      engResumeId = store.currentId
      watchEngine()
    } else {
      toast(t('启动失败：{m}', { m: e.message }))
    }
  } finally { tranStarting = false }
}
/* 引擎装好后的自动续翻：只续"因为等引擎而停下"的那一篇——设置页手动装的
 * （没记 engResumeId）只收 toast，不冷不丁替用户开翻译反而吓人。 */
let engResumeId = ''
onEngineReady(() => {
  toast(t('引擎装好了，继续翻译'))
  if (engResumeId && store.currentId === engResumeId) doTranslateFull()
  engResumeId = ''
})

/* 全文翻译的进度：pdf2zh 用 tqdm 打 `11%|██ | 2/18`，后端逐行抠出页数。
   完成这一拍也在这里接——完成通知与「译文/双语」的解锁都看它。 */
async function pollTranslate() {
  if (!store.currentId) return
  const j = await api.translateStatus(store.currentId)
  if (j.pages && j.pages[1]) tranProg.value = { done: j.pages[0], total: j.pages[1], svc: j.service || '', cur: j.current || [], started: tranProg.value.started }
  if (j.status === 'done') {
    tranProg.value = { done: 0, total: 0, svc: '' }
    await refreshPapers()                 // 译文/双语两个按钮看的是 papers 里的 translate_status
    cheerEgg()                            // 全文翻完了：跳两下（析读完成才是滚一圈）
    toast(t('全文翻译完成'))   // 盘上只落译文版，双语首次点开才派生——"双语已生成"是假话
  } else if (j.status === 'error') {
    tranProg.value = { done: 0, total: 0, svc: '' }
    await refreshPapers()
    toast(t('全文翻译失败：{m}', { m: (j.error || '').slice(0, 100) }), 6000)
  }
}

/* 后台篇的全文翻译：人已经转到别的论文上，那条 3 秒轮询只看当前篇——译完悄无声息，
   用户只能反复切回去看。这里替"还在跑"的每篇问一次状态（顺带让后端把孤儿译文认领了），
   完成时补一条通知；庆祝动画仍然只留给在场的这篇。 */
const _bgTran = new Set()
async function bgTranslateTick() {
  for (const p of store.papers.filter(x => x.translate_status === 'running')) {
    if (_bgTran.has(p.id)) continue
    _bgTran.add(p.id)
    try {
      const j = await api.translateStatus(p.id)
      if (j.status === 'done') {
        await refreshPapers()
        if (p.id !== store.currentId) {
          toast(t('《{t}》翻译完成', { t: (p.title || p.filename || '').slice(0, 24) }))
        }
      }
      if (j.status !== 'running') _bgTran.delete(p.id)
    } catch { _bgTran.delete(p.id) }
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
let importing = false
async function onImport(list) {
  const files = (Array.isArray(list) ? list : [list]).filter(f => f && f.name)
  if (!files.length) return
  if (importing) { toast(t('正在导入上一批')) ; return }   // 批量导入是几十秒的串行循环，别让两批交错
  importing = true
  const many = files.length > 1
  const ok = []
  for (let i = 0; i < files.length; i++) {
    const f = files[i]
    toast(many ? t('正在导入 {i}/{n}：{name}', { i: i + 1, n: files.length, name: f.name.slice(0, 24) })
                    : t('已导入，正在后台通读…'))
    try {
      const r = await api.upload(f)
      ok.push(r)
      if (r.duplicate) toast(t('库里已有这篇，直接打开'))
      const c = store.lib.coll
      if (typeof c === 'number') {
        try { await api.paperColls(r.paper.id, [c]); await refreshCollections() } catch { /* 归类失败不影响导入 */ }
      }
      if (r.same_title) await askSupersede(r)
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
  importing = false
  if (!first) return
  if (many && ok.length > 1) {
    toast(t('已导入 {n} 篇，其余在后台排队通读', { n: ok.length }))
  } else if (first.no_text) {
    toast(t('扫描件：后台识别中，完事自动析读'))
  } else if (first.n_paragraphs && first.n_paragraphs < 5) {
    toast(t('只认出 {n} 段，析读会比较粗', { n: first.n_paragraphs }))
  }
}

/* 导入进来的是库里已有论文的另一个版本（arXiv v2、换源重排）：问一次——
   替换旧篇（问答/术语/分类跟迁到新版）还是两篇都留。不做任何默认动作。 */
async function askSupersede(r) {
  const act = await choiceBox({
    title: t('疑似同一篇论文'),
    body: t('《{a}》和库里的《{b}》像是同一篇的不同版本。', {
      a: (r.paper.title || '').slice(0, 40), b: (r.same_title.title || '').slice(0, 40),
    }),
    actions: [
      { key: 'keep', label: t('两篇都保留') },
      { key: 'replace', label: t('替换旧篇') },
    ],
  })
  if (act !== 'replace') return
  const oldId = r.same_title.id
  try {
    const st = await api.supersede(r.paper.id, oldId)
    store.openIds = store.openIds.filter(x => x !== oldId)
    await refreshPapers()
    await refreshCollections()
    toast(st.pins_lost
      ? t('已替换；{n} 条页边卡在新版找不到了', { n: st.pins_lost })
      : t('已替换，问答、术语与分类已迁到新版'))
  } catch (e) { toast(e.message) }
}

async function saveSettings(body) {
  try {
    store.settings = await api.saveSettings(body)
  } catch (e) {
    toast(e.message)      // 没存上：弹窗留着别关，别让用户以为存好了
    return
  }
  showSettings.value = false
  if (store.currentId) refreshAnalysis()
}

const anaBusy = computed(() => ['running', 'queued'].includes(store.analysis.status))
/* 演示模式 = 没配 key 或勾了演示。界面上不标注的话，新用户拿到一手假数据
   还以为是 AI 就这水平——常驻一枚小徽标，点了直达设置。 */
const demoOn = computed(() => !!store.settings && (store.settings.mock || !store.settings.provider?.has_key))
/* 日历图标上的小点：今天已经读过点什么——轻提醒，不弹任何东西 */
const readToday = computed(() => {
  const d = new Date()
  const day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return store.papers.some(p => (p.last_read_at || '').startsWith(day))
})
function titleOf(pid) {
  const p = store.papers.find(x => x.id === pid)
  return p ? (p.title || p.filename) : t('正在打开…')
}
async function onSplitMany(ids) {
  /* 多选出来的同屏组是一次多选的整体结果：先清场再逐篇进窗格，不再是追加语义 */
  store.openIds = []
  for (const pid of ids) {
    const r = await store.addPane(pid)
    if (r?.full) { toast(t('同屏最多 4 篇')); break }
  }
}
const tranSt = computed(() => store.papers.find(x => x.id === store.currentId)?.translate_status || 'none')
const tranProg = ref({ done: 0, total: 0, svc: '', started: 0, cur: [] })
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
  const sec = Math.max(0, Math.round(Date.now() / 1000 - tranProg.value.started))
  return sec >= 90 ? t('{m} 分 {s} 秒', { m: Math.floor(sec / 60), s: sec % 60 }) : t('{s} 秒', { s: sec })
})
const tranLabel = computed(() => {
  if (tranSt.value === 'done') return t('重新全文翻译')
  if (tranSt.value !== 'running') return t('全文翻译')
  const n = tranProg.value.total ? ` ${tranProg.value.done}/${tranProg.value.total}` : '…'
  return t('翻译中') + n
})
const tranTip = computed(() => {
  if (tranSt.value === 'running') {
    // pdf2zh 2.x 不吐实时页进度，后端报的是"正在译哪些批"——至少让用户看见在动
    const cur = tranProg.value.cur?.length
      ? ` ${t('第{p}页', { p: tranProg.value.cur.join('、') })}` : ''
    return t('正在译{svc}{cur} · 已用 {t}', { svc: tranProg.value.svc ? `（${tranProg.value.svc}）` : '',
                                              cur, t: tranElapsed.value || t('刚刚') })
  }
  return ''
})

/* ---------------- 键盘流 ---------------- */
function onKey(e) {
  const t = e.target
  if (dlg.open) { if (e.key === 'Escape') { e.preventDefault(); dlgCancel() } return }
  if (t && (t.matches?.('input, textarea, select') || t.isContentEditable)) return
  if (showSettings.value) {
    if (e.key === 'Escape') { e.preventDefault(); showSettings.value = false }
    return
  }
  if (store.update.show) {
    if (e.key === 'Escape' && !store.update.installing) { e.preventDefault(); store.update = { ...store.update, show: false } }
    return
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') { e.preventDefault(); store.viewerApi?.openSearch(); return }
  if (e.altKey && e.key === 'ArrowLeft') { store.viewerApi?.jumpBack(); e.preventDefault(); return }
  if (e.ctrlKey || e.metaKey || e.altKey) return   // 其余组合键还给浏览器：Ctrl+C 复制不该顺手开截图
  if (e.key === 'Escape') {
    gPending.value = false         // 弦按到一半被 Esc 打断：整条作废
    openDrawer(null)
    store.shortcutCard = false
    store.cite.open = false
    dragOver.value = false
    store.viewer.frame = false     // 框选模式永远能一键退出
    store.escTick++                // PDF 侧的划词/框选/查找浮层收起
    return
  }
  if (gPending.value) {
    gPending.value = false
    if (e.key === 'l') { openDrawer('lib'); e.preventDefault() }
    if (e.key === 'c') { openDrawer('cal'); e.preventDefault() }
    if (e.key === 'o') { openDrawer('toc'); e.preventDefault() }
    if (e.key === 'h') { goHome(); e.preventDefault() }
    return
  }
  /* g 弦必须在「没开论文」的守卫之前注册：书桌正是最需要 g l/g c/g o 的地方，
     挡在后面这组主导航在书桌上就全是死的。 */
  if (e.key === 'g') { gPending.value = true; setTimeout(() => (gPending.value = false), 700); return }
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
    case 'c': store.viewerApi?.startShot(); break
    case '1': store.viewer.variant = 'original'; break
    case '2': if (tranSt.value === 'done') store.viewer.variant = 'mono'; break
    case '3': if (tranSt.value === 'done') store.viewer.variant = 'dual'; break
    case '/': e.preventDefault(); store.askFocusTick++; break
    case 'x': store.viewer.railUser = !store.viewer.railUser; break
    case 'a': doAnalyze(); break
    case 'm': doMarginalia(); break
    case '?': store.shortcutCard = !store.shortcutCard; break
  }
}
</script>

<template>
  <div class="app" :style="{ '--rail-w': store.viewer.railW + 'px', '--lib-w': libWCss() }"
       @dragenter="onDragEnter" @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop">
    <header class="topbar">
      <div class="wordmark" :title="t('回书桌')" @click="goHome">
        <span class="egg-wrap" :title="t('彩蛋')"
              :ref="r => (homeEl = r)" @pointerdown="petDown" @click.stop="petClick" @wheel="topPet.wheel"
              @dragenter="topPet.enter" @dragover="topPet.over" @dragleave="topPet.leave" @drop="topPet.drop">
          <EggMark :skin="eggSkin" class="egg pet" v-if="!petPos && !dragFloat" :class="petCls(topPet)" :style="spinStyle(topPet)" />
          <span class="egg-z" v-if="sleepEgg && !petPos && !dragFloat" aria-hidden="true"><i>z</i><i>z</i></span>
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
                <button class="toggle" :class="{ on: store.viewer.frame }" :title="t('框选问 AI')"
                @click="store.viewer.frame = !store.viewer.frame">{{ t('框选') }}</button>
                        <button v-if="!isEn()" @click="doTranslateFull"
                :disabled="tranSt === 'running' || tranStarting"
                :title="tranTip">
          {{ tranLabel }}
        </button>
        <button v-if="tranSt === 'running'" class="ghost" @click="stopTranslate"
                :title="t('已译好的页会保留')">{{ t('停止') }}</button>
        <button class="primary" @click="doAnalyze" :disabled="anaBusy">
          {{ store.analysis.status === 'queued' ? t('排队中…') : (store.analysis.status === 'running' ? t('通读中…')
             : (store.analysis.status === 'done' ? t('重新析读') : t('析读'))) }}
        </button>
        <button v-if="anaBusy" class="ghost" @click="stopAnalyze"
                :title="t('已生成的部分会保留')">{{ t('停止') }}</button>
      </div>
      <div class="actions">
        <button class="demo-badge" v-if="demoOn" @click="showSettings = true"
                :title="t('演示数据')">{{ t('演示模式') }}</button>
        <button class="ghost" @click="showSettings = true" :title="t('设置')">⚙</button>
      </div>
            <div class="tran-line" v-if="tranSt === 'running' && !isEn()">
        <i :class="{ det: tranPct > 0 }" :style="tranPct > 0 ? { width: tranPct + '%' } : null"></i>
      </div>
    </header>

    <div class="main">
            <div class="left-strip">
        <button class="strip-btn" :class="{ on: store.viewer.libOpen }" :title="t('文库')"
                @click="toggleLib">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M4 4h6v16H4zM14 4h6v16h-6z" />
            <path d="M7 8h.01M7 12h.01M17 8h.01M17 12h.01" stroke-linecap="round" stroke-width="2.4" />
          </svg>
          <span class="badge" v-if="store.papers.length">{{ store.papers.length }}</span>
        </button>
        <button class="strip-btn" :class="{ on: store.viewer.calOpen }" :title="t('论文日历')"
                @click="toggleCal">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <rect x="4" y="5.5" width="16" height="14.5" rx="1.5" />
            <path d="M4 10.5h16M8.5 3.5v3.5M15.5 3.5v3.5" stroke-linecap="round" />
          </svg>
          <i class="strip-dot" v-if="readToday && !store.viewer.calOpen"></i>
        </button>
        <button class="strip-btn" :class="{ on: store.viewer.tocOpen }" :title="t('目录')"
                @click="toggleToc">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
            <path d="M5 6h14M5 11.5h9M5 17h5M17.5 14.5v6M14.5 17.5h6" />
          </svg>
        </button>
        <div class="strip-sep"></div>
      </div>

            <main class="panes" v-if="store.openIds.length > 1" :class="'cols' + store.openIds.length">
        <TransitionGroup name="panein">
        <section v-for="(pid, i) in store.openIds" :key="pid" class="pane"
                 :class="{ active: pid === store.currentId }"
                 @pointerdown="pid !== store.currentId && store.activatePaper(pid, false)">
          <div class="pane-head">
            <span class="p-t" :title="titleOf(pid)">{{ titleOf(pid) }}</span>
            <button class="p-x" :title="t('关闭这篇')" @click.stop="store.closePane(i)">×</button>
          </div>
          <div class="desk pane-body">
            <PdfViewer :pid="pid" :key="pid" />
          </div>
        </section>
        </TransitionGroup>
      </main>
            <main class="desk" v-else>
                <div class="empty" v-if="!store.paper">
          <span class="egg-wrap" :title="t('彩蛋')" :ref="r => (deskPet.el = r)" @click.stop="deskPet.poke" @wheel="deskPet.wheel"
                @dragenter="deskPet.enter" @dragover="deskPet.over" @dragleave="deskPet.leave" @drop="deskPet.drop">
            <EggMark :skin="eggSkin" class="egg-big pet" :class="petCls(deskPet, { hop: dragOver })" :style="spinStyle(deskPet)" />
            <span class="egg-z" v-if="sleepEgg" aria-hidden="true"><i>z</i><i>z</i></span>
          </span>
          <div class="e-title" role="button" tabindex="0" @click="pickFiles"
               @keydown.enter.prevent="pickFiles" @keydown.space.prevent="pickFiles">{{ t('论文，启动！') }}</div>
          <div class="stamp" role="button" tabindex="0" @click="pickFiles"
               @keydown.enter.prevent="pickFiles" @keydown.space.prevent="pickFiles">EGGPAPER · LOCAL-FIRST</div>
          <div class="desk-hint" role="button" tabindex="0" @click="pickFiles"
               @keydown.enter.prevent="pickFiles" @keydown.space.prevent="pickFiles">{{ t('拖入 PDF，或点击选择文件') }}</div>
          <div class="desk-hint demo-hint" v-if="demoOn" role="button" tabindex="0"
               @click="showSettings = true" @keydown.enter.prevent="showSettings = true"
               @keydown.space.prevent="showSettings = true">
            {{ t('去设置配好模型') }}
          </div>
        </div>
        <PdfViewer v-else :pid="store.currentId" :key="store.currentId" />
      </main>

            <Transition name="fade">
              <button class="rail-tab" v-if="store.paper && !store.railRight" :title="t('展开右栏')"
                @click="store.viewer.railUser = true" aria-label="展开右栏"></button>
            </Transition>
            <div class="rail-wrap" v-if="store.paper" :class="{ collapsed: !store.railRight, overlay: store.railOverlay }">
        <RightRail @analyze="doAnalyze" @marginalia="doMarginalia" />
      </div>
    </div>

    <Transition name="fade">
      <div class="lib-mask" v-if="store.viewer.libOpen || store.viewer.calOpen || store.viewer.tocOpen"
           @click="openDrawer(null)"></div>
    </Transition>
    <Transition name="slide-l">
      <LibPanel v-if="store.viewer.libOpen" @import="onImport" @split-many="onSplitMany" @close="store.viewer.libOpen = false" />
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
    <Transition name="pop">
      <div class="eng-card" v-if="engInst.on && !engInst.hidden">
        <div class="eng-title">
          <span v-if="engInst.state === 'downloading'">{{ t('正在下载全文翻译引擎 {p}%', { p: engInst.pct }) }}</span>
          <span v-else-if="engInst.state === 'unpacking'">{{ t('引擎解压安装中…') }}</span>
          <span v-else-if="engInst.state === 'warming'">{{ t('引擎预热中…') }}</span>
          <span v-else-if="engInst.state === 'error'" style="color:var(--vermilion)">{{ engInst.error }}</span>
        </div>
        <div class="eng-track" v-if="engInst.state === 'downloading'"><i :style="{ width: engInst.pct + '%' }"></i></div>
        <div class="eng-sub" v-if="engInst.state === 'downloading'">
          {{ t('已下载 {a} / {b} MB（{s}）', { a: fmtMB(engInst.got), b: fmtMB(engInst.total), s: engInst.src }) }}
        </div>
        <div class="eng-sub" v-else-if="engInst.state === 'unpacking' || engInst.state === 'warming'">{{ t('装好后自动开始全文翻译') }}</div>
        <div class="eng-btns">
          <template v-if="engInst.state === 'error'">
            <button @click="startEngineInstall">{{ t('重试') }}</button>
            <button @click="closeEngineCard">{{ t('关闭') }}</button>
          </template>
          <template v-else>
            <button @click="cancelEngineInstall">{{ t('停止下载') }}</button>
            <button style="margin-left:auto" @click="hideEngineCard">{{ t('收起') }}</button>
          </template>
        </div>
      </div>
    </Transition>
        <div class="quit-mask" v-if="quitMask">{{ t('eggpaper 已退出，可以关掉这个页面') }}</div>
        <input ref="appFile" type="file" accept="application/pdf" multiple hidden @change="onAppFile" />

        <Transition name="pop">
    <div class="keys-card" v-if="store.shortcutCard" @click="store.shortcutCard = false">
      <div class="mono-label" style="margin-bottom:8px">{{ t('键盘') }}</div>
      <div class="k-row"><span>{{ t('下一段 / 上一段') }}</span><kbd>j / k</kbd></div>
      <div class="k-row"><span>{{ t('翻页') }}</span><kbd>PageUp / PageDown</kbd></div>
      <div class="k-row"><span>{{ t('查找') }}</span><kbd>Ctrl + F</kbd></div>
      <div class="k-row" v-if="!isEn()"><span>{{ t('译当前段并钉页边') }}</span><kbd>t</kbd></div>
      <div class="k-row" v-if="!isEn()"><span>{{ t('翻译划选') }}</span><kbd>s</kbd></div>
      <div class="k-row"><span>{{ t('框选问 AI（Esc 退出）') }}</span><kbd>r</kbd></div>
      <div class="k-row"><span>{{ t('截图') }}</span><kbd>c</kbd></div>
      <div class="k-row" v-if="!isEn()"><span>{{ t('原文 / 译文 / 双语') }}</span><kbd>1 / 2 / 3</kbd></div>
      <div class="k-row"><span>{{ t('聚焦提问') }}</span><kbd>/</kbd></div>
      <div class="k-row"><span>{{ t('折叠右栏') }}</span><kbd>x</kbd></div>
      <div class="k-row" v-if="!isEn()"><span>{{ t('析读') }}</span><kbd>a</kbd></div>
      <div class="k-row" v-if="!isEn()"><span>{{ t('AI 眉批') }}</span><kbd>m</kbd></div>
      <div class="k-row"><span>{{ t('文库 / 日历 / 目录') }}</span><kbd>g l / g c / g o</kbd></div>
      <div class="k-row"><span>{{ t('回书桌') }}</span><kbd>g h</kbd></div>
      <div class="k-row"><span>{{ t('返回原位') }}</span><kbd>Alt + ←</kbd></div>
      <div class="k-row"><span>{{ t('收起所有浮层 / 退出框选') }}</span><kbd>Esc</kbd></div>
      <div class="k-row"><span>{{ t('这张卡') }}</span><kbd>?</kbd></div>
    </div>
    </Transition>

    <Transition name="pop">
      <div class="toast" v-if="store.toast" :key="store.toastN">{{ store.toast }}</div>
    </Transition>
        <Transition name="greet">
          <div class="greet-box" v-if="greetShow">
            <i class="gtl"></i><i class="gtr"></i><i class="gbl"></i><i class="gbr"></i>
            <span class="gt">{{ greetText }}</span>
          </div>
        </Transition>
        <i class="feed-ghost" v-for="g in ghosts" :key="g.id" aria-hidden="true"
       :style="{ left: g.x + 'px', top: g.y + 'px', '--dx': (g.tx - g.x) + 'px', '--dy': (g.ty - g.y) + 'px', animationDelay: g.delay + 'ms' }" />
    <!-- 浮动的蛋：长按抓起来安到哪算哪，拖回左上角原位附近自动归位 -->
    <span class="egg-float" v-if="petPos || dragFloat" :class="{ drag: dragFloat }" :title="t('彩蛋')"
          :style="petPos ? { left: petPos.x + 'px', top: petPos.y + 'px' } : null"
          :ref="r => (topPet.el = r)" @pointerdown="petDown" @click.stop="petClick"
          @wheel="topPet.wheel"
          @dragenter="topPet.enter" @dragover="topPet.over" @dragleave="topPet.leave" @drop="topPet.drop">
      <EggMark :skin="eggSkin" class="egg pet" :class="petCls(topPet)" :style="spinStyle(topPet)" />
      <span class="egg-z" v-if="sleepEgg" aria-hidden="true"><i>z</i><i>z</i></span>
    </span>
    <Transition name="fade">
    <div class="modal-mask" v-if="dragOver && store.paper" style="pointer-events:none; background:rgba(var(--wash-rgb), .22)">
      <div class="modal" style="text-align:center">
        <div style="font-size:var(--fs-xl);font-weight:650">{{ t('松手，放到书桌上') }}</div>
      </div>
    </div>
    </Transition>
  </div>
</template>
