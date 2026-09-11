/* 极简 markdown：标题 / 加粗 / 无序列表 / 行内代码 / ¶n 引用。
 *
 * 只解析模型真会吐的那几种。不做通用解析器——通用解析器会顺手把表格、嵌套引用、
 * HTML 透传都带进来，在这类"读一句算一句"的面板里全是风险和噪音。
 */
const CITE = /¶\s*\d+/
const CODE = /`[^`]+`/
const BOLD = /\*\*[^*]+\*\*/

export function mdSegs(text) {
  const out = []
  for (const raw of String(text || '').split('\n')) {
    let t = raw.replace(/\s+$/, '')
    let head = 0, bullet = false
    const hm = t.match(/^(#{1,4})\s*(.*)$/)
    if (hm) { head = hm[1].length; t = hm[2] }
    if (/^\s*[-*]\s+/.test(t)) { bullet = true; t = t.replace(/^\s*[-*]\s+/, '') }
    const runs = []
    for (const seg of t.split(/(\*\*[^*]+\*\*|`[^`]+`|¶\s*\d+)/)) {
      if (!seg) continue
      if (BOLD.test(seg) && seg.startsWith('**')) runs.push({ text: seg.slice(2, -2), bold: true })
      else if (CODE.test(seg) && seg.startsWith('`')) runs.push({ text: seg.slice(1, -1), code: true })
      else if (CITE.test(seg) && /^¶\s*\d+$/.test(seg)) runs.push({ text: seg, cite: parseInt(seg.replace(/\D/g, ''), 10) })
      else runs.push({ text: seg })
    }
    // 连续空行只留一个段距，别把面板撑出大片空白
    if (!runs.length && out.length && out[out.length - 1].blank) continue
    out.push({ head, bullet, runs, blank: !runs.length })
  }
  while (out.length && out[out.length - 1].blank) out.pop()
  return out
}
