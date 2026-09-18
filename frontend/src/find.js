const KEEP = /[0-9a-z\u4e00-\u9fff]/

/* PDF 里的连字（ligature）是**一个**字符：ﬁ U+FB01、ﬂ U+FB02 这些。
   不折叠的话，"scientiﬁc"（原文）和 "scientific"（模型抄回来的）归一化之后是两个不同的串——
   于是引文"对不上"，划线只盖前半截（卡片上还会冒那个 ≈ 号）。
   用户抱怨的"对应的不好"里有一份就是它。折叠成字母后两边自然同形。 */
const LIG = { '\ufb00': 'ff', '\ufb01': 'fi', '\ufb02': 'fl',
              '\ufb03': 'ffi', '\ufb04': 'ffl', '\ufb05': 'ft', '\ufb06': 'st' }

function normText(s) {
  let out = ''
  for (const ch of (s || '').toLowerCase()) {
    const lig = LIG[ch]
    if (lig) out += lig
    else if (KEEP.test(ch)) out += ch
  }
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
      const lig = LIG[t[i].toLowerCase()]
      if (lig) {
        for (const ch of lig) { idx.norm += ch; idx.map.push(node, i) }
        continue
      }
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

/** 一个文本节点落在给定的页面矩形里吗（都相对 pageEl 的左上角）。 */
function nodeInBox(node, pageEl, box) {
  const el = node && node.parentElement
  if (!el) return false
  const base = pageEl.getBoundingClientRect()
  const r = el.getBoundingClientRect()
  const y0 = r.top - base.top, y1 = r.bottom - base.top
  return y1 >= box.y0 - 2 && y0 <= box.y1 + 2
}

/** 在某一页的文本层里找 quoted，返回纸面坐标下的逐行矩形（可能多块）。
 *
 *  paraBox（可选，页面像素坐标、相对 pageEl）：同一句话在这一页出现两次时，
 *  优先挑**落在这一段里**的那一次。页面上重复的句子并不少见（正文说一遍、
 *  图注再说一遍；同一个术语的定义反复出现），只认"第一个命中"就会把线划到别处去——
 *  用户说的"对应的不好"有一份就是这个。找不到落在段里的，就退回第一个命中。 */
export function findQuoteRects(pageEl, quote, paraBox) {
  const tl = pageEl?.querySelector?.('.textLayer')
  const q = normText(quote)
  if (!tl || q.length < 4) return null
  const idx = indexOf(tl)
  const len = matchLen(idx.norm, quote)
  if (len < 8) return null
  const head = q.slice(0, len)
  let at = idx.norm.indexOf(head)
  if (at < 0) return null
  if (paraBox) {
    let probe = at
    while (probe >= 0) {
      if (nodeInBox(idx.map[probe * 2], pageEl, paraBox)) { at = probe; break }
      probe = idx.norm.indexOf(head, probe + 1)
    }
  }
  const rects = rectsOf(idx, at, at + len, pageEl.getBoundingClientRect())
  return rects.length ? { rects, 覆盖比: len / q.length } : null
}

/* ---------- 引文扩成整句 ---------- */

/* 把模型引的那一段（常常只是半句）扩成"它所在的一整句"，返回原文里的那句原话。

   划线是按引文对回原文的字符算的：引半句，纸上就只有半句被划上。补成整句之后，
   线与句子齐、卡片上显示的也是同一句。句子取自**段落原文**（与 PDF 同源，逐字可比），
   精度不会更差；找不到就返回空串，调用方退回原引文。 */
export function sentenceAround(text, quote) {
  if (!text || !quote) return ''
  const q = normText(quote)
  if (q.length < 6) return ''
  const spans = []
  const SENT = /[^.!?。！？；;]+[.!?。！？；;]+["'”’)\]]*\s*/g
  let m, last = 0
  while ((m = SENT.exec(text))) {
    spans.push(m[0])
    last = SENT.lastIndex
  }
  if (last < text.length) spans.push(text.slice(last))   // 段末那句可能没有句号收尾
  for (const s of spans) {
    const n = normText(s)
    if (n && (n.indexOf(q) >= 0 || matchLen(n, quote) >= q.length * 0.8)) {
      const t = s.trim()
      return t.length <= 400 ? t : ''       // 整句过长（罕见）就算了，别划掉半页
    }
  }
  return ''
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
