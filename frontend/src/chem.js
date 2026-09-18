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
  s = s.replace(/(?<![A-Za-z])([A-Z][A-Za-z0-9]{0,2})_([a-z0-9]{1,4})(?![a-z])/g,
                (m, a, b) => (canSub(b) ? a + toSub(b) : m))
  return s
}
