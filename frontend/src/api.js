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

/* 短时去重：同一个 GET 在 1.2s 内的并发/连发请求共用第一发的结果。
   打开一篇论文时 store.activatePaper 与 PdfViewer.loadPaperData 各拉一遍
   paper+paragraphs（后者可达数 MB），纯重复传输；1.2s 过后自动失效，
   不影响轮询类调用的时效。 */
const _recent = new Map()
const RECENT_MS = 1200   // 1.2s 内的同键请求直接复用在途 Promise，不影响轮询类调用的时效
function onceGet(key, fn) {
  const hit = _recent.get(key)
  if (hit && Date.now() - hit.at < RECENT_MS) return hit.p
  const p = fn()
  _recent.set(key, { p, at: Date.now() })
  p.catch(() => {}).finally(() => { const h = _recent.get(key); if (h?.p === p) _recent.delete(key) })
  return p
}

export const api = {
  papers: () => req('GET', '/api/papers'),
  search: (q) => req('GET', '/api/search?q=' + encodeURIComponent(q)),
  libOverview: () => req('GET', '/api/library/overview'),
  upload: (file) => { const fd = new FormData(); fd.append('file', file); return req('POST', '/api/papers', fd) },
  importPath: (path) => req('POST', '/api/papers/import-path', { path }),
  zoteroItems: () => req('GET', '/api/zotero/items'),
  paperMeta: (pid, body) => req('POST', `/api/papers/${pid}/meta`, body),
  compare: (ids, dims, cols) => req('POST', '/api/compare', { ids, dims, cols }),
  citedby: (pid) => req('GET', `/api/papers/${pid}/citedby`),
  citedbyClassify: (pid) => req('POST', `/api/papers/${pid}/citedby/classify`),
  paper: (pid) => onceGet('p:' + pid, () => req('GET', `/api/papers/${pid}`)),
  deletePaper: (pid) => req('DELETE', `/api/papers/${pid}`),
  supersede: (newPid, oldPid) => req('POST', `/api/papers/${newPid}/supersede`, { old_pid: oldPid }),
  citeTable: (ids) => req('POST', '/api/cite-table', { ids }),
  touchPaper: (pid) => req('POST', `/api/papers/${pid}/touch`),
  openRequest: () => req('GET', '/api/open-request'),
  paragraphs: (pid) => onceGet('g:' + pid, () => req('GET', `/api/papers/${pid}/paragraphs`)),
  analyze: (pid) => req('POST', `/api/papers/${pid}/analyze`),
  analysis: (pid) => req('GET', `/api/papers/${pid}/analysis`),
  analysisCancel: (pid) => req('POST', `/api/papers/${pid}/analysis/cancel`),
  marginalia: (pid) => req('GET', `/api/papers/${pid}/marginalia`),
  pin: (pid, body) => req('POST', `/api/papers/${pid}/pin`, body),
  unpin: (pid, mid) => req('DELETE', `/api/papers/${pid}/marginalia/${mid}`),
  summary: (pid) => req('GET', `/api/papers/${pid}/summary`),
  methodCard: (pid, cached = false) => req('GET', `/api/papers/${pid}/method-card${cached ? '?cached=1' : ''}`),
  citation: (pid, cached = false, refresh = false) =>
    req('GET', `/api/papers/${pid}/citation?${[cached && 'cached=1', refresh && 'refresh=1'].filter(Boolean).join('&')}`),
  suggest: (pid) => req('GET', `/api/papers/${pid}/suggest`),
  sixAnswers: (pid) => req('GET', `/api/papers/${pid}/six-answers`),
  sixAnswer: (pid, key) => req('GET', `/api/papers/${pid}/six-answers/${key}`),
  figures: (pid) => req('GET', `/api/papers/${pid}/figures`),
  figCaption: (pid, idx) => req('GET', `/api/papers/${pid}/fig_caption?idx=${idx}`),
  calendar: (month) => req('GET', `/api/calendar?month=${month}`),
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
  setTitle: (pid, title) => req('POST', `/api/papers/${pid}/title`, { title }),
  glossary: (pid) => req('GET', `/api/papers/${pid}/glossary`),
  glossaryGen: (pid) => req('POST', `/api/papers/${pid}/glossary/generate`),
  glossaryAdd: (pid, item) => req('POST', `/api/papers/${pid}/glossary`, item),
  glossaryDelete: (id, pid) => req('DELETE', `/api/glossary/${id}?pid=` + encodeURIComponent(pid || '')),
  glossaryGlobal: () => req('GET', '/api/glossary-global'),
  glossaryGlobalAdd: (item) => req('POST', '/api/glossary-global', item),
  glossaryGlobalDelete: (id) => req('DELETE', `/api/glossary-global/${id}`),
  glossaryGlobalCsvUrl: () => '/api/glossary-global/export.csv',
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
  revealData: () => req('POST', '/api/reveal-data'),
  revealPaper: (pid) => req('POST', `/api/papers/${pid}/reveal`),
  forward: (pids, kind) => req('POST', '/api/papers/forward', { pids, kind }),
  saveSettings: (body) => req('PUT', '/api/settings', body),
  testSettings: () => req('POST', '/api/settings/test'),
  pdf2zhEngine: (path = '') =>
    req('GET', '/api/pdf2zh/engine' + (path ? `?path=${encodeURIComponent(path)}` : '')),
  pdf2zhInstall: (url = '') => req('POST', '/api/pdf2zh/install', { url }),
  pdf2zhInstallStatus: () => req('GET', '/api/pdf2zh/install-status'),
  pdf2zhInstallCancel: () => req('POST', '/api/pdf2zh/install-cancel'),
  setDataLocation: (path) => req('POST', '/api/data/location', { path }),
  dataPick: () => req('POST', '/api/data/pick', {}),
  backupExportUrl: () => '/api/backup/export',
  backupPick: () => req('POST', '/api/backup/pick', {}),
  backupRestore: (path) => req('POST', '/api/backup/restore', { path }),
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

/* 接着写：上一条回答被输出上限截断了，从断点续写（服务端写回同一条消息）。
   与 askStream 同一套 SSE 事件，done 里带 appended: true。 */
export function askContinueStream(pid, mid, onEvent) {
  return sseStream(`/api/papers/${pid}/ask-continue`, { mid }, onEvent)
}

export function translateStream(pid, kind, body, onEvent) {
  return sseStream(`/api/papers/${pid}/translate-${kind}`, body, onEvent)
}

export const ROLE_ZH = {
  background: '背景铺垫', gap: '缺口转折', claim: '核心主张', evidence: '关键证据',
  control: '对照参比', boilerplate: '标准流程', extension: '优化拓展', limitation: '让步局限',
}
const KIND_ZH = { lookup: '查译', region: '选区问答', note: '批注' }
/* 论证色带：写死的十六进制在暗色主题下会反向——深灰混进黑底（隐形）、浅灰在暗底上刺眼。
   与 ROLE_TEXT_COLOR 同一个口径，改成主题变量：四档由强到弱的灰阶交给 token 定，
   色带在哪个主题下都自洽 */
export const ROLE_COLOR = {
  claim: 'var(--accent)',                                              // 唯一彩色：核心主张
  evidence: 'var(--ink-2)', gap: 'var(--ink-2)',                       // 深灰：论证主干
  control: 'var(--ink-3)', extension: 'var(--ink-3)', limitation: 'var(--ink-3)',  // 中灰：外围与让步
  background: 'var(--hairline)', boilerplate: 'var(--hairline)',       // 浅灰：铺垫与标准流程
}
export const ROLE_TEXT_COLOR = {
  claim: 'var(--accent-deep)', evidence: 'var(--ink)', gap: 'var(--ink)',
  control: 'var(--ink-2)', extension: 'var(--ink-2)', limitation: 'var(--ink-2)',
  background: 'var(--ink-3)', boilerplate: 'var(--ink-3)',
}

/* 页边卡的标签：三种钉子各有名字。颜色与笔触是统一的（styles.css 的
   .mg-note / .mg-mark.mine），这里只回答"叫什么"。 */
export function kindZH(n) {
  return n ? t(KIND_ZH[n.kind] || '') : ''
}
