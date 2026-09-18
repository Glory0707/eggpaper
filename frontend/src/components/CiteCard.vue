<script setup>
import { computed, ref, watch } from 'vue'
import { api, store, toast } from '../store'
import { copyText } from '../clip'
import { t } from '../i18n'

const data = ref(null)      // {meta, groups}
const busy = ref(false)
const err = ref('')

const FIELDS = [
  ['authors', '作者', m => ((m.authors || []).map(a => a.family).filter(Boolean).join('、') ||
                            (m.authors || []).length + ' ' + t('人'))],
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
watch(() => store.escTick, () => { if (store.cite.open) store.cite.open = false })

async function recognize() {
  busy.value = true
  err.value = ''
  try {
    data.value = await api.citation(store.currentId, false, true)
    toast(t('文献信息认好了'))
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

async function copy(row) {
  const ok = await copyText(row.text)
  toast(ok ? t('已复制 · {label}', { label: row.label }) : t('复制没成功，选中文字手动复制一下'))
}
</script>

<template>
  <Transition name="fade">
    <div class="modal-mask" v-if="store.cite.open && store.paper" @click.self="store.cite.open = false">
      <div class="modal cite">
        <div class="modal-head">
          <h3>{{ t('引用这篇') }}</h3>
          <button class="modal-x" :title="t('关闭（Esc）')" @click="store.cite.open = false">×</button>
        </div>

        <div class="cite-note" v-if="err">{{ err }}</div>

                <div class="cite-empty" v-if="!meta && !err">
          <div v-if="busy">{{ t('正在认首页…') }}</div>
          <template v-else>
            <div style="margin-top:2px">
              <button class="primary" @click="recognize">{{ t('识别文献信息') }}</button>
            </div>
          </template>
        </div>

        <template v-if="meta">
          <div class="cite-fields">
            <span class="cf" v-for="f in fields" :key="f.k" :class="{ miss: !f.v }">
              {{ t(f.zh) }} <b>{{ f.v || '—' }}</b>
            </span>
          </div>

          <div class="cite-group" v-for="g in groups" :key="g.k">
            <div class="mono-label">{{ g.name }}</div>
            <button class="cite-row" v-for="r in g.rows" :key="r.k" @click="copy(r)">
              <span class="cr-k">{{ r.label }}<i>{{ r.hint }}</i></span>
              <span class="cr-v">{{ r.text }}</span>
              <span class="cr-cp">{{ t('复制') }}</span>
            </button>
          </div>

          <div class="cite-foot">
            <a v-if="link" :href="link" target="_blank" rel="noreferrer">{{ t('去核对 ↗') }}</a>
            <button class="ghost" :disabled="busy" @click="recognize">
              {{ busy ? t('重认中…') : t('重新识别') }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </Transition>
</template>
