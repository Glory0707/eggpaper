<script setup>
import { computed, ref, watch } from 'vue'
import { api, store, toast, refreshCollections } from '../store'
import { t } from '../i18n'
import { modalFocus } from '../modalFocus'

/* 从 Zotero 挑文献导入：条目列表来自它的本地 API，导入走 import-path 管线
   （拷贝进库 + 指纹去重），再把可信的标题/作者/年份回填进去。单向、一次性。 */
const props = defineProps({ open: Boolean })
const emit = defineEmits(['update:open', 'imported'])
const maskEl = ref(null)
modalFocus(maskEl, () => props.open, () => searchEl.value)
const searchEl = ref(null)

const items = ref([])
const q = ref('')
const sel = ref(new Set())
const loading = ref(false)
const busy = ref(false)

watch(() => props.open, async v => {
  if (!v) return
  q.value = ''
  sel.value = new Set()
  items.value = []
  loading.value = true
  try {
    items.value = (await api.zoteroItems()).items
  } catch (e) {
    toast(e.message)
    close()
  }
  loading.value = false
})
function close() {
  if (busy.value) return   // 导入进行中关掉窗，后台循环还在跑、界面却零痕迹——busy 时不给关
  emit('update:open', false)
}

const shown = computed(() => {
  const kw = q.value.trim().toLowerCase()
  if (!kw) return items.value
  return items.value.filter(x =>
    `${x.title} ${x.authors} ${x.year}`.toLowerCase().includes(kw))
})
function toggle(x) {
  if (!x.pdf) return                      // 没有 PDF 附件的条目导进来只是一行字，不让勾
  const s = new Set(sel.value)
  s.has(x.key) ? s.delete(x.key) : s.add(x.key)
  sel.value = s
}
async function doImport() {
  const picks = items.value.filter(x => sel.value.has(x.key))
  if (!picks.length) return
  busy.value = true
  let ok = 0
  for (const x of picks) {
    try {
      const r = await api.importPath(x.pdf)
      if (!r.duplicate) {
        await api.paperMeta(r.paper.id, { title: x.title, authors: x.authors, year: x.year })
        ok++
        const c = store.lib.coll
        if (typeof c === 'number') { try { await api.paperColls(r.paper.id, [c]) } catch { /* 归类失败不影响导入 */ } }
      }
    } catch (e) {
      toast(t('《{name}》导入失败：{m}', { name: x.title.slice(0, 20), m: e.message }))
    }
  }
  busy.value = false
  close()
  if (ok) {
    toast(t('已从 Zotero 导入 {n} 篇，后台通读中', { n: ok }))
    emit('imported')
  } else {
    toast(t('选中的都已在库里'))
  }
  refreshCollections()
}
</script>

<template>
  <Transition name="pop" appear>
    <div class="modal-mask" ref="maskEl" v-if="open" tabindex="-1" @keydown.esc="close" @click.self="close">
      <div class="modal zot-modal">
        <div class="modal-head">
          <h3>{{ t('从 Zotero 导入') }}</h3>
          <button class="modal-x" :title="t('关闭（Esc）')" @click="close">×</button>
        </div>
        <div class="zot-tools">
          <input ref="searchEl" type="text" v-model="q" :placeholder="t('搜标题 / 作者…')" class="lib-search" />
          <span class="zot-count">{{ t('{n} 条 · 已勾 {m}', { n: items.length, m: sel.size }) }}</span>
        </div>
        <div class="zot-list">
          <div v-for="x in shown" :key="x.key" class="zot-row"
               :class="{ off: !x.pdf, on: sel.has(x.key) }" @click="toggle(x)">
            <i class="p-check" :class="{ on: sel.has(x.key), dis: !x.pdf }">
              <svg viewBox="0 0 12 12" width="10" height="10"><path d="M2 6.2 4.8 9 10 3.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </i>
            <span class="z-title">{{ x.title }}</span>
            <span class="z-meta" v-if="x.authors || x.year">{{ x.authors }}<template v-if="x.authors && x.year"> · </template>{{ x.year }}</span>
            <span class="z-nopdf" v-if="!x.pdf">{{ t('无 PDF') }}</span>
          </div>
          <div v-if="loading" class="zot-empty">{{ t('正在读 Zotero 的库…') }}</div>
          <div v-else-if="!shown.length" class="zot-empty">{{ t('没有匹配的条目。') }}</div>
        </div>
        <div class="f-actions">
          <button @click="close">{{ t('取消') }}</button>
          <button class="primary" :disabled="busy || !sel.size" @click="doImport">
            {{ busy ? t('导入中…') : t('导入 {n} 篇', { n: sel.size }) }}
          </button>
        </div>
      </div>
    </div>
  </Transition>
</template>
