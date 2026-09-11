/* 把一段引文对回纸上的具体位置。
 *
 * 为什么不用段落框：段落的 bbox 是整段的（含没被引用的字），拿它画高亮会糊一大片。
 * 模型抄回来的引文是**句子**，所以要把这句话对回字符，再取字符的矩形——
 * 划了几行、每行划多长，才跟原文一致。
 *
 * 归一化是必须的：PDF 的行尾常断在连字符上（"pro-" / "jector"），正文里又常有
 * 换行空格和标点差异。所以匹配前两边都只留字母/数字/汉字并小写，
 * 连字符、空格、标点一律丢掉——"pro-jector" 和 "projector" 自然就同形了。
 *
 * 两条路各司其职：
 *   lineSpanOf() —— 用后端给的行级坐标，不需要 DOM，算得出来就能用来定位（页边排序、
 *                   跳转目标）。给的是"覆盖第几行到第几行"和首行 y。
 *   findQuoteRects() —— 用已渲染的 textLayer 建 Range，range.getClientRects()
 *                   一次拿到逐行的精确矩形（该多长就多长），用来画线。
 */

const KEEP = /[0-9a-z\u4e00-\u9fff]/

function normText(s) {
  let out = ''
  for (const ch of (s || '').toLowerCase()) if (KEEP.test(ch)) out += ch
  return out
}

/* ---------- 路一：行级坐标（不依赖 DOM） ---------- */

export function lineSpanOf(para, quote) {
  const lines = para?.lines
  const q = normText(quote)
  if (!q || q.length < 4 || !lines?.length) return null
  let acc = ''
  const owner = []
  for (let li = 0; li < lines.length; li++) {
    for (const ch of lines[li].text || '') {
      const c = ch.toLowerCase()
      if (c.length === 1 && KEEP.test(c)) { acc += c; owner.push(li) }
    }
  }
  const at = acc.indexOf(q)
  if (at < 0) return null
  const from = owner[at], to = owner[at + q.length - 1]
  const ls = lines.slice(from, to + 1)
  return {
    from, to, lines: ls,
    bbox: { x0: Math.min(...ls.map(l => l.bbox.x0)), y0: ls[0].bbox.y0,
            x1: Math.max(...ls.map(l => l.bbox.x1)), y1: ls[ls.length - 1].bbox.y1 },
  }
}

/* ---------- 路二：DOM Range（已渲染时用，逐行精确） ---------- */

const cache = new WeakMap()          // textLayer -> {norm, map}

/** 文本层被重建（换缩放/换模式）时清掉索引——里面存的是节点引用，重画后即失效。 */
export function clearTextIndex(root) {
  if (root) cache.delete(root)
}

function indexOf(root) {
  let idx = cache.get(root)
  if (idx) return idx
  idx = { norm: '', map: [] }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let node
  while ((node = walker.nextNode())) {
    const t = node.nodeValue || ''
    for (let i = 0; i < t.length; i++) {
      const c = t[i].toLowerCase()
      if (c.length === 1 && KEEP.test(c)) { idx.norm += c; idx.map.push(node, i) }
    }
  }
  cache.set(root, idx)
  return idx
}

/** 命中区间 → 逐行矩形。
 *
 * 不用 range.getClientRects() 一把梭：跨文本节点时 Chromium 会顺带吐几个几像素宽的
 * 碎片（实测 147/3/3/161 这种），画出来就是几道莫名其妙的细线。改成按"覆盖到哪个
 * 文本节点"分组，每个节点各算一个矩形取最宽的那条——pdf.js 的文本层一行一个 span，
 * 所以这正好等于"划了哪几行、每行划多长"。 */
function rectsOf(idx, from, to, base) {
  const out = []
  let i = from
  while (i < to) {
    const node = idx.map[i * 2]
    let j = i
    while (j < to && idx.map[j * 2] === node) j++
    const r = document.createRange()
    try {
      r.setStart(node, idx.map[i * 2 + 1])
      r.setEnd(node, idx.map[(j - 1) * 2 + 1] + 1)
      let best = null
      for (const box of r.getClientRects()) {
        if (box.width < 1 || box.height < 1) continue
        if (!best || box.width > best.width) best = box
      }
      if (best) out.push({ x: best.left - base.left, y: best.top - base.top, w: best.width, h: best.height })
    } catch { /* 边界异常就跳过这一段 */ }
    i = j
  }
  return mergeRow(out)
}

/** 同一行里断开的矩形合成一条。
 *
 * 为什么会有断口：归一化匹配只留字母数字，标点和**空格被吃掉了**，而 pdf.js 的文本层
 * 会把空格单独成节点——于是引文跨过空格时，一行上会得到两个矩形，中间留一道几像素到
 * 十几像素的缝，画出来就是一条中间开洞的高亮（实测有一处 8px）。
 * 断在行之间不管：那是真的换行，本该分段。 */
function mergeRow(rects) {
  const out = []
  for (const r of rects.sort((a, b) => a.y - b.y || a.x - b.x)) {
    const last = out[out.length - 1]
    const sameRow = last && Math.abs(last.y - r.y) < Math.max(2, r.h * 0.4)
    if (sameRow && r.x - (last.x + last.w) < r.h * 0.9) {     // 缝小于一个字高：接上
      const x1 = Math.max(last.x + last.w, r.x + r.w)
      last.w = x1 - last.x
      last.h = Math.max(last.h, r.h)
      continue
    }
    out.push({ ...r })
  }
  return out
}
/** 引文在归一化文本里能对上多长（入参是**原文引文**，不是归一化后的串——
 *  归一化已经把空格吃掉了，再按词切就只剩一个词）。 */
function matchLen(norm, quote) {
  const q = normText(quote)
  if (norm.indexOf(q) >= 0) return q.length
  let best = 0, acc = ''
  for (const w of quote.split(/\s+/)) {
    const nw = normText(w)
    if (!nw) continue
    acc += nw
    if (acc.length > q.length) break
    if (norm.indexOf(acc) >= 0) best = acc.length
  }
  return best
}

/** 在某一页的文本层里找 quoted，返回纸面坐标下的逐行矩形（可能多块）。 */
export function findQuoteRects(pageEl, quote) {
  const tl = pageEl?.querySelector?.('.textLayer')
  const q = normText(quote)
  if (!tl || q.length < 4) return null
  const idx = indexOf(tl)
  const len = matchLen(idx.norm, quote)
  if (len < 8) return null
  const at = idx.norm.indexOf(q.slice(0, len))
  if (at < 0) return null
  const rects = rectsOf(idx, at, at + len, pageEl.getBoundingClientRect())
  return rects.length ? { rects, 覆盖比: len / q.length } : null
}

/** 整页里所有命中（文档内搜索用）。 */
export function findAllRects(pageEl, query, limit = 60) {
  const tl = pageEl?.querySelector?.('.textLayer')
  const q = normText(query)
  if (!tl || q.length < 2) return []
  const idx = indexOf(tl)
  const base = pageEl.getBoundingClientRect()
  const out = []
  let at = idx.norm.indexOf(q)
  while (at >= 0 && out.length < limit) {
    const rects = rectsOf(idx, at, at + q.length, base)
    if (rects.length) out.push({ rects, y: rects[0].y })
    at = idx.norm.indexOf(q, at + q.length)
  }
  return out
}
