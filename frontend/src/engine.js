/* 全文翻译引擎的安装状态：全局唯一一份 reactive + 唯一一条轮询。
 *
 * 为什么抽出来：安装可以从两个地方发起（点「全文翻译」时的自动安装、设置页的手动安装），
 * 但状态必须处处一致——设置页的引擎行、右下角的等待卡、localStorage 的 engState 缓存，
 * 看到的都是同一份。谁先发起都行，轮询只会有一条（timer 幂等）。
 *
 * 装好的收尾也在这里统一做：探一次引擎把 engState 缓存刷掉，再挨个通知 doneHooks
 * （App 的等待卡用它自动续发全文翻译，设置页用它把状态行刷成「可用」）。
 */
import { reactive } from 'vue'
import { api } from './api'

export const engInst = reactive({
  on: false,          // 等待卡要不要显示（error 时也显示，给重试/关闭）
  hidden: false,      // 用户点了「后台」：卡收起，轮询继续
  state: 'idle',      // idle | downloading | unpacking | done | error
  pct: 0, got: 0, total: 0,
  src: '',            // 正在用的源（更新源/镜像/官方直连/本地文件）
  error: '',
})

let timer = null
const doneHooks = []
const errorHooks = []

export function onEngineReady(fn) { doneHooks.push(fn) }
export function onEngineError(fn) { errorHooks.push(fn) }

function stopPolling() {
  if (timer) { clearInterval(timer); timer = null }
}

async function poll() {
  let s
  try {
    s = await api.pdf2zhInstallStatus()
  } catch { return /* 下一拍再问 */ }
  Object.assign(engInst, {
    state: s.state, pct: s.pct || 0, got: s.got || 0, total: s.total || 0,
    src: s.src || '', error: s.error || '',
  })
  if (s.state === 'done') {
    stopPolling()
    engInst.on = false
    engInst.hidden = false
    try {   // 刷一遍引擎缓存（localStorage 的 engState），设置页下次打开不用闪"未安装"
      const e = await api.pdf2zhEngine(s.path || '')
      localStorage.setItem('engState', JSON.stringify({ ok: e.ok, path: e.path, why: e.why }))
    } catch { /* 探测失败不打扰安装成功的消息 */ }
    doneHooks.forEach(fn => { try { fn() } catch { /* 钩子自己的事 */ } })
  } else if (s.state === 'error') {
    stopPolling()
    engInst.on = true     // 错误要露出来：重试或关闭，不能无声无息
    errorHooks.forEach(fn => { try { fn(s.error) } catch { /* 同上 */ } })
  }
}

export function watchEngine() {
  engInst.on = true
  engInst.hidden = false
  if (!timer) timer = setInterval(poll, 1000)
  poll()
}

/** 收起等待卡（下载继续，装好照样 toast + 自动续翻）。 */
export function hideEngineCard() { engInst.hidden = true }

/** 关掉错误卡。 */
export function closeEngineCard() { engInst.on = false; engInst.hidden = false }

/** 喊停下载（断点保留，下次从断点接着下）。 */
export async function cancelEngineInstall() {
  try { await api.pdf2zhInstallCancel() } catch { /* 状态下一拍自会归位 */ }
  stopPolling()
  engInst.on = false
  engInst.hidden = false
  engInst.state = 'idle'
}

/** 发起安装并接管轮询（设置页手动装、等待卡上的重试都走这里）。 */
export async function startEngineInstall() {
  engInst.error = ''
  try {
    const s = await api.pdf2zhInstall()
    Object.assign(engInst, {
      state: s.state, pct: s.pct || 0, got: s.got || 0, total: s.total || 0, src: s.src || '',
    })
  } catch { /* 起不动也进轮询，让状态接口说话 */ }
  watchEngine()
}

export function fmtMB(n) { return ((n || 0) / 1048576).toFixed(0) }
