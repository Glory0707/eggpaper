/* 浏览器侧的文件落盘：Blob → 临时链 → 点击下载 → 回收。 */
export function saveBlob(blob, name) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}

/* CSV 单元格转义：引号加倍 + 用引号包整格（回车/换行也要进引号，不然表格里会断行）。
   对比表、引用导出、术语导出三条路各写一份 esc 时，总有一处忘了某条规则。 */
export function csvCell(v) {
  const c = String(v ?? '')
  return /[",\r\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c
}

/* 直接按 URL 下载（后端已经会带 attachment 头的那种：全库备份、CSV、导出 .md） */
export function downloadUrl(url, name = '') {
  const a = document.createElement('a')
  a.href = url
  if (name) a.download = name
  a.click()
}
