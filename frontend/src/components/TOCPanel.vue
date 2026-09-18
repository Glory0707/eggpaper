<script setup>
import { computed, ref, watch } from 'vue'
import { api, store } from '../store'
import { t } from '../i18n'

const open = computed(() => store.viewer.tocOpen)
const toc = ref([])
const loading = ref(false)

async function load() {
  if (!store.currentId) return
  const pid = store.currentId
  loading.value = true
  try {
    const r = await api.toc(pid)
    if (store.currentId !== pid) return     // 等待期间换了篇：旧目录不落新篇
    toc.value = r.toc || []
  } catch { toc.value = [] }
  loading.value = false
}

function jump(item) {
  store.viewer.calOpen = false
  store.viewer.tocOpen = false
  store.viewerApi?.gotoPage(item.page + 1)
}

watch(open, v => {
  if (!v) return
  toc.value = []
  load()
}, { immediate: true })
</script>

<template>
  <div class="lib-panel cal-panel">
    <div class="rail-head">
      <span class="mono-label">{{ t('目录') }}</span>
      <button class="ghost head-x" :title="t('收起目录')" @click="store.viewer.tocOpen = false">‹</button>
    </div>

    <div class="cal-empty" v-if="loading">{{ t('读取中…') }}</div>
    <div class="cal-empty" v-else-if="!toc.length">{{ t('这份 PDF 没有书签目录') }}</div>

    <div class="toc-list" v-else>
      <button v-for="(item, i) in toc" :key="i" class="toc-row"
              :style="{ paddingLeft: 10 + (item.level - 1) * 14 + 'px' }"
              :title="item.title" @click="jump(item)">
        <span class="toc-t">{{ item.title }}</span>
        <span class="toc-p">{{ item.page + 1 }}</span>
      </button>
    </div>
  </div>
</template>
