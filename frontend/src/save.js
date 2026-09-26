/* 浏览器侧的文件落盘：Blob → 临时链 → 点击下载 → 回收。 */
export function saveBlob(blob, name) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}
