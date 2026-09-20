<script setup>
import { ref, watch } from 'vue'
import { api, toast } from '../store'
import { t } from '../i18n'
import { modalFocus } from '../modalFocus'

/* 数据对比表的全屏覆盖层：几篇文献 × 六个维度，格子带 ¶ 锚点点回原文。
   文本要能选中复制，所以点遮罩关灯箱的守卫这里同样要有。 */
const props = defineProps({ open: Boolean, ids: { type: Array, default: () => [] } })
const emit = defineEmits(['close', 'goto'])
const maskEl = ref(null)
modalFocus(maskEl, () => props.open, () => closeEl.value)
const closeEl = ref(null)

const loading = ref(false)
const papers = ref([])
const cells = ref({})
const dims = ref([])
const demo = ref(false)
const failed = ref('')

watch(() => props.open, async v => {
  if (!v) return
  loading.value = true
  failed.value = ''
  papers.value = []
  cells.value = {}
  try {
    const r = await api.compare(props.ids)
    papers.value = r.papers
    cells.value = r.cells
    dims.value = r.dims
    demo.value = !!r.demo
  } catch (e) {
    failed.value = e.message
  }
  loading.value = false
})

function closeClick() {
  const s = window.getSelection()
  if (s && !s.isCollapsed) return     // 划选表格文字时松手别把整扇关掉
  emit('close')
}
function onKey(e) { if (e.key === 'Escape') { e.preventDefault(); emit('close') } }

function titleOf(p) { return p.title || p.filename || t('(无标题)') }
function goto(p, n) {
  if (!n) return
  emit('goto', { pid: p.id, n })
  emit('close')      // 跳去读原文了，对比表让位
}
async function copyMd() {
  const head = [' ', ...papers.value.map(p => titleOf(p).slice(0, 24))]
  const lines = [head.join(' | ')]
  lines.push(head.map((h, i) => i ? '---' : '').join(' | '))
  for (const d of dims.value) {
    lines.push([d.label, ...papers.value.map(p => (cells.value[p.id]?.[d.k]?.text || ''))].join(' | '))
  }
  try {
    await navigator.clipboard.writeText(lines.join('\n'))
    toast(t('对比表已复制成 Markdown，可直接粘贴'))
  } catch (e) { toast(e.message) }
}
</script>

<template>
  <Transition name="fade" appear>
    <div class="cmp-mask" ref="maskEl" v-if="open" tabindex="-1" @keydown="onKey" @click.self="closeClick">
      <div class="cmp-panel">
        <div class="cmp-head">
          <span class="mono-label">{{ t('数据对比 · {n} 篇', { n: papers.length }) }}<i v-if="demo" class="demo-flag">演示</i></span>
          <div class="cmp-actions">
            <button v-if="papers.length" @click="copyMd">{{ t('复制 Markdown') }}</button>
            <button class="cmp-x" :title="t('关闭（Esc）')" ref="closeEl" @click="emit('close')">
              <svg viewBox="0 0 12 12" width="11" height="11"><path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
            </button>
          </div>
        </div>

        <div v-if="loading" class="cmp-wait">
          <span class="r-dots">正在逐篇抽取要点</span>…<br />
          <span class="cmp-wait-sub">{{ t('每篇一次模型调用，几篇同时进行，通常十几秒') }}</span>
        </div>
        <div v-else-if="failed" class="cmp-wait">{{ failed }}</div>

        <div class="cmp-scroll" v-else-if="papers.length">
          <table class="cmp-table">
            <thead>
              <tr>
                <th class="dim"></th>
                <th v-for="p in papers" :key="p.id">
                  <span class="cmp-title" :title="titleOf(p)">{{ titleOf(p) }}</span>
                  <span class="cmp-byline" v-if="p.authors || p.year">{{ p.authors }}<template v-if="p.authors && p.year"> · </template>{{ p.year }}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="d in dims" :key="d.k">
                <td class="dim">{{ t(d.label) }}</td>
                <td v-for="p in papers" :key="p.id">
                  <span class="cmp-text">{{ cells[p.id]?.[d.k]?.text }}</span>
                  <button class="cmp-ref" v-if="cells[p.id]?.[d.k]?.ref"
                          :title="t('跳到原文这段')" @click="goto(p, cells[p.id][d.k].ref)">¶{{ cells[p.id][d.k].ref }}</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </Transition>
</template>
