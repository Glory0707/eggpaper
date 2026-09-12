async function req(method, url, body) {
  const opt = { method, headers: {} }
  if (body instanceof FormData) opt.body = body
  else if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  const r = await fetch(url, opt)
  if (!r.ok) {
    // 后端会把失败翻译成人话放进 detail；拿不到（比如请求根本没到服务）才退回状态码 + 短语
    let msg = `${r.status} ${r.statusText || ''}`.trim()
    try { msg = (await r.json()).detail || msg } catch { /* 不是 JSON，就用上面的兜底 */ }
    throw new Error(msg)
  }
  return r.json()
}

export const api = {
  papers: () => req('GET', '/api/papers'),
  upload: (file) => { const fd = new FormData(); fd.append('file', file); return req('POST', '/api/papers', fd) },
  paper: (pid) => req('GET', `/api/papers/${pid}`),
  deletePaper: (pid) => req('DELETE', `/api/papers/${pid}`),
  touchPaper: (pid) => req('POST', `/api/papers/${pid}/touch`),
  // 双击 PDF 打开：后端把文件建进库，这里问"该切到哪一篇"
  openRequest: () => req('GET', '/api/open-request'),
  paragraphs: (pid) => req('GET', `/api/papers/${pid}/paragraphs`),
  analyze: (pid) => req('POST', `/api/papers/${pid}/analyze`),
  analysis: (pid) => req('GET', `/api/papers/${pid}/analysis`),
  overrideRole: (pid, paraIdx, role) => req('POST', `/api/papers/${pid}/override-role`, { para_idx: paraIdx, role }),
  marginaliaStart: (pid) => req('POST', `/api/papers/${pid}/marginalia`),
  marginalia: (pid) => req('GET', `/api/papers/${pid}/marginalia`),
  pin: (pid, body) => req('POST', `/api/papers/${pid}/pin`, body),
  unpin: (pid, mid) => req('DELETE', `/api/papers/${pid}/marginalia/${mid}`),
  summary: (pid) => req('GET', `/api/papers/${pid}/summary`),
  // cached=1：只读缓存，没有就返回空——进速览页要把算过的显示出来，但不该顺手花一次模型调用
  methodCard: (pid, cached = false) => req('GET', `/api/papers/${pid}/method-card${cached ? '?cached=1' : ''}`),
  // 引用信息：cached 只读缓存（打开浮层不该顺手花一次模型调用），refresh 是「重新识别」
  citation: (pid, cached = false, refresh = false) =>
    req('GET', `/api/papers/${pid}/citation?${[cached && 'cached=1', refresh && 'refresh=1'].filter(Boolean).join('&')}`),
  suggest: (pid) => req('GET', `/api/papers/${pid}/suggest`),
  sixAnswers: (pid) => req('GET', `/api/papers/${pid}/six-answers`),
  sixAnswer: (pid, key) => req('GET', `/api/papers/${pid}/six-answers/${key}`),
  advisor: (pid, cached = false) => req('GET', `/api/papers/${pid}/advisor${cached ? '?cached=1' : ''}`),
  figures: (pid) => req('GET', `/api/papers/${pid}/figures`),
  askVisual: (body) => req('POST', '/api/ask-visual', body),
  figureUrl: (pid, f, dpi = 130) =>
    `/api/papers/${pid}/figure.png?page=${f.page}&x0=${f.x0}&y0=${f.y0}&x1=${f.x1}&y1=${f.y1}&dpi=${dpi}`,
  exportMdUrl: (pid) => `/api/papers/${pid}/export.md`,
  glossaryCsvUrl: '/api/glossary/export.csv',

  // 提问：会话 + 流式回答
  askUrl: (pid) => `/api/papers/${pid}/ask`,
  conversations: (pid) => req('GET', `/api/papers/${pid}/conversations`),
  convNew: (pid, title) => req('POST', `/api/papers/${pid}/conversations`, { title }),
  convRename: (cid, title) => req('PATCH', `/api/conversations/${cid}`, { title }),
  convDelete: (cid) => req('DELETE', `/api/conversations/${cid}`),
  qaHistory: (pid, convId) => req('GET', `/api/papers/${pid}/qa-history${convId ? `?conv_id=${convId}` : ''}`),
  qaSave: (pid, body) => req('POST', `/api/papers/${pid}/qa-save`, body),
  qaRegenerate: (pid, convId) => req('POST', `/api/papers/${pid}/regenerate`, { conv_id: convId }),
  qaDeleteOne: (cid, mid) => req('DELETE', `/api/conversations/${cid}/messages/${mid}`),

  // 文库分类
  collections: () => req('GET', '/api/collections'),
  collAdd: (name) => req('POST', '/api/collections', { name }),
  collRename: (cid, name) => req('PATCH', `/api/collections/${cid}`, { name }),
  collDelete: (cid) => req('DELETE', `/api/collections/${cid}`),
  paperColls: (pid, ids) => req('PUT', `/api/papers/${pid}/collections`, { ids }),

  // 划词/段译只有流式一条路（translateStream）：这两个非流式包装没人用，而且后端
  // 那两个接口返回的是 SSE，真被调也会炸——删掉，别再钓着一个错的东西
  translateFull: (pid) => req('POST', `/api/papers/${pid}/translate-full`),
  translateStatus: (pid) => req('GET', `/api/papers/${pid}/translate-status`),
  glossary: () => req('GET', '/api/glossary'),
  glossaryAdd: (item) => req('POST', '/api/glossary', item),
  glossaryDelete: (id) => req('DELETE', `/api/glossary/${id}`),
  settings: () => req('GET', '/api/settings'),
  version: () => req('GET', '/api/version'),
  // 更新：查源 / 下载（进度另轮询）/ 交给安装器 / 退出程序
  updateCheck: (force = false) => req('GET', `/api/update/check${force ? '?force=1' : ''}`),
  updateDownload: (body) => req('POST', '/api/update/download', body),
  updateProgress: () => req('GET', '/api/update/progress'),
  updateInstall: (path) => req('POST', '/api/update/install', { path }),
  revealUpdate: (path) => req('POST', '/api/update/reveal', { path }),
  quit: () => req('POST', '/api/quit'),
  nativeWindow: () => req('POST', '/api/window'),
  saveSettings: (body) => req('PUT', '/api/settings', body),
  testSettings: () => req('POST', '/api/settings/test'),
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
    if (!r.ok || !r.body) {
      let msg = `${r.status} ${r.statusText || ''}`.trim()
      try { msg = (await r.json()).detail || msg } catch { /* 非 JSON 就用状态码 */ }
      throw new Error(msg)
    }
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
        // onEvent 必须在 try **外面**调：翻译那条路收到 error 事件时会 throw
        // （用来让 done 拒绝、弹出错误），而这个 throw 以前正好被上面那个空 catch 吞掉了——
        // 结果是"半截译文被当成成品钉在页边，报错一个字都没说"。
        onEvent(ev)
      }
    }
  })()
  return { abort: () => ctrl.abort(), done }
}

export function askStream(pid, body, onEvent) {
  return sseStream(api.askUrl(pid), body, onEvent)
}

// 翻译也走流式：划词等一秒就该见到字，等 10 秒才砸出整段没人受得了
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
// 色标只表达一件事：读的时候该给多少注意力。
// 八个色相谁也记不住（人能一眼解码的上限是 3–4 个），所以颜色不该再区分
// "对照参比 vs 优化拓展"这种细类——读的时候本来也分不出来。
// 一个暖色 = 全文的芯；三级墨由深到浅 = 论证主干 → 让步 → 铺垫与流程。
export const ROLE_COLOR = {
  claim: '#1d4e5f',                                                    // 唯一彩色：核心主张
  evidence: '#57534a', gap: '#57534a',                                 // 深灰：论证主干
  control: '#8e8a80', extension: '#8e8a80', limitation: '#8e8a80',      // 中灰：外围与让步
  background: '#c9c4ba', boilerplate: '#c9c4ba',                       // 浅灰：铺垫与标准流程
}
// 用作文字色时不能用浅灰（白底上看不见），另给一档正文可读的阶梯
export const ROLE_TEXT_COLOR = {
  claim: '#123a47', evidence: '#1d1b17', gap: '#1d1b17',
  control: '#55524a', extension: '#55524a', limitation: '#55524a',
  background: '#6f6b62', boilerplate: '#6f6b62',
}

// 眉批用同一套逻辑：值得读 / 要当心 / 是噪音 / 你自己写的
const KIND_COLOR = {
  insight: '#1d4e5f',                                                          // 值得读
  warning: '#b8462e', hype: '#b8462e', ai: '#b8462e', conflict: '#b8462e',       // 要当心
  padding: '#c9c4ba', redundant: '#c9c4ba', stiff: '#c9c4ba', hedge: '#c9c4ba', // 噪音／可跳过
  lookup: '#57534a', region: '#57534a',                    // 你自己钉的
  note: '#57534a',                                        // 你自己写的批注
}
// 眉批标签的文字色（浅灰在白卡上看不清，另给可读的一档）
const KIND_TEXT_COLOR = {
  insight: '#123a47', warning: '#9d3a25', hype: '#9d3a25', ai: '#9d3a25', conflict: '#9d3a25',
  padding: '#6f6b62', redundant: '#6f6b62', stiff: '#6f6b62', hedge: '#6f6b62',
  lookup: '#3f3230', region: '#3f3230', note: '#3f3230',
}

/* 眉批的档位：三档，就三档。
   九种常用款各自落在一档里；模型自造的类型（kind='custom'）必须自己声明档位，
   颜色按档位走——页边只有三种笔触，读者也只需要分清三种。 */
const BAND_COLOR = { good: '#1d4e5f', warn: '#b8462e', noise: '#8e8a80', mine: '#57534a' }
const BAND_TEXT = { good: '#123a47', warn: '#9d3a25', noise: '#6f6b62', mine: '#3f3230' }
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
  if (typeof n === 'string') return KIND_ZH[n] || ''
  if (!n) return ''
  if (n.kind === 'custom') return n.label || '新批注'   // 模型自造的：用它自己起的那个短标签
  return KIND_ZH[n.kind] || ''
}
export const CORE_ROLES = ['gap', 'claim', 'evidence', 'limitation']
