async function req(method, url, body) {
  const opt = { method, headers: {} }
  if (body instanceof FormData) opt.body = body
  else if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  const r = await fetch(url, opt)
  if (!r.ok) {
    let msg = `${r.status}`
    try { msg = (await r.json()).detail || msg } catch { /* ignore */ }
    throw new Error(msg)
  }
  return r.json()
}

export const api = {
  papers: () => req('GET', '/api/papers'),
  upload: (file) => { const fd = new FormData(); fd.append('file', file); return req('POST', '/api/papers', fd) },
  paper: (pid) => req('GET', `/api/papers/${pid}`),
  deletePaper: (pid) => req('DELETE', `/api/papers/${pid}`),
  paragraphs: (pid) => req('GET', `/api/papers/${pid}/paragraphs`),
  analyze: (pid) => req('POST', `/api/papers/${pid}/analyze`),
  analysis: (pid) => req('GET', `/api/papers/${pid}/analysis`),
  overrideRole: (pid, paraIdx, role) => req('POST', `/api/papers/${pid}/override-role`, { para_idx: paraIdx, role }),
  marginaliaStart: (pid) => req('POST', `/api/papers/${pid}/marginalia`),
  marginalia: (pid) => req('GET', `/api/papers/${pid}/marginalia`),
  pin: (pid, body) => req('POST', `/api/papers/${pid}/pin`, body),
  unpin: (pid, mid) => req('DELETE', `/api/papers/${pid}/marginalia/${mid}`),
  summary: (pid) => req('GET', `/api/papers/${pid}/summary`),
  methodCard: (pid) => req('GET', `/api/papers/${pid}/method-card`),
  suggest: (pid) => req('GET', `/api/papers/${pid}/suggest`),
  advisor: (pid) => req('GET', `/api/papers/${pid}/advisor`),
  figures: (pid) => req('GET', `/api/papers/${pid}/figures`),
  askVisual: (body) => req('POST', '/api/ask-visual', body),
  figureUrl: (pid, f, dpi = 130) =>
    `/api/papers/${pid}/figure.png?page=${f.page}&x0=${f.x0}&y0=${f.y0}&x1=${f.x1}&y1=${f.y1}&dpi=${dpi}`,
  exportMdUrl: (pid) => `/api/papers/${pid}/export.md`,
  glossaryCsvUrl: '/api/glossary/export.csv',
  ask: (pid, question) => req('POST', `/api/papers/${pid}/ask`, { question }),
  qaHistory: (pid) => req('GET', `/api/papers/${pid}/qa-history`),
  translateSelection: (pid, text, context) => req('POST', `/api/papers/${pid}/translate-selection`, { text, context }),
  translatePara: (pid, idx) => req('POST', `/api/papers/${pid}/translate-para`, { idx }),
  translateFull: (pid) => req('POST', `/api/papers/${pid}/translate-full`),
  translateStatus: (pid) => req('GET', `/api/papers/${pid}/translate-status`),
  glossary: () => req('GET', '/api/glossary'),
  glossaryAdd: (item) => req('POST', '/api/glossary', item),
  glossaryDelete: (id) => req('DELETE', `/api/glossary/${id}`),
  settings: () => req('GET', '/api/settings'),
  saveSettings: (body) => req('PUT', '/api/settings', body),
  testSettings: () => req('POST', '/api/settings/test'),
}

export const ROLE_ZH = {
  background: '背景铺垫', gap: '缺口转折', claim: '核心主张', evidence: '关键证据',
  control: '对照参比', boilerplate: '标准流程', extension: '优化拓展', limitation: '让步局限',
}
// 页边书签上只放一个字：色标 + 首字，扫一眼就知道这段是什么
export const ROLE_GLYPH = {
  background: '背', gap: '缺', claim: '主', evidence: '证',
  control: '对', boilerplate: '流', extension: '拓', limitation: '限',
}
export const KIND_ZH = {
  hedge: '妥协让步', padding: '凑字数', stiff: '生硬别扭', redundant: '多余重复',
  hype: '吹嘘过头', ai: 'AI 痕迹', insight: '点睛之笔', warning: '有坑', lookup: '查译',
}
// 色标只表达一件事：读的时候该给多少注意力。
// 八个色相谁也记不住（人能一眼解码的上限是 3–4 个），所以颜色不该再区分
// "对照参比 vs 优化拓展"这种细类——那是点开角色卡才需要知道的。
// 一个暖色 = 全文的芯；三级墨由深到浅 = 论证主干 → 让步 → 铺垫与流程。
export const ROLE_COLOR = {
  claim: '#d08a1c',                                                    // 唯一暖色：核心主张
  evidence: '#57534a', gap: '#57534a',                                 // 深灰：论证主干
  control: '#8e8a80', extension: '#8e8a80', limitation: '#8e8a80',      // 中灰：外围与让步
  background: '#c9c4ba', boilerplate: '#c9c4ba',                       // 浅灰：铺垫与标准流程
}
// 色块上的字色：四档各自对白字/深字的对比度都过了 4.5:1
export const ROLE_INK = { claim: '#2b1d05', control: '#26231e', extension: '#26231e',
                          limitation: '#26231e', background: '#26231e', boilerplate: '#26231e' }
export const roleInk = (role) => ROLE_INK[role] || '#ffffff'
// 用作文字色时不能用浅灰（白底上看不见），另给一档正文可读的阶梯
export const ROLE_TEXT_COLOR = {
  claim: '#96620a', evidence: '#1d1b17', gap: '#1d1b17',
  control: '#55524a', extension: '#55524a', limitation: '#55524a',
  background: '#6f6b62', boilerplate: '#6f6b62',
}

// 眉批用同一套逻辑：值得读 / 要当心 / 是噪音 / 你自己写的
export const KIND_COLOR = {
  insight: '#c8811a',                                                          // 值得读
  warning: '#b8462e', hype: '#b8462e', ai: '#b8462e',                          // 要当心
  padding: '#c9c4ba', redundant: '#c9c4ba', stiff: '#c9c4ba', hedge: '#c9c4ba', // 噪音／可跳过
  lookup: '#57534a',                                                           // 你自己钉的
}
// 眉批标签的文字色（浅灰在白卡上看不清，另给可读的一档）
export const KIND_TEXT_COLOR = {
  insight: '#96620a', warning: '#9d3a25', hype: '#9d3a25', ai: '#9d3a25',
  padding: '#6f6b62', redundant: '#6f6b62', stiff: '#6f6b62', hedge: '#6f6b62',
  lookup: '#3f3230',
}
export const CORE_ROLES = ['gap', 'claim', 'evidence', 'limitation']
