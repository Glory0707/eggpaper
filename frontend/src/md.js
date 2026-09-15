/* 极简 markdown：标题 / 加粗 / 无序列表 / 行内代码 / ¶n 引用 / 表格。
 *
 * 只解析模型真会吐的那几种。不做通用解析器——通用解析器会顺手把嵌套引用、
 * HTML 透传都带进来，在这类"读一句算一句"的面板里全是风险和噪音。
 */
import { prettyChem } from './chem'

const CITE = /¶\s*\d+/
const CODE = /`[^`]+`/
const BOLD = /\*\*[^*]+\*\*/

export function mdSegs(text) {
  const out = []
  const lines = String(text || '').split('\n')
  for (let i = 0; i < lines.length; i++) {
    // 表格块：本行是行、下一行是 --- 分隔行，才当表格吃；中间任何一行不像行就断块。
    // 解析不出来的行一律按普通文本走——宁可显示竖线也不吞内容。
    if (isRow(lines[i]) && i + 1 < lines.length && isSep(lines[i + 1])) {
      const block = tableBlock(lines, i)
      out.push(block.seg)
      i = block.next
      continue
    }
    const seg = lineSeg(lines[i], out)
    if (seg) out.push(seg)
  }
  while (out.length && out[out.length - 1].blank) out.pop()
  return out
}

function lineSeg(raw, out) {
  let t = prettyChem(raw.replace(/\s+$/, ''))
  let head = 0, bullet = false
  const hm = t.match(/^(#{1,4})\s*(.*)$/)
  if (hm) { head = hm[1].length; t = hm[2] }
  if (/^\s*[-*]\s+/.test(t)) { bullet = true; t = t.replace(/^\s*[-*]\s+/, '') }
  const runs = runsOf(t)
  // 连续空行只留一个段距，别把面板撑出大片空白（null = 这行不产生内容）
  if (!runs.length && out.length && out[out.length - 1].blank) return null
  return { head, bullet, runs, blank: !runs.length }
}

/* ---------- 表格 ---------- */

/* 行：以 | 开头且后面还有一根竖线（行尾的 | 允许省） */
function isRow(t) {
  const s = String(t).trim()
  return s.startsWith('|') && s.slice(1).includes('|')
}

/* 分隔行：每个格子都是 --- / :---: 这类，至少一根横线。
   用拆格子的办法判，别用整行正则——边缘有没有竖线、竖线贴多紧，模型随手写，全要容。 */
function isSep(t) {
  const cells = String(t).split('|')
  const inner = cells.filter((c, k) => !(c.trim() === '' && (k === 0 || k === cells.length - 1)))
  return inner.length > 0 && inner.every(c => /^\s*:?-{2,}:?\s*$/.test(c))
}

function cellsOf(t) {
  let s = String(t).trim()
  if (s.startsWith('|')) s = s.slice(1)
  if (s.endsWith('|')) s = s.slice(0, -1)
  return s.split('|').map(c => runsOf(prettyChem(c.trim())))
}

function tableBlock(lines, i) {
  const head = cellsOf(lines[i])
  const rows = []
  let j = i + 2                       // 跳过分隔行
  while (j < lines.length && isRow(lines[j]) && rows.length < 200) {
    const cells = cellsOf(lines[j])
    rows.push(cells)
    j++
  }
  return { seg: { table: true, head, rows, blank: false }, next: j - 1 }
}

/* ---------- 行内 run：加粗 / 行内代码 / ¶n 引用 ---------- */

function runsOf(t) {
  const runs = []
  for (const seg of String(t).split(/(\*\*[^*]+\*\*|`[^`]+`|¶\s*\d+)/)) {
    if (!seg) continue
    if (BOLD.test(seg) && seg.startsWith('**')) runs.push({ text: seg.slice(2, -2), bold: true })
    else if (CODE.test(seg) && seg.startsWith('`')) runs.push({ text: seg.slice(1, -1), code: true })
    else if (CITE.test(seg) && /^¶\s*\d+$/.test(seg)) runs.push({ text: seg, cite: parseInt(seg.replace(/\D/g, ''), 10) })
    else runs.push({ text: seg })
  }
  return runs
}
