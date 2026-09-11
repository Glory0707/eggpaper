/* 复制到剪贴板。就一件事，但有两套环境要照顾。
 *
 * 正常情况走 navigator.clipboard——127.0.0.1 是安全上下文，一定可用。
 * 但用户可能从局域网 IP 打开（http://192.168.x.x），那时 clipboard API 在浏览器里
 * 根本不存在，只能退回老的 execCommand。复制是"用户按了就该生效"的动作，
 * 不能因为域名不同就静默失败。
 */
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
