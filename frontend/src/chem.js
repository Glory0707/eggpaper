/* 公式与化学式的"顺手规范化"。
 *
 * 模型写材料/参数时三种风格混着来：Unicode 下标（Sc₂O₃）、下划线（Sc_2O_3）、LaTeX
 * （$Sc_2O_3$、\Gamma、10^{-7}）。在 336px 的右栏里，$ 和 _ 既难看又占宽——
 * 这里统一收成 Unicode 上下标与真正的希腊字母，不改任何语义。
 *
 * 只做"看起来像记号"的替换，不当 markdown 解析器：
 *   Sc_2O_3 → Sc₂O₃      O_s → Oₛ（s 有下标字形）      10^{-7} → 10⁻⁷
 *   \Gamma → Γ           $\sim$0.1 eV → ∼0.1 eV        O_w 原样（w 没有下标字形）
 * 下划线只在"化学式形状"的左边才转换（前面是 1~3 个含大写的字母数字），
 * 免得把 snake_case 的英文词也扭成下标；字形不全的一律不动——
 * 硬把 O_w 转成 "Ow" 比留着下划线还糊。
 */

const SUB = { 0: '₀', 1: '₁', 2: '₂', 3: '₃', 4: '₄', 5: '₅', 6: '₆', 7: '₇', 8: '₈', 9: '₉',
              '+': '₊', '-': '₋', '=': '₌', '(': '₍', ')': '₎',
              a: 'ₐ', e: 'ₑ', h: 'ₕ', i: 'ᵢ', j: 'ⱼ', k: 'ₖ', l: 'ₗ', m: 'ₘ',
              n: 'ₙ', o: 'ₒ', p: 'ₚ', r: 'ᵣ', s: 'ₛ', t: 'ₜ', u: 'ᵤ', v: 'ᵥ', x: 'ₓ' }
const SUP = { 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹',
              '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽', ')': '⁾', n: 'ⁿ', i: 'ⁱ' }
const GREEK = { Gamma: 'Γ', Delta: 'Δ', Sigma: 'Σ', Omega: 'Ω', alpha: 'α', beta: 'β', gamma: 'γ',
                delta: 'δ', epsilon: 'ε', theta: 'θ', lambda: 'λ', mu: 'μ', nu: 'ν', pi: 'π',
                rho: 'ρ', sigma: 'σ', tau: 'τ', phi: 'φ', chi: 'χ', psi: 'ψ' }
const SYM = { times: '×', cdot: '·', approx: '≈', sim: '∼', simeq: '≃', leq: '≤', geq: '≥',
              ll: '≪', gg: '≫', to: '→', rightarrow: '→', pm: '±', mp: '∓', AA: 'Å', deg: '°',
              infty: '∞', propto: '∝', neq: '≠', equiv: '≡' }

const canSub = t => [...t].every(c => SUB[c] !== undefined)
const canSup = t => [...t].every(c => SUP[c] !== undefined)
const toSub = t => [...t].map(c => SUB[c]).join('')
const toSup = t => [...t].map(c => SUP[c]).join('')

export function prettyChem(text) {
  let s = String(text ?? '')
  if (!s) return s
  s = s.replace(/\$+([^$]{1,160})\$+/g, '$1')                        // 去掉 $…$
  s = s.replace(/\\([A-Za-z]+)/g, (m, g) => GREEK[g] ?? SYM[g] ?? g)  // \Gamma \sim …
  s = s.replace(/\\(?:text|mathrm|rm|it|bf)\s*\{([^{}]*)\}/g, '$1')
  s = s.replace(/\\(?:text|mathrm|rm|it|bf)\s+([A-Za-z]+)/g, '$1')
  s = s.replace(/\^\{([^{}]{1,6})\}/g, (m, x) => (canSup(x) ? toSup(x) : m))   // 10^{-7}
  s = s.replace(/\^([0-9+\-n]{1,3})\b/g, (m, x) => (canSup(x) ? toSup(x) : m))  // 10^-7 / 10^+3
  s = s.replace(/_\{([^{}]{1,6})\}/g, (m, x) => (canSub(x) ? toSub(x) : m))
  // 化学式形状的两侧：左边 1~3 个含大写的字母数字，右边只吃小写字母与数字
  // （吃到大写就会把 Sc_2O_3 的下标拼成 "2O"）。起始处用"前面不是字母"代替 \b：
  // Sc_2O_3 里 O_3 的 O 前面是数字 2，用 \b 会漏掉第二段下标。
  s = s.replace(/(?<![A-Za-z])([A-Z][A-Za-z0-9]{0,2})_([a-z0-9]{1,4})(?![a-z])/g,
                (m, a, b) => (canSub(b) ? a + toSub(b) : m))
  return s
}
