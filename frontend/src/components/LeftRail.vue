<script setup>
import { ref } from 'vue'
import { api, store, toast, refreshPapers } from '../store'

const emit = defineEmits(['pick', 'import'])
const fileInput = ref(null)
const over = ref(false)

function onFile(e) {
  emit('import', e.target.files?.[0])
  e.target.value = ''
}
function onDrop(e) {
  over.value = false
  emit('import', e.dataTransfer?.files?.[0])
}
async function del(pid, name) {
  if (!confirm(`删除《${name.slice(0, 30)}…》及其全部批注？`)) return
  await api.deletePaper(pid)
  if (store.currentId === pid) {
    store.currentId = null
    store.paper = null
    store.paras = []
    store.analysis = { status: 'none', claims: [], annotations: {}, error: '' }
    store.marginalia = { status: 'none', notes: [] }
    store.summary = null
    store.qa = []
  }
  await refreshPapers()
  toast('已删除')
}
</script>

<template>
  <aside class="rail-left">
    <div class="rail-head">
      <span class="mono-label">文库 · Library</span>
      <span class="mono-label" style="letter-spacing:.06em">{{ store.papers.length }} 篇</span>
    </div>
    <div class="paper-list">
      <div v-for="p in store.papers" :key="p.id" class="paper-item" :class="{ on: p.id === store.currentId }"
           @click="emit('pick', p.id)">
        <button class="p-del" title="删除" @click.stop="del(p.id, p.title || p.filename)">×</button>
        <div class="fn" :title="p.title || p.filename">{{ p.title || p.filename }}</div>
        <div class="st">
          <i :class="p.analysis_status === 'done' ? 'done' : p.analysis_status === 'error' ? 'err' : ''"
             :title="'骨架 ' + p.analysis_status"></i>
          <i :class="p.marginalia_status === 'done' ? 'done' : p.marginalia_status === 'error' ? 'err' : ''"
             :title="'眉批 ' + p.marginalia_status"></i>
          <i :class="p.translate_status === 'done' ? 'done' : p.translate_status === 'error' ? 'err' : ''"
             :title="'双语 ' + p.translate_status"></i>
        </div>
      </div>
    </div>
    <div class="drop-hint" :class="{ over }" @click="fileInput.click()"
         @dragover.prevent="over = true" @dragleave="over = false" @drop.prevent="onDrop">
      拖入 PDF · 或点击导入<br />
      <span style="font-size:10.5px">解析在本地完成</span>
    </div>
    <div class="rail-foot mono-label" style="line-height:1.7">
      文献 · 术语 · 批注<br />全部留在本机
    </div>
    <input ref="fileInput" type="file" accept="application/pdf" hidden @change="onFile" />
  </aside>
</template>
