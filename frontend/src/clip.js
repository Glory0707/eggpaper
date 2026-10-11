export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* 权限被拒 / 非安全上下文：走下面的兜底 */ }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.cssText = 'position:fixed;top:-1000px;opacity:0'
  document.body.appendChild(ta)
  ta.select()
  let ok = false
  try { ok = document.execCommand('copy') } catch { ok = false }
  document.body.removeChild(ta)
  return ok
}

/* 复制 + 吐司：成功说 okMsg，失败说人话（execCommand 兜底也失败才算失败）。
   各处的成功文案不一样，作为参数传进来；失败文案全站统一。 */
import { toast } from './store'
import { t } from './i18n'

export async function copyWithToast(text, okMsg) {
  const ok = await copyText(text)
  toast(ok ? okMsg : t('复制失败'))
  return ok
}

/* Blob → dataURL：图表灯箱问 AI、页边贴图都用它（FileReader 的样板两处各写一遍）。 */
export function blobToDataURL(blob) {
  return new Promise(res => {
    const fr = new FileReader()
    fr.onload = () => res(fr.result)
    fr.readAsDataURL(blob)
  })
}

/* 复制图片（ClipboardItem）：失败别抛——调用方按 false 走 toast。 */
export async function copyBlob(blob) {
  try {
    await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })])
    return true
  } catch { return false }
}

/* 流式回答"按帧合并上屏"：每个 token 写一次响应式状态＝每个 token 重排一次 Markdown
   再读一次 scrollHeight，长回答肉眼可见地卡。合并成"一帧一次"之后只是更快、字一个不少。
   AskPanel（提问页）与 PdfViewer（划词/框选区）各写过一份，这里共用。 */
export function frameBuffer(apply) {
  let buf = '', raf = 0
  const flush = () => {
    raf = 0
    if (!buf) return
    const piece = buf
    buf = ''
    apply(piece)
  }
  return {
    push(piece) {
      buf += piece
      if (!raf) raf = requestAnimationFrame(flush)
    },
    flush() { if (raf) { cancelAnimationFrame(raf); raf = 0 } flush() },
  }
}
