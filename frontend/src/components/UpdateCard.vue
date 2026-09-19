<script setup>
import { computed, onUnmounted, ref } from 'vue'
import { api, store, toast } from '../store'
import { t } from '../i18n'
import { modalFocus } from '../modalFocus'

const d = computed(() => store.update)
const poll = ref(null)
const maskEl = ref(null)
modalFocus(maskEl, computed(() => !!d.value.show))

function close() {
  stopPoll()
  store.update = { ...store.update, show: false }
}
function stopPoll() {
  if (poll.value) { clearInterval(poll.value); poll.value = null }
}
onUnmounted(stopPoll)

async function startDownload() {
  try {
    await api.updateDownload({ url: d.value.url, sha256: d.value.sha256, size: d.value.size })
    stopPoll()
    poll.value = setInterval(async () => {
      const p = await api.updateProgress()
      store.update = { ...store.update, prog: p }
      if (p.state === 'ready' || p.state === 'error') {
        stopPoll()
        if (p.state === 'ready') toast(t('安装包下好了'))
      }
    }, 500)
  } catch (e) { toast(e.message) }
}

async function install() {
  try {
    await api.updateInstall(d.value.prog.path)
    store.update = { ...store.update, installing: true }
  } catch (e) { toast(e.message) }
}
async function openFolder() {
  try { await api.revealUpdate(d.value.prog.path) } catch { toast(d.value.prog.path || '') }
}

const pct = computed(() => d.value.prog?.pct || 0)
const state = computed(() => d.value.prog?.state || 'idle')
const mb = n => (n / 1048576).toFixed(1)
</script>

<template>
  <Transition name="pop" appear>
  <div class="modal-mask" v-if="d.show" @click.self="close" ref="maskEl">
    <div class="modal upd">
      <div class="modal-head">
        <h3>{{ t('有新版本 {v}', { v: d.latest }) }}</h3>
        <button class="modal-x" :title="t('关闭')" @click="close">×</button>
      </div>

      <div class="upd-ver">
        <span class="mono-num">{{ d.current }}</span>
        <span class="upd-arrow">→</span>
        <span class="mono-num upd-new">{{ d.latest }}</span>
        <span v-if="d.pub_date" class="upd-date">{{ d.pub_date }}</span>
        <span v-if="d.size" class="upd-date">{{ mb(d.size) }} MB</span>
      </div>

      <div class="upd-notes" v-if="d.notes">{{ d.notes }}</div>
      <div class="upd-notes muted" v-else>{{ t('这一版没有写更新说明。') }}</div>

            <div class="upd-prog" v-if="state === 'downloading'">
        <div class="upd-bar"><i :style="{ width: pct + '%' }" /></div>
        <div class="upd-pct">
          <span v-if="d.prog.total">{{ pct }}% · {{ mb(d.prog.got) }} / {{ mb(d.prog.total) }} MB</span>
          <span v-else>{{ t('已下 {m} MB', { m: mb(d.prog.got) }) }}</span>
        </div>
      </div>
      <div class="upd-note err" v-if="state === 'error'">{{ t(d.prog.error) }}</div>
      <div class="upd-note ok" v-if="state === 'ready' && !d.installing">
        {{ t('装好之后软件会自动重启；文库、批注、问答都不会动（它们不在安装目录里）。') }}
      </div>
      <div class="upd-note" v-if="d.installing">{{ t('安装器已经拉起，这个窗口可以关了。') }}</div>

      <div class="f-actions">
        <button v-if="!d.required" @click="close">{{ t('稍后') }}</button>
        <button v-if="state === 'idle' || state === 'error'" class="primary" @click="startDownload">
          {{ state === 'error' ? t('重新下载') : (d.packaged ? t('下载并安装') : t('下载安装包')) }}
        </button>
        <button v-if="state === 'ready' && d.packaged" class="primary" @click="install">{{ t('立即重启并安装') }}</button>
        <button v-if="state === 'ready' && !d.packaged" @click="openFolder">{{ t('打开安装包所在文件夹') }}</button>
      </div>
    </div>
  </div>
  </Transition>
</template>
