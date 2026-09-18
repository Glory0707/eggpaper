<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, store, toast, refreshPapers, refreshCollections, openPaper, refreshAnalysis,
         refreshMarginalia, reloadSummary, checkUpdate, loadVersion } from './store'
import PdfViewer from './components/PdfViewer.vue'
import LibPanel from './components/LeftRail.vue'
import RightRail from './components/RightRail.vue'
import SettingsModal from './components/SettingsModal.vue'
import Dialog from './components/Dialog.vue'
import CiteCard from './components/CiteCard.vue'
import UpdateCard from './components/UpdateCard.vue'
import { dlg, dlgCancel } from './dialog'
import EggMark from './components/EggMark.vue'

const showSettings = ref(false)
const dragOver = ref(false)
const roll = ref(false)          // 完成一个动作时，印章滚一下（借蛋仔的"动作"，不借它的配色）
const gPending = ref(false)
const VARIANTS = ['original', 'mono', 'dual']
let pollTimer = null
let marginFastTimer = null     // 眉批运行时那条 1 秒快轮询（跑完即停）
let _lastErrToast = { msg: '', at: 0 }   // 全局兜底报错的限流记号

function rollOnce(ms = 700) {
  roll.value = false
  requestAnimationFrame(() => { roll.value = true })
  clearTimeout(rollOnce._t)
  rollOnce._t = setTimeout(() => (roll.value = false), ms)
}

/* 戳一戳蛋：点一下晃一下；3 秒内戳满三下翻滚一圈。无声——戳了会动，仅此而已。
   活物有破绽：24 下里有 1 下不是常规晃动，是打喷嚏或打趔趄——不预告、不收集，
   只能被撞见。 */
const wobbling = ref(false)
function wobbleOnce(ms = 450) {
  wobbling.value = false
  requestAnimationFrame(() => { wobbling.value = true })
  clearTimeout(wobbleOnce._t)
  wobbleOnce._t = setTimeout(() => (wobbling.value = false), ms)
}
const surprise = ref('')        // '' | 'sneeze' | 'stumble'
function surpriseOnce(kind) {
  surprise.value = ''
  requestAnimationFrame(() => { surprise.value = kind })
  clearTimeout(surpriseOnce._t)
  surpriseOnce._t = setTimeout(() => (surprise.value = ''), kind === 'sneeze' ? 650 : 1000)
}
let pokes = 0
let pokeReset = 0
function pokeEgg() {
  pokes++
  clearTimeout(pokeReset)
  if (pokes >= 3) {
    pokes = 0
    rollOnce()
    return
  }
  pokeReset = setTimeout(() => (pokes = 0), 3000)
  if (Math.random() < 1 / 24) {
    surpriseOnce(Math.random() < 0.5 ? 'sneeze' : 'stumble')
    return
  }
  wobbleOnce()
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
    wobbleOnce()
  }
}

/* 滚轮搓蛋：光标压在蛋上滚滚轮，它跟着转；停手一拍后带着弹性自己摆回正。
   转角始终折进 ±180°——回正永远走最近的那半圈，不会解开一麻花再回来。 */
const spinDeg = ref(0)
const spinning = ref(false)     // 正被搓着：transform-origin 才落到支点上
const spinFree = ref(false)     // 停手回正的这一段才挂弹性过渡
let spinTimer = 0
function onEggWheel(e) {
  if (e.ctrlKey) return         // Ctrl+滚轮是页面缩放，不抢浏览器的
  e.preventDefault()
  spinFree.value = false
  spinning.value = true
  spinDeg.value = ((spinDeg.value + e.deltaY * 0.18 + 180) % 360 + 360) % 360 - 180
  clearTimeout(spinTimer)
  spinTimer = setTimeout(() => {
    spinFree.value = true
    spinDeg.value = 0
    setTimeout(() => { spinning.value = spinFree.value = false }, 700)
  }, 160)
}

/* 喂蛋：PDF 精准丢在左上角蛋身上=它吃掉——论文缩成小纸片翻着跟头飞进蛋里，
   鼓一下咽下（一次三篇起改演「撑到」）。导入照旧走 onImport：吃只是仪式，活照干；
   拖到窗口其他地方仍是老样子，两条路互不打扰。 */
const hungry = ref(false)       // 有纸悬在头顶：等饭的小幅急摆
const gulping = ref('')         // '' | 'gulp' | 'stuff'
const ghosts = ref([])          // 飞行途中的纸片
const eggEl = ref(null)
let ghostId = 0
function eggDragEnter(e) { if (hasFiles(e)) hungry.value = true }
function eggDragOver(e) { if (hasFiles(e)) e.preventDefault() }
function eggDragLeave(e) {
  if (hasFiles(e) && !e.currentTarget.contains(e.relatedTarget)) hungry.value = false
}
function onEggDrop(e) {
  if (!hasFiles(e)) return          // 拖文字之类的不归它管，照走默认
  e.preventDefault()
  e.stopPropagation()               // 别再冒给 .app 的 onDrop——一次导入只做一遍
  hungry.value = false
  dragOver.value = false            // .app 的 onDrop 收不到这一拍了，浮层自己收
  const files = Array.from(e.dataTransfer?.files || []).filter(f => f && f.name)
  if (!files.length) return
  feedShow(e.clientX, e.clientY, files.length)
  onImport(files)
}
function feedShow(x, y, n) {
  if (sleepEgg.value) return    // 它睡着了：饭照收（导入照跑），仪式免了
  const r = eggEl.value?.getBoundingClientRect?.()
  if (!r) return
  const tx = r.left + r.width / 2
  const ty = r.top + r.height / 2
  ghosts.value = Array.from({ length: Math.min(n, 4) }, (_, i) => ({
    id: ++ghostId,
    x: x + (i ? Math.random() * 36 - 18 : 0),
    y: y + (i ? Math.random() * 24 - 12 : 0),
    tx, ty,
    delay: i * 90,
  }))
  clearTimeout(feedShow._t)
  feedShow._t = setTimeout(() => {
    ghosts.value = []
    gulpOnce(n >= 3 ? 'stuff' : 'gulp')
  }, 420 + (Math.min(n, 4) - 1) * 90)
}
function gulpOnce(kind) {
  gulping.value = ''
  requestAnimationFrame(() => { gulping.value = kind })
  clearTimeout(gulpOnce._t)
  gulpOnce._t = setTimeout(() => (gulping.value = ''), kind === 'stuff' ? 1350 : 850)
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
  onImport(Array.from(e.dataTransfer?.files || []))     // 一次拖进来的全收下
}
function endDrag() { dragOver.value = false; hungry.value = false }   // 中途在窗口外松手也得收：否则蛋一直馋着

onMounted(async () => {
  // 监听与轮询**先装上**：它们不该依赖任何一次网络请求成功。
  // 原来这一串 await 是连着的，第一个（/api/settings）一失败，后面全不执行——
  // 文库不加载、3 秒轮询不开（双击打开 PDF、翻译进度、析读状态全哑）、键盘也没挂上，
  // 界面停在"论文，启动！"却一个字都不解释；此时点设置还会因为 settings 是 null 直接报错。
  window.addEventListener('keydown', onKey)
  window.addEventListener('dragend', endDrag)
  window.addEventListener('blur', endDrag)
  // 兜底的最后一道网：哪条链路漏了 catch，也别静默死掉——报给用户（限流：同一句
  // 30 秒内只报一次，轮询类的重复失败不刷屏）。拦过之后控制台里照样能看全栈。
  window.addEventListener('unhandledrejection', ev => {
    const msg = String(ev.reason?.message || ev.reason || '未知错误')
    const now = Date.now()
    if (msg === _lastErrToast.msg && now - _lastErrToast.at < 30000) return
    _lastErrToast = { msg, at: now }
    console.error('[eggpaper] 未处理的失败：', ev.reason)
    toast('出错了：' + msg.slice(0, 120), 5000)
  })
  pollTimer = setInterval(poll, 3000)
  sleepGreet()               // 深夜开着 eggpaper：蛋先睡下，问候随后
  // 跨进/跨出深夜那一拍的检查搭轮询的车（3 秒一次足够），不单开计时器
  // 发呆打盹的「动」：键、鼠、滚轮随便哪个都算
  window.addEventListener('pointermove', wakeEgg, { passive: true })
  window.addEventListener('pointerdown', wakeEgg, { passive: true })
  window.addEventListener('keydown', wakeEgg)
  window.addEventListener('wheel', wakeEgg, { passive: true })
  wakeEgg()                  // 先把打盹的表立起来
  try {
    store.settings = await api.settings()
    await refreshPapers()
    await refreshCollections()
    if (store.papers.length) openPaper(store.papers[0].id)
  } catch (e) {
    toast('初始化失败：' + e.message + '（后台可能刚起来，稍后会自动恢复）', 6000)
  }
  // 更新：先问自己是哪个版本，再等 6 秒做一次安静探测。故意不抢首屏——
  // 用户先看到论文，更新提示随后自己浮出来；源里没东西就什么都不会发生。
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
  // "双击 PDF / 右键用它打开"：导入是在后端做的，界面这边只是被通知切过去。
  // 挂在原来这个 3 秒轮询上——为一次打开请求新起一条轮询不值得。
  try {
    const r = await api.openRequest()
    if (r?.quitting) { pageQuit(); return }
    if (r?.pid && r.pid !== store.currentId) {
      await refreshPapers()
      await openPaper(r.pid)
    }
  } catch { /* 轮询里的失败不打扰用户 */ }
  // 每 15 秒问一次后端版本（5 拍 × 3 秒）：对不上就说明软件被升级过，而这个标签页
  // 跑的还是升级前的界面——**版本号会变、界面不会变**（用户看到的正是这个：
  // "设置里的版本号更新了，但其他修改没生效"）。刷新是无损的（阅读位置存在本地），
  // 所以直接替他刷，别让他自己去想"为什么没生效"。
  if (bootVersion && !verHintShown && ++verTick % 5 === 0) {
    try {
      const v = await api.version()
      if (v.version && v.version !== bootVersion) {
        verHintShown = true
        toast(`eggpaper 已更新到 ${v.version}，正在刷新界面…`, 6000)
        setTimeout(() => window.location.reload(), 1200)
      }
    } catch { /* 下个 15 秒再问 */ }
  }
  if (!store.currentId) return
  // 状态刷新各自兜住：后端正在重启/瞬时失败时，轮询本身不能死——
  // 死了的话翻译进度、析读状态就永远不更新了，看起来像"卡住"
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
      toast('蛋都睡了，你还在读。', 8000)
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
  // 析读把一眼卡一起作废了（服务端清了缓存），所以这里要重新取一次。
  // 写成"进 done"而不是"running→done"：现在中间还多一个 queued（排队），
  // 只认 running→done 会在"排队→读完"这条路径上漏掉这一拍。
  // 'none' 不算：那只是初值——打开一篇早就析读完的论文也会走出 none→done，
  // 那时什么都没完成，不该滚一圈。
  if (n === 'done' && o && o !== 'done' && o !== 'none') {
    rollOnce(); reloadSummary()
    // 析读的产出全在右栏（骨架、五问、一眼卡）：这一局真的跑完了就把它展开，
    // 别让用户读完再去找那颗 ◂。只在**这一局是本会话发起/见过在跑**时动手——
    // 打开一篇早就析读完的论文不算，用户特意收起的右栏不该每次换篇都被强行撑开。
    const mine = analyzeReq.id === store.currentId && Date.now() - analyzeReq.at < 600000
    if (o === 'running' || o === 'queued' || mine) store.viewer.railUser = true
  }
})

async function doAnalyze() {
  if (!store.currentId) return
  analyzeReq = { id: store.currentId, at: Date.now() }
  await api.analyze(store.currentId)
  await refreshAnalysis()
  // 演示模式的析读是秒完的：POST 回来再拉状态就已经是 done（前一拍也是 done），
  // 上面的 watch 看不到"进 done"这一拍，这里补上同样的收尾（重复执行无害）。
  if (store.analysis.status === 'done') {
    rollOnce(); reloadSummary()
    store.viewer.railUser = true
  }
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
  // 译过一次也允许再来：有的译文打不开（文件写坏/服务抽风），用户要的就是"重译一遍"
  const again = tranSt.value === 'done'
  try {
    const r = await api.translateFull(store.currentId, again)
    tranProg.value = { done: 0, total: 0, svc: r.service || '', started: Date.now() / 1000 | 0 }
    await refreshPapers()
    // 服务被自动换掉（比如 google 在这台机器的网络下不通）要说出来——
    // 用户设的是 google、跑的是 bing，不吭声等于骗人
    toast(r.note || (again ? '已开始重新整本翻译' : '整本翻译已开始'))
  } catch (e) { toast('启动失败：' + e.message) }
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
    toast('整本翻译完成')   // 盘上只落译文版，双语首次点开才派生——"双语已生成"是假话
  } else if (j.status === 'error') {
    tranProg.value = { done: 0, total: 0, svc: '' }
    await refreshPapers()
    toast('整本翻译失败：' + (j.error || '').slice(0, 100), 6000)
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
    toast(many ? `正在导入 ${i + 1}/${files.length}：${f.name.slice(0, 24)}` : '已导入，正在后台通读…')
    try {
      const r = await api.upload(f)
      ok.push(r)
      if (r.duplicate) toast('库里已有这篇——直接打开原来那份')
      // 正在看某个分类时导入的，就顺手归到那个分类里——Zotero 的"导入到分类"一个意思
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
      toast(`《${f.name.slice(0, 20)}》导入失败：` + e.message)
    }
  }
  const first = ok[0]
  if (!first) return
  if (many && ok.length > 1) {
    toast(`已导入 ${ok.length} 篇，其余在后台排队通读`)
  } else if (first.no_text) {
    toast('扫描件：只能读，析读与眉批用不了')
  } else if (first.n_paragraphs && first.n_paragraphs < 5) {
    toast('只认出 ' + first.n_paragraphs + ' 段，析读会比较粗')
  }
}

async function saveSettings(body) {
  store.settings = await api.saveSettings(body)
  showSettings.value = false
  if (store.currentId) refreshAnalysis()
}

// 析读在忙：排队与在读都算（后台排队时按钮也该按不动、并说清是在排队）
const anaBusy = computed(() => ['running', 'queued'].includes(store.analysis.status))
const tranSt = computed(() => store.papers.find(x => x.id === store.currentId)?.translate_status || 'none')
// 整本翻译的进度（回填自 /translate-status 的 pages）。total 为 0 = 还没解析出页数
const tranProg = ref({ done: 0, total: 0, svc: '', started: 0 })
const tranPct = computed(() => tranProg.value.total
  ? Math.round(tranProg.value.done * 100 / tranProg.value.total) : 0)
// 已用时：页与页之间可能隔好久（服务限流会自动重试），把"在走"明明白白写给用户看
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
  return t >= 90 ? `${Math.floor(t / 60)} 分 ${t % 60} 秒` : `${t} 秒`
})
const tranLabel = computed(() => {
  if (tranSt.value === 'done') return '重新整本翻译'
  if (tranSt.value !== 'running') return '整本翻译'
  const n = tranProg.value.total ? ` ${tranProg.value.done}/${tranProg.value.total}` : '…'
  return `翻译中${n}`
})
const tranTip = computed(() => {
  if (tranSt.value === 'running') {
    return `正在译${tranProg.value.svc ? '（' + tranProg.value.svc + '）' : ''}`
         + ` · 已用 ${tranElapsed.value || '刚刚'}`
  }
  return '译出第二份 PDF，供「译文 / 双语」'
})

/* ---------------- 键盘流 ---------------- */
function onKey(e) {
  const t = e.target
  // 对话框是最上面一层：Esc 先关它，别的浮层这一拍都别动
  // （否则"删分类"弹窗开着按 Esc，会把整个文库也一起收掉）
  if (dlg.open) { if (e.key === 'Escape') { e.preventDefault(); dlgCancel() } return }
  if (t && (t.matches?.('input, textarea, select') || t.isContentEditable)) return
  // 设置弹窗开着就**一个键都不接**（含 Esc，它按设计只有 × 和「保存」两个出口）：
  // 焦点在弹窗按钮上时按 t / 2 / r 会在弹窗背后真的去翻译、切变体、进框选。
  if (showSettings.value) return
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') { e.preventDefault(); store.viewerApi?.openSearch(); return }
  if (e.altKey && e.key === 'ArrowLeft') { store.viewerApi?.jumpBack(); e.preventDefault(); return }
  if (e.key === 'Escape') {
    store.viewer.libOpen = false
    store.shortcutCard = false
    store.cite.open = false
    // 设置**不**在 Esc 里关：里面可能填了一半（base_url / key / 模型号），
    // 一键关掉就把输入丢了。出口只有右上角 × 和「保存」（用户明确要求）。
    dragOver.value = false
    store.viewer.frame = false     // 框选模式永远能一键退出
    store.escTick++                // PDF 侧的划词/框选/查找浮层收起
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
    case 'r': store.viewer.frame = !store.viewer.frame; break
    case 'f': store.viewer.layers.skim = !store.viewer.layers.skim; break
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
      <div class="wordmark">
        <span class="egg-wrap" ref="eggEl" @click="pokeEgg" @wheel="onEggWheel"
              @dragenter="eggDragEnter" @dragover="eggDragOver" @dragleave="eggDragLeave" @drop="onEggDrop">
          <EggMark class="egg"
            :class="[{ hungry }, { roll }, { wobble: wobbling }, surprise, { sleep: sleepEgg }, { busy: eggBusy }, gulping, { cheer: eggCheer }, { doze: dozing }, { spun: spinning }, { free: spinFree }]"
            :style="spinning ? { transform: `rotate(${spinDeg}deg) scale(${spinFree ? 1 : 0.94})` } : null" />
          <span class="egg-z" v-if="sleepEgg" aria-hidden="true"><i>z</i><i>z</i></span>
        </span>
        <span class="name">eggpaper</span>
      </div>
      <div class="doc-head" v-if="store.paper">
        <div class="t-row">
          <div class="t">{{ store.paper.title || store.paper.filename }}</div>
          <!-- 引用格式是这篇的身份信息，跟标题同一族数据 → 就挂在标题旁边。
               任何页签下都够得着，不占右栏那四栏的版面 -->
          <button class="cite-btn" @click="store.cite.open = true">引用</button>
        </div>
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
        <!-- 略读：只蒙不用细读的正文，图与图注永不蒙；悬停掀开，点一下=这段也要读 -->
        <button class="toggle" :class="{ on: store.viewer.layers.skim }"
                title="略读（f）"
                @click="store.viewer.layers.skim = !store.viewer.layers.skim">略读</button>
        <button class="toggle" :class="{ on: store.viewer.frame }" title="框选问 AI（r）"
                @click="store.viewer.frame = !store.viewer.frame">框选</button>
        <!-- 整本翻译：把 PDF 整篇译成第二份文档（奇页原文偶页译文），译文/双语两个模式靠它。
             译完就没必要再露出来了——留一个永远点不动的按钮只会让人猜它还能干什么。 -->
        <!-- 整本翻译常驻：译过一次也要能再来（有的译文打不开，重译一遍就好）。
             译完后的按钮是「重新整本翻译」，点了会覆盖现有译文重译。 -->
        <button @click="doTranslateFull" :disabled="tranSt === 'running'"
                :title="tranTip">
          {{ tranLabel }}
        </button>
        <button class="primary" @click="doAnalyze" :disabled="anaBusy">
          {{ store.analysis.status === 'queued' ? '排队中…' : (store.analysis.status === 'running' ? '通读中…'
             : (store.analysis.status === 'done' ? '重新析读' : '析读')) }}
        </button>
      </div>
      <div class="actions">
        <button class="ghost" @click="showSettings = true" title="设置">⚙</button>
      </div>
      <!-- 整本翻译的进度：一条发丝墨线压在工具栏下沿，译完/失败自己消失。
           它是唯一要跑分钟级的活（这篇 18 页实测 2 分钟），不给点动静用户只会以为卡了。 -->
      <div class="tran-line" v-if="tranSt === 'running'">
        <i :class="{ det: tranPct > 0 }" :style="tranPct > 0 ? { width: tranPct + '%' } : null"></i>
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
        <!-- 空态整屏都可以点：第一次打开软件时，用户面对的就是这一屏，
             "拖进来"三个字只交代了一半——手边没有拖拽习惯的人会去点它。
             所以整块都能点开文件选择，键盘（Enter/Space）与拖入同样有效。 -->
        <div class="empty" v-if="!store.paper" role="button" tabindex="0"
             @click="pickFiles" @keydown.enter.prevent="pickFiles" @keydown.space.prevent="pickFiles">
          <EggMark class="egg-big" :class="{ hop: dragOver }" />
          <div class="e-title">论文，启动！</div>
          <div class="e-sub">把 PDF 拖进来，或<b>点这里选择文件</b></div>
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
      <LibPanel v-if="store.viewer.libOpen" @import="onImport" @close="store.viewer.libOpen = false" />
    </Transition>
    <Transition name="fade">
      <SettingsModal v-if="showSettings" @close="showSettings = false" @save="saveSettings" @quit="onQuitApp" />
    </Transition>
    <!-- 全局唯一的应用内对话框：别处 await confirmBox / inputBox 就行。
         别放进上面那个 Transition——Transition 只允许一个子节点，多一个就编译不过 -->
    <Dialog />
    <CiteCard />
    <UpdateCard />
    <!-- 退出后的兜底：普通浏览器标签页浏览器不许脚本关，亮一层遮罩别让用户对死页面发愣 -->
    <div class="quit-mask" v-if="quitMask">eggpaper 已退出，这个页面可以关掉了。</div>
    <!-- 应用级文件选择：空态整屏可点、键盘也能用（多选：一次导入多篇） -->
    <input ref="appFile" type="file" accept="application/pdf" multiple hidden @change="onAppFile" />

    <!-- 键盘卡 -->
    <Transition name="pop">
    <div class="keys-card" v-if="store.shortcutCard" @click="store.shortcutCard = false">
      <div class="mono-label" style="margin-bottom:8px">键盘</div>
      <div class="k-row"><span>略读开 / 关</span><kbd>f</kbd></div>
      <div class="k-row"><span>下一段 / 上一段（略读时仅核心段）</span><kbd>j / k</kbd></div>
      <div class="k-row"><span>译当前段并钉页边</span><kbd>t</kbd></div>
      <div class="k-row"><span>翻译划选</span><kbd>s</kbd></div>
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
    <!-- 喂蛋的纸片：从松手的位置翻着跟头飞进蛋里（fixed 定位，挂在哪层都行） -->
    <i class="feed-ghost" v-for="g in ghosts" :key="g.id" aria-hidden="true"
       :style="{ left: g.x + 'px', top: g.y + 'px', '--dx': (g.tx - g.x) + 'px', '--dy': (g.ty - g.y) + 'px', animationDelay: g.delay + 'ms' }" />
    <Transition name="fade">
    <div class="modal-mask" v-if="dragOver && store.paper" style="pointer-events:none; background:rgba(29,27,23,.22)">
      <div class="modal" style="text-align:center">
        <div style="font-size:var(--fs-xl);font-weight:650">松手，放到书桌上</div>
      </div>
    </div>
    </Transition>
  </div>
</template>
