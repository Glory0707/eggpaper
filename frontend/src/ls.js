/* localStorage 的统一出入口：前缀 eggpaper: + JSON 序列化。
   坏数据/隐私模式只回落默认值，不让任何一处的读写在启动时炸掉。 */

const LS = 'eggpaper:'

export function lsGet(k, d) {
  try { return JSON.parse(localStorage.getItem(LS + k)) ?? d } catch { return d }
}

export function lsSet(k, v) {
  localStorage.setItem(LS + k, JSON.stringify(v))
}
