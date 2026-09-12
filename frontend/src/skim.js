/* 略读的刀口：段该不该蒙是析读判的（background / boilerplate / 参考文献），
 * 这里管的是**段内**——一段铺垫里常常埋着一句带数字的结果、一个图表指引、
 * 一句 "we propose"。整段涂掉会把它们一起埋了。
 *
 * 所以蒙纱有两档，按段里句子的成色灵活选：
 *   · 整段都可略 → 段落模式，整段盖；
 *   · 段里混着要紧的句子 → 句子模式，只盖可略的那些句子；
 *   · 每句都有硬信息 → 这一段其实不该蒙，一段都不盖。
 *
 * 判一句"要不要紧"只看纸上看得见的东西：数字、引用标记、图表指引、
 * 作者第一人称的动作、转折/总结词。没有这些 → 可略。 */

export function isCaptionText(t) {
  return /^(fig(ure)?s?|table|scheme|equation|plot|图|表|附图|式)\s*[.\d]/i.test((t || '').trim())
}

/* 分句。论文里的句点一半不是句尾：小数（0.5）、编号（Fig. 3）、缩写（et al.）。
   判定太松会把两句并一句（多蒙半句），太贪会把一句切成三截（蒙纱跟着碎）；
   切错了还有下面"短碎片并回上一句"兜着，所以这里宁松勿贪。 */
const ABBR_RE = /(?:e\.g|i\.e|et al|etc|vs|cf|ca|viz|Fig|Figs|Eq|Eqn|Ref|Refs|Sec|Tab|No|Vol|pp|ed|eds|Prof|Dr|Mr|Ms|St|approx)\.$/i

export function splitSentences(text) {
  const t = text || ''
  const cuts = []
  for (let i = 0; i < t.length; i++) {
    const ch = t[i]
    if (ch !== '.' && ch !== '!' && ch !== '?' && ch !== '。' && ch !== '！' && ch !== '？') continue
    if (ch === '.') {
      // 小数（3.5）与编号（Fig. 3 / Eq. 2a）：点前面是数字、点后面也是数字
      if (/\d/.test(t[i - 1] || '') && /\d/.test(t[i + 1] || '')) continue
      // 单个大写字母加点的多是首字母缩写（J. Zhang），不是句尾
      if (/^[A-Z]\.$/.test(t.slice(i - 2, i + 1))) continue
      const before = t.slice(Math.max(0, i - 8), i + 1)
      if (ABBR_RE.test(before)) continue
    }
    // 句点后面必须跟空白或右引号才算一句完了（"v2.q3" 这种连写不切）
    const next = t[i + 1]
    if (next && !/[\s"'”’)\]]/.test(next)) continue
    cuts.push(i + 1)
  }
  const parts = []
  let start = 0
  for (const c of cuts) { parts.push(t.slice(start, c)); start = c }
  if (start < t.length) parts.push(t.slice(start))
  // 短碎片（缩写误切、编号残留）并回上一句：多并不少切，蒙纱不至于碎成一地
  const out = []
  for (const s of parts) {
    const prev = out[out.length - 1]
    if (prev && (s.trim().length < 24 || prev.trim().length < 24)) out[out.length - 1] = prev + s
    else out.push(s)
  }
  return out.map(s => s.trim()).filter(Boolean)
}

/* 这些句子留下来不蒙。每条都对着"读者扫一眼就知道这里有货"的东西。 */
const HARD = [
  /\[\d+(?:[,\s]+\d+)*\]/,                    // 数字引用 [12] [3, 7]
  /\(\s*[A-Z][^()]{2,40}?,?\s*(?:19|20)\d{2}\s*\)/,   // (Zhang et al., 2020) 这类作者-年份引用
  /(?:\d[\d,.]*\s*%)/,                        // 百分数
  /\b\d\.\d+/,                                // 小数：数据值的通用形状
  /(?:^|\s)[+-]?\d{3,}\b/,                    // 三位以上的整数（300 K、1000 cycles）
  /(?:^|[^a-z])(?:fig|figure|figs|table|scheme|eq|eqn|equation)s?\.\s*\d/i,   // 图表/公式指引
  /\bwe\b[^.?!]{0,60}?\b(?:propos|present|report|demonstr|show|found|find|observ|design|fabricat|investigat|introduc|achiev|develop|address|identif|revea|measure|perform|conduct|synthesiz|exploit|employ)/i,
  /\bin (?:this|our) (?:work|study|paper|experiment)s?\b/i,
  /\b(?:however|therefore|thus|hence|in contrast|overall|in summary|conclusion|remarkably|importantly|unexpectedly|notably|surprisingly|to our knowledge)\b/i,
]

export function sentenceIsKept(s) {
  return HARD.some(re => re.test(s || ''))
}

/* 一段的蒙纱方案：none=一段都是硬信息（别蒙）；partial=句子模式（只蒙 skip 里的）；
   否则=段落模式（整段盖）。 */
export function planFor(text) {
  const sents = splitSentences(text)
  const skip = sents.filter(s => !sentenceIsKept(s))
  if (!sents.length || !skip.length) return { mode: 'none', skip: [] }
  if (skip.length === sents.length) return { mode: 'all', skip: [] }
  return { mode: 'partial', skip }
}
