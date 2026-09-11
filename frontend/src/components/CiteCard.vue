<script setup>
/* 「引用这篇」浮层：长条目 / 短引用 / 导入格式，点一行就复制走。
 *
 * 为什么开在顶栏标题旁边（不是塞进右栏某个页签里）：引用格式是论文的**身份信息**
 * （作者、刊名、卷期页、DOI），和标题是同一族数据；而右栏四个页签都属于"读"，
 * 把它放进任一个都会污染那一栏的语义。它也不是"读"的信息——是"把这篇带走"。
 *
 * 字段是从首页印刷块里抄的（后端 llm.extract_citation），排版在后端 citation.py 做。
 * 这里只负责显示和复制；缺哪个字段就明着空着，不猜。
 */
import { computed, ref, watch } from 'vue'
import { api, store, toast } from '../store'
import { copyText } from '../clip'

const data = ref(null)      // {meta, groups}
const busy = ref(false)
const err = ref('')

const FIELDS = [
  ['authors', '作者', m => ((m.authors || []).map(a => a.family).filter(Boolean).join('、') ||
                            (m.authors || []).length + ' 人')],
  ['journal', '期刊', m => m.journal || m.journal_abbr],
  ['year', '年份', m => m.year],
  ['volume', '卷', m => m.volume],
  ['issue', '期', m => m.issue],
  ['pages', '页码', m => m.pages],
  ['doi', 'DOI', m => m.doi],
  ['preprint', '预印本', m => m.preprint],
]
const meta = computed(() => data.value?.meta || null)
const groups = computed(() => data.value?.groups || [])
// 这一行是给自己核对的：哪些字段认出来了（认不出来的虚着显示），一眼就看得出
const fields = computed(() => meta.value
  ? FIELDS.map(([k, zh, get]) => ({ k, zh, v: String(get(meta.value) || '').trim() }))
  : [])
const link = computed(() => {
  const d = meta.value?.doi
  if (d) return `https://doi.org/${d}`
  const m = String(meta.value?.preprint || '').match(/(\d{4}\.\d{4,5})/)
  return m ? `https://arxiv.org/abs/${m[1]}` : ''
})

async function load() {
  const pid = store.currentId
  if (!pid) return
  err.value = ''
  try {
    const r = await api.citation(pid, true)
    if (store.currentId !== pid) return      // 期间换了论文：回来的不是当前这篇就丢掉
    data.value = r
  } catch (e) { err.value = e.message }
}

watch(() => store.cite.open, v => { if (v) { data.value = null; load() } })
watch(() => store.currentId, () => { if (store.cite.open) { data.value = null; load() } })
// Esc：全局那个按键处理器负责开关（App.vue），这里跟着 escTick 收一下就够了
watch(() => store.escTick, () => { if (store.cite.open) store.cite.open = false })

async function recognize() {
  busy.value = true
  err.value = ''
  try {
    data.value = await api.citation(store.currentId, false, true)
    toast('文献信息认好了')
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

async function copy(row) {
  const ok = await copyText(row.text)
  toast(ok ? `已复制 · ${row.label}` : '复制没成功，选中文字手动复制一下')
}
</script>

<template>
  <Transition name="fade">
    <div class="modal-mask" v-if="store.cite.open && store.paper" @click.self="store.cite.open = false">
      <div class="modal cite">
        <div class="modal-head">
          <h3>引用这篇</h3>
          <button class="modal-x" title="关闭（Esc）" @click="store.cite.open = false">×</button>
        </div>

        <div class="cite-note" v-if="err">{{ err }}</div>

        <!-- 还没认过：把花不花这次调用交给用户决定（一次模型调用 = 一次授权） -->
        <div class="cite-empty" v-if="!meta && !err">
          <div v-if="busy">正在认首页<span class="r-dots">…</span></div>
          <template v-else>
            <div>参考文献要的作者、刊名、卷期页、DOI 就印在首页的刊头、页脚和页边水印上。
              认一次（一次模型调用）存下来，之后各种格式都在本地排。</div>
            <div style="margin-top:14px">
              <button class="primary" @click="recognize">识别文献信息</button>
            </div>
          </template>
        </div>

        <template v-if="meta">
          <div class="cite-fields">
            <span class="cf" v-for="f in fields" :key="f.k" :class="{ miss: !f.v }">
              {{ f.zh }} <b>{{ f.v || '—' }}</b>
            </span>
          </div>

          <div class="cite-group" v-for="g in groups" :key="g.k">
            <div class="mono-label">{{ g.name }}</div>
            <button class="cite-row" v-for="r in g.rows" :key="r.k" @click="copy(r)"
                    :title="`点一下就复制 · ${r.hint}`">
              <span class="cr-k">{{ r.label }}<i>{{ r.hint }}</i></span>
              <span class="cr-v">{{ r.text }}</span>
              <span class="cr-cp">复制</span>
            </button>
          </div>

          <div class="cite-foot">
            <span>抄自 PDF 首页的刊头、页脚与页边水印，投稿前核对一眼</span>
            <a v-if="link" :href="link" target="_blank" rel="noreferrer">去核对 ↗</a>
            <button class="ghost" :disabled="busy" @click="recognize">
              {{ busy ? '重认中…' : '重新识别' }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </Transition>
</template>
