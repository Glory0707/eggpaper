<script setup>
import { ref, watch } from 'vue'
import { api, toast } from '../store'
import { t } from '../i18n'
import { saveBlob } from '../save'
import { modalFocus } from '../modalFocus'

/* 批量引用表格导出：勾格式（带示例，用户不用懂格式名）→ 每篇一行出 CSV。
   引用格式全部来自后端缓存的刊头识别（citation meta），本地排版零模型调用；
   没识别过的篇进不了引用列，导完提示去补。 */
const props = defineProps({ open: Boolean, ids: { type: Array, default: () => [] } })
const emit = defineEmits(['update:open'])
const maskEl = ref(null)
modalFocus(maskEl, () => props.open, () => maskEl.value)

const FORMATS = [
  { k: 'title', grp: 'basic', zh: '标题', ex: '面向图像识别的轻量化深度学习研究' },
  { k: 'authors', grp: 'basic', zh: '作者', ex: 'Zhang W, Li N, Wang Q' },
  { k: 'year', grp: 'basic', zh: '年份', ex: '2024' },
  { k: 'gbt7714', grp: 'ref', zh: 'GB/T 7714', ex: 'ZHANG W, LI N, WANG Q. Lightweight deep learning for image recognition[J]. J. Demo Stud., 2024, 12(3): 345-352.' },
  { k: 'apa', grp: 'ref', zh: 'APA 7', ex: 'Zhang, W., Li, N., & Wang, Q. (2024). Lightweight deep learning for image recognition. Journal of Demo Studies, 12(3), 345–352.' },
  { k: 'nature', grp: 'ref', zh: 'Nature 体', ex: 'Zhang, W., Li, N. & Wang, Q. Lightweight deep learning for image recognition. J. Demo Stud. 12, 345–352 (2024).' },
  { k: 'short_y', grp: 'short', zh: '作者 + 年', ex: 'Zhang et al., 2024' },
  { k: 'inline', grp: 'short', zh: '正文括注', ex: '(Zhang et al., 2024)' },
  { k: 'bibtex', grp: 'import', zh: 'BibTeX', ex: '@article{zhang2024lightweight, title={…}, author={…}, year={2024}}' },
  { k: 'doi', grp: 'import', zh: 'DOI', ex: '10.0000/demo.2024.12345' },
]
const GROUPS = [
  { k: 'basic', zh: '基本信息' },
  { k: 'ref', zh: '参考文献条目' },
  { k: 'short', zh: '短引用（PPT / 图注）' },
  { k: 'import', zh: '导入与链接' },
]
const picked = ref(new Set(['gbt7714', 'apa']))
const busy = ref(false)

watch(() => props.open, v => { if (v) picked.value = new Set(['gbt7714', 'apa']) })

function flip(k) {
  const s = new Set(picked.value)
  s.has(k) ? s.delete(k) : s.add(k)
  picked.value = s
}

function csvCell(v) {
  v = String(v ?? '')
  return /[",\n\r]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v
}
function close() { if (!busy.value) emit('update:open', false) }

async function doExport() {
  const fmts = FORMATS.filter(x => picked.value.has(x.k))
  if (!fmts.length) { toast(t('至少选一种格式')); return }
  busy.value = true
  try {
    const rows = await api.citeTable(props.ids)
    const head = fmts.map(f => t(f.zh))
    const lines = [head.map(csvCell).join(',')]
    let missing = 0
    for (const r of rows) {
      if (!r.recognized) missing++
      lines.push(fmts.map(f => {
        if (f.k === 'title') return r.title
        if (f.k === 'authors') return r.authors
        if (f.k === 'year') return r.year
        return (r.cite || {})[f.k] || ''
      }).map(csvCell).join(','))
    }
    // BOM：Excel 打开中文不乱码
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' })
    saveBlob(blob, `eggpaper-引用-${new Date().toISOString().slice(0, 10)}.csv`)
    if (missing) toast(t('{n} 篇未识别过引用，引用列为空', { n: missing }))
    emit('update:open', false)
  } catch (e) {
    toast(e.message)
  }
  busy.value = false
}
</script>

<template>
  <Transition name="pop" appear>
    <div class="modal-mask" ref="maskEl" v-if="open" tabindex="-1" @keydown.esc="close" @click.self="close">
      <div class="modal cite-modal">
        <div class="modal-head">
          <h3>{{ t('导出引用表格') }}</h3>
          <button class="modal-x" :title="t('关闭（Esc）')" @click="close">×</button>
        </div>
        <div class="cite-body">
          <div v-for="g in GROUPS" :key="g.k" class="cite-grp">
            <div class="cite-grp-name">{{ t(g.zh) }}</div>
            <label v-for="f in FORMATS.filter(x => x.grp === g.k)" :key="f.k" class="cite-row"
                   :class="{ on: picked.has(f.k) }" @click.prevent="flip(f.k)">
              <input type="checkbox" :checked="picked.has(f.k)" @change="flip(f.k)" />
              <span class="c-fmt">{{ t(f.zh) }}</span>
              <span class="c-ex">{{ f.ex }}</span>
            </label>
          </div>
        </div>
        <div class="f-actions">
          <button @click="close">{{ t('取消') }}</button>
          <button class="primary" :disabled="busy || !picked.size" @click="doExport">
            {{ busy ? t('导出中…') : t('导出 CSV · {n} 篇', { n: ids.length }) }}
          </button>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.cite-modal { width: min(560px, 92vw); }
.cite-body { max-height: 56vh; overflow-y: auto; padding: 4px 4px 8px; }
.cite-grp { margin-bottom: 10px; }
.cite-grp-name { font-size: var(--fs-xs); color: var(--ink-3); margin: 8px 6px 4px; }
.cite-row {
  display: flex; align-items: baseline; gap: 8px; padding: 5px 8px; border-radius: 6px;
  cursor: pointer;
}
.cite-row:hover { background: var(--paper-2, rgba(0, 0, 0, 0.03)); }
.cite-row.on { background: rgba(29, 78, 95, 0.07); }
.cite-row input { margin-top: 2px; }
.c-fmt { font-weight: 600; font-size: var(--fs-sm); color: var(--ink); white-space: nowrap; }
.c-ex {
  font-size: var(--fs-xs); color: var(--ink-3); overflow: hidden;
  white-space: nowrap; text-overflow: ellipsis; direction: rtl; text-align: left;
}
</style>
