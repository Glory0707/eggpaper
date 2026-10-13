/* localStorage 的统一出入口：前缀 eggpaper: + JSON 序列化。
   坏数据/隐私模式只回落默认值，不让任何一处的读写在启动时炸掉。 */

const LS = 'eggpaper:'

export function lsGet(k, d) {
  try { return JSON.parse(localStorage.getItem(LS + k)) ?? d } catch { return d }
}

/* 写侧也要兜住：隐私模式/配额满时 setItem 会抛，而 lsSet 是在若干 watch(immediate)
   的初始化路径上被调的——抛出去就是"整页起不来"。失败当没存（下次读回落默认值），
   与 lsGet 的兜底口径一致 */
export function lsSet(k, v) {
  try { localStorage.setItem(LS + k, JSON.stringify(v)) } catch { /* 存不下就算了 */ }
}

export function lsRemove(k) {
  try { localStorage.removeItem(LS + k) } catch { /* 同上 */ }
}
