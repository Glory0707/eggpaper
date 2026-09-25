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
