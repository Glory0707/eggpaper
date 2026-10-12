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
  finally {
    // 先于那一行 early-return 之前把 loading 放下（用 finally 保证）：
    // 换篇那一拍旧请求直接 return 的话，spinner 会永远转着等不到人来关
    if (store.currentId === pid) loading.value = false
  }
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
// 开着目录换篇（多窗格点另一格）：抽屉不关，但内容必须是新篇的——只监听 open
// 的话还挂着旧篇目录，点一条会拿旧篇的页码跳到新篇某个不相干的页
watch(() => store.currentId, () => { if (open.value) { toc.value = []; load() } })
</script>

<template>
  <div class="lib-panel cal-panel">
    <div class="rail-head">
      <span class="mono-label">{{ t('目录') }}</span>
      <button class="ghost head-x" :title="t('收起目录')" @click="store.viewer.tocOpen = false">‹</button>
    </div>

    <div class="cal-empty" v-if="loading">{{ t('读取中…') }}</div>
    <div class="cal-empty" v-else-if="!toc.length">{{ t('没有书签目录') }}</div>

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
