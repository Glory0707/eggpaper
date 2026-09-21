import { t } from './i18n'

/* 后端错误的人话解析：detail 优先，退回状态码——req / 表单上传 / SSE 三条通道
   共用这一份，别再各写一份"读 detail 兜状态码"。 */
async function respError(r, fallback = '') {
  let msg = fallback || `${r.status} ${r.statusText || ''}`.trim()
  try { msg = (await r.json()).detail || msg } catch { /* 不是 JSON，就用兜底 */ }
  return new Error(t(msg))
}

async function req(method, url, body) {
  const opt = { method, headers: {} }
  if (body instanceof FormData) opt.body = body
  else if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  const r = await fetch(url, opt)
  if (!r.ok) throw await respError(r)
  return r.json()
}

export const api = {
  papers: () => req('GET', '/api/papers'),
  libOverview: () => req('GET', '/api/library/overview'),
  upload: (file) => { const fd = new FormData(); fd.append('file', file); return req('POST', '/api/papers', fd) },
  importPath: (path) => req('POST', '/api/papers/import-path', { path }),
  zoteroItems: () => req('GET', '/api/zotero/items'),
  paperMeta: (pid, body) => req('POST', `/api/papers/${pid}/meta`, body),
  compare: (ids, dims) => req('POST', '/api/compare', { ids, dims }),
  paper: (pid) => req('GET', `/api/papers/${pid}`),
  deletePaper: (pid) => req('DELETE', `/api/papers/${pid}`),
  touchPaper: (pid) => req('POST', `/api/papers/${pid}/touch`),
  openRequest: () => req('GET', '/api/open-request'),
  paragraphs: (pid) => req('GET', `/api/papers/${pid}/paragraphs`),
  analyze: (pid) => req('POST', `/api/papers/${pid}/analyze`),
  analysis: (pid) => req('GET', `/api/papers/${pid}/analysis`),
  analysisCancel: (pid) => req('POST', `/api/papers/${pid}/analysis/cancel`),
  marginaliaStart: (pid) => req('POST', `/api/papers/${pid}/marginalia`),
  marginalia: (pid) => req('GET', `/api/papers/${pid}/marginalia`),
  marginaliaCancel: (pid) => req('POST', `/api/papers/${pid}/marginalia/cancel`),
  pin: (pid, body) => req('POST', `/api/papers/${pid}/pin`, body),
  unpin: (pid, mid) => req('DELETE', `/api/papers/${pid}/marginalia/${mid}`),
  summary: (pid) => req('GET', `/api/papers/${pid}/summary`),
  methodCard: (pid, cached = false) => req('GET', `/api/papers/${pid}/method-card${cached ? '?cached=1' : ''}`),
  citation: (pid, cached = false, refresh = false) =>
    req('GET', `/api/papers/${pid}/citation?${[cached && 'cached=1', refresh && 'refresh=1'].filter(Boolean).join('&')}`),
  suggest: (pid) => req('GET', `/api/papers/${pid}/suggest`),
  sixAnswers: (pid) => req('GET', `/api/papers/${pid}/six-answers`),
  sixAnswer: (pid, key) => req('GET', `/api/papers/${pid}/six-answers/${key}`),
  advisor: (pid, cached = false) => req('GET', `/api/papers/${pid}/advisor${cached ? '?cached=1' : ''}`),
  figures: (pid) => req('GET', `/api/papers/${pid}/figures`),
  figCaption: (pid, idx) => req('GET', `/api/papers/${pid}/fig_caption?idx=${idx}`),
  calendar: (month) => req('GET', `/api/calendar?month=${month}`),
  monthReport: (month) => req('POST', '/api/calendar/report', { month }),
  plan: (pid, day) => req('POST', `/api/papers/${pid}/plan`, { day }),
  libraryVersion: () => req('GET', '/api/library/version'),
  toc: (pid) => req('GET', `/api/papers/${pid}/toc`),
  askVisual: (body) => req('POST', '/api/ask-visual', body),
  figureUrl: (pid, f, dpi = 130) =>
    `/api/papers/${pid}/figure.png?page=${f.page}&x0=${f.x0}&y0=${f.y0}&x1=${f.x1}&y1=${f.y1}&dpi=${dpi}`,
  exportMdUrl: (pid) => `/api/papers/${pid}/export.md`,
  glossaryCsvUrl: (pid) => `/api/papers/${pid}/glossary/export.csv`,

  askUrl: (pid) => `/api/papers/${pid}/ask`,
  conversations: (pid) => req('GET', `/api/papers/${pid}/conversations`),
  convNew: (pid, title) => req('POST', `/api/papers/${pid}/conversations`, { title }),
  convRename: (cid, title) => req('PATCH', `/api/conversations/${cid}`, { title }),
  convDelete: (cid) => req('DELETE', `/api/conversations/${cid}`),
  qaHistory: (pid, convId) => req('GET', `/api/papers/${pid}/qa-history${convId ? `?conv_id=${convId}` : ''}`),
  qaSave: (pid, body) => req('POST', `/api/papers/${pid}/qa-save`, body),
  qaRegenerate: (pid, convId) => req('POST', `/api/papers/${pid}/regenerate`, { conv_id: convId }),
  qaDeleteOne: (cid, mid) => req('DELETE', `/api/conversations/${cid}/messages/${mid}`),

  collections: () => req('GET', '/api/collections'),
  collAdd: (name) => req('POST', '/api/collections', { name }),
  collRename: (cid, name) => req('PATCH', `/api/collections/${cid}`, { name }),
  collDelete: (cid) => req('DELETE', `/api/collections/${cid}`),
  paperColls: (pid, ids) => req('PUT', `/api/papers/${pid}/collections`, { ids }),

  translateFull: (pid, force) => req('POST', `/api/papers/${pid}/translate-full${force ? '?force=1' : ''}`),
  translateStatus: (pid) => req('GET', `/api/papers/${pid}/translate-status`),
  translateCancel: (pid) => req('POST', `/api/papers/${pid}/translate-full/cancel`),
  glossary: (pid) => req('GET', `/api/papers/${pid}/glossary`),
  glossaryGen: (pid) => req('POST', `/api/papers/${pid}/glossary/generate`),
  glossaryAdd: (pid, item) => req('POST', `/api/papers/${pid}/glossary`, item),
  glossaryDelete: (id) => req('DELETE', `/api/glossary/${id}`),
  settings: () => req('GET', '/api/settings'),
  version: () => req('GET', '/api/version'),
  updateCheck: (force = false) => req('GET', `/api/update/check${force ? '?force=1' : ''}`),
  updateDownload: (body) => req('POST', '/api/update/download', body),
  updateProgress: () => req('GET', '/api/update/progress'),
  updateInstall: (path) => req('POST', '/api/update/install', { path }),
  revealUpdate: (path) => req('POST', '/api/update/reveal', { path }),
  quit: (body) => req('POST', '/api/quit', body || {}),
  nativeWindow: () => req('POST', '/api/window'),
  screenshot: (body) => req('POST', '/api/screenshot', body),
  screenshotSave: (title, page, png) => req('POST', '/api/screenshot/save', { title, page, png }),
  screenshotFolder: () => req('POST', '/api/screenshot/folder'),
  saveSettings: (body) => req('PUT', '/api/settings', body),
  testSettings: () => req('POST', '/api/settings/test'),
  pdf2zhEngine: (path = '') =>
    req('GET', '/api/pdf2zh/engine' + (path ? `?path=${encodeURIComponent(path)}` : '')),
  pdf2zhInstall: (url = '') => req('POST', '/api/pdf2zh/install', { url }),
  pdf2zhInstallStatus: () => req('GET', '/api/pdf2zh/install-status'),
  pdf2zhInstallCancel: () => req('POST', '/api/pdf2zh/install-cancel'),
  setDataLocation: (path) => req('POST', '/api/data/location', { path }),
  dataPick: () => req('POST', '/api/data/pick', {}),
  pdf2zhInstallFromFile: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch('/api/pdf2zh/install-from-file', { method: 'POST', body: fd })
      .then(async r => {
        if (!r.ok) throw await respError(r, `HTTP ${r.status}`)
        return r.json()
      })
  },
}

/* SSE 流式回答。EventSource 不能 POST，所以用 fetch + ReadableStream 自己拆帧。
   onEvent 收到 {type:'delta'|'done'|'error'}；返回一个 abort() 用来"停止生成"。
   只给本文件的 askStream / translateStream 用，不对外导出。 */
function sseStream(url, body, onEvent) {
  const ctrl = new AbortController()
  const done = (async () => {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), signal: ctrl.signal,
    })
    if (!r.ok || !r.body) throw await respError(r)
    const reader = r.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done: fin, value } = await reader.read()
      if (fin) break
      buf += dec.decode(value, { stream: true })
      const frames = buf.split('\n\n')
      buf = frames.pop()
      for (const f of frames) {
        const line = f.replace(/^data:\s?/, '').trim()
        if (!line) continue
        let ev
        try { ev = JSON.parse(line) } catch { continue }   // 半帧/心跳：解不出来就跳过
        onEvent(ev)
      }
    }
  })()
  return { abort: () => ctrl.abort(), done }
}

export function askStream(pid, body, onEvent) {
  return sseStream(api.askUrl(pid), body, onEvent)
}

export function translateStream(pid, kind, body, onEvent) {
  return sseStream(`/api/papers/${pid}/translate-${kind}`, body, onEvent)
}

export const ROLE_ZH = {
  background: '背景铺垫', gap: '缺口转折', claim: '核心主张', evidence: '关键证据',
  control: '对照参比', boilerplate: '标准流程', extension: '优化拓展', limitation: '让步局限',
}
const KIND_ZH = {
  hedge: '妥协让步', padding: '凑字数', stiff: '生硬别扭', redundant: '多余重复',
  hype: '吹嘘过头', ai: 'AI 痕迹', insight: '点睛之笔', warning: '有坑',
  conflict: '前后打架', lookup: '查译', region: '选区问答', note: '批注',
}
export const ROLE_COLOR = {
  claim: '#1d4e5f',                                                    // 唯一彩色：核心主张
  evidence: '#57534a', gap: '#57534a',                                 // 深灰：论证主干
  control: '#8e8a80', extension: '#8e8a80', limitation: '#8e8a80',      // 中灰：外围与让步
  background: '#c9c4ba', boilerplate: '#c9c4ba',                       // 浅灰：铺垫与标准流程
}
export const ROLE_TEXT_COLOR = {
  claim: 'var(--accent-deep)', evidence: 'var(--ink)', gap: 'var(--ink)',
  control: 'var(--ink-2)', extension: 'var(--ink-2)', limitation: 'var(--ink-2)',
  background: 'var(--ink-3)', boilerplate: 'var(--ink-3)',
}

const KIND_COLOR = {
  insight: '#1d4e5f',                                                          // 值得读
  warning: '#b8462e', hype: '#b8462e', ai: '#b8462e', conflict: '#b8462e',       // 要当心
  padding: '#c9c4ba', redundant: '#c9c4ba', stiff: '#c9c4ba', hedge: '#c9c4ba', // 噪音／可跳过
  lookup: '#57534a', region: '#57534a',                    // 你自己钉的
  note: '#57534a',                                        // 你自己写的批注
}
const KIND_TEXT_COLOR = {
  insight: 'var(--accent-deep)', warning: 'var(--vermilion-deep)', hype: 'var(--vermilion-deep)', ai: 'var(--vermilion-deep)', conflict: 'var(--vermilion-deep)',
  padding: 'var(--ink-3)', redundant: 'var(--ink-3)', stiff: 'var(--ink-3)', hedge: 'var(--ink-3)',
  lookup: 'var(--ink-2)', region: 'var(--ink-2)', note: 'var(--ink-2)',
}

/* 眉批的档位：三档，就三档。
   九种常用款各自落在一档里；模型自造的类型（kind='custom'）必须自己声明档位，
   颜色按档位走——页边只有三种笔触，读者也只需要分清三种。 */
const BAND_COLOR = { good: '#1d4e5f', warn: '#b8462e', noise: '#8e8a80', mine: '#57534a' }
const BAND_TEXT = { good: 'var(--accent-deep)', warn: 'var(--vermilion-deep)', noise: 'var(--ink-3)', mine: 'var(--ink-2)' }
const BAND_OF_KIND = {
  insight: 'good',
  warning: 'warn', hype: 'warn', ai: 'warn', conflict: 'warn',
  padding: 'noise', redundant: 'noise', stiff: 'noise', hedge: 'noise',
  lookup: 'mine', region: 'mine', note: 'mine',
}

/* 以下是**唯一的**解析口：给一条批注，回答它属于哪一档、什么颜色、标签写什么。
   kind 是开放词表，散落着判断的话，加一种新类型就要改五个地方。 */
export function bandOf(n) {
  if (!n) return 'noise'
  if (n.band) return n.band                      // 后端已经算好档位（含自造款）
  return BAND_OF_KIND[n.kind] || 'noise'
}
export function kindColor(n) {
  const k = typeof n === 'string' ? n : n?.kind
  const c = KIND_COLOR[k]
  if (c) return c
  return BAND_COLOR[typeof n === 'string' ? 'noise' : bandOf(n)] || BAND_COLOR.noise
}
export function kindText(n) {
  const k = typeof n === 'string' ? n : n?.kind
  const c = KIND_TEXT_COLOR[k]
  if (c) return c
  return BAND_TEXT[typeof n === 'string' ? 'noise' : bandOf(n)] || BAND_TEXT.noise
}
export function kindZH(n) {
  if (typeof n === 'string') return t(KIND_ZH[n] || '')
  if (!n) return ''
  if (n.kind === 'custom') return n.label || '新批注'   // 模型自造的：用它自己起的那个短标签
  return t(KIND_ZH[n.kind] || '')
}
