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
  figures: (pid) => req('GET', `/api/papers/${pid}/figures`),
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
export const KIND_ZH = {
  hedge: '妥协让步', padding: '凑字数', stiff: '生硬别扭', redundant: '多余重复',
  hype: '吹嘘过头', ai: 'AI 痕迹', insight: '点睛之笔', warning: '有坑', lookup: '查译',
}
export const KIND_COLOR = {
  insight: '#b0740d', padding: '#a89c85', hedge: '#637a8e', redundant: '#8d8066',
  hype: '#b8462e', ai: '#7b6e96', warning: '#99505f', stiff: '#a89c85', lookup: '#57503f',
}
export const ROLE_COLOR = {
  background: '#a89c85', gap: '#b8462e', claim: '#b0740d', evidence: '#55704d',
  control: '#637a8e', boilerplate: '#ab9166', extension: '#7b6e96', limitation: '#99505f',
}
export const CORE_ROLES = ['gap', 'claim', 'evidence', 'limitation']
