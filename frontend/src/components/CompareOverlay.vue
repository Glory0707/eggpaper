<script setup>
import { ref, watch } from 'vue'
import { api, toast } from '../store'
import { t } from '../i18n'
import { modalFocus } from '../modalFocus'

/* 数据对比表的全屏覆盖层，两步走：
   ① 选维度——六个维度默认全勾，减掉不要的（每减一个省一份抽取的活）
   ② 抽取呈现——逐篇并行抽，表格格子带 ¶ 锚点点回原文，可复制 Markdown。
   表格文本要能选中复制，点遮罩关灯箱的守卫这里同样要有。 */
const props = defineProps({ open: Boolean, ids: { type: Array, default: () => [] } })
const emit = defineEmits(['close', 'goto'])
const maskEl = ref(null)
modalFocus(maskEl, () => props.open, () => null)
const closeEl = ref(null)

const DIMS_ALL = [
  { k: 'problem', label: '研究问题' },
  { k: 'method', label: '方法' },
  { k: 'system', label: '研究体系' },
  { k: 'datasets', label: '数据' },
  { k: 'results', label: '关键结果' },
  { k: 'limits', label: '局限' },
]
const step = ref('pick')            // pick 选维度 | run 抽取中 | done 表格 | fail
const picked = ref(new Set(DIMS_ALL.map(d => d.k)))
const papers = ref([])
const cells = ref({})
const demo = ref(false)
const failed = ref('')

watch(() => props.open, v => {
  if (!v) return
  step.value = 'pick'
  picked.value = new Set(DIMS_ALL.map(d => d.k))
  papers.value = []
  cells.value = {}
  failed.value = ''
})

function toggleDim(k) {
  const s = new Set(picked.value)
  s.has(k) ? s.delete(k) : s.add(k)
  picked.value = s
}
async function run() {
  if (!picked.value.size) return
  step.value = 'run'
  try {
    const r = await api.compare(props.ids, [...picked.value])
    papers.value = r.papers
    cells.value = r.cells
    dims.value = DIMS_ALL.filter(d => picked.value.has(d.k))
    demo.value = !!r.demo
    step.value = 'done'
  } catch (e) {
    failed.value = e.message
    step.value = 'fail'
  }
}
const dims = ref(DIMS_ALL)

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
          <span class="mono-label">{{ t('数据对比 · {n} 篇', { n: ids.length }) }}<i v-if="demo" class="demo-flag">{{ t('演示') }}</i></span>
          <div class="cmp-actions">
            <button v-if="step === 'done'" class="cmp-ghost" @click="step = 'pick'">{{ t('换维度') }}</button>
            <button v-if="step === 'done'" @click="copyMd">{{ t('复制 Markdown') }}</button>
            <button class="cmp-x" :title="t('关闭（Esc）')" ref="closeEl" @click="emit('close')">
              <svg viewBox="0 0 12 12" width="11" height="11"><path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
            </button>
          </div>
        </div>

        <!-- ① 选维度 -->
        <div class="cmp-pick" v-if="step === 'pick'">
          <p class="cmp-hint">{{ t('勾选要对比的维度') }}</p>
          <div class="cmp-dims">
            <label v-for="d in DIMS_ALL" :key="d.k" class="cmp-dim" :class="{ on: picked.has(d.k) }">
              <input type="checkbox" :checked="picked.has(d.k)" @change="toggleDim(d.k)" />
              <span>{{ t(d.label) }}</span>
            </label>
          </div>
          <div class="f-actions">
            <button @click="emit('close')">{{ t('取消') }}</button>
            <button class="primary" :disabled="!picked.size" @click="run">
              {{ t('开始对比（{n} 个维度）', { n: picked.size }) }}
            </button>
          </div>
        </div>

        <!-- ② 抽取中 -->
        <div v-else-if="step === 'run'" class="cmp-wait">
          <span>{{ t('正在逐篇抽取要点') }}</span><span class="r-dots">…</span><br />
          <span class="cmp-wait-sub">{{ t('每篇一次模型调用，几篇同时进行，通常十几秒') }}</span>
        </div>
        <div v-else-if="step === 'fail'" class="cmp-wait">{{ failed }}</div>

        <!-- ③ 表格 -->
        <div class="cmp-scroll" v-else-if="step === 'done' && papers.length">
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
