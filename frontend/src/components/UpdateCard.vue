<script setup>
/* 更新提示：打开软件时如果更新源里有更新的版本，就在这里说一声。
 *
 * 三种状态一条线走完：**有新版本 → 下载中 → 已就绪**（打包版是"立即重启并安装"，
 * 开发模式是"打开文件夹"，因为正在跑的是源码，装一份发行版没有意义）。
 *
 * 设计上的两个取舍：
 *   ① 不打断首屏。检查是打开软件 6 秒后一次安静的探测（设置里可关），源里没东西
 *      或者网断了都当"没有更新"——用户不该因为一次后台探测看到一个报错。
 *   ② 「稍后」永远在。只有更新源里写了 min_version 且当前版本低于它时（协议不兼容
 *      那种）才不给推迟——那是必须升的。
 */
import { computed, onUnmounted, ref } from 'vue'
import { api, store, toast } from '../store'

const d = computed(() => store.update)
const poll = ref(null)

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
        if (p.state === 'ready') toast('安装包下好了')
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
  // 开发模式（跑的是源码）：下好了自己装。安装包躺在 %TEMP% 里，把它亮出来
  try { await api.revealUpdate(d.value.prog.path) } catch { toast(d.value.prog.path || '') }
}

const pct = computed(() => d.value.prog?.pct || 0)
const state = computed(() => d.value.prog?.state || 'idle')
const mb = n => (n / 1048576).toFixed(1)
</script>

<template>
  <Transition name="pop" appear>
  <div class="modal-mask" v-if="d.show" @click.self="close">
    <div class="modal upd">
      <div class="modal-head">
        <h3>有新版本 {{ d.latest }}</h3>
        <button class="modal-x" title="关闭" @click="close">×</button>
      </div>

      <div class="upd-ver">
        <span class="mono-num">{{ d.current }}</span>
        <span class="upd-arrow">→</span>
        <span class="mono-num upd-new">{{ d.latest }}</span>
        <span v-if="d.pub_date" class="upd-date">{{ d.pub_date }}</span>
        <span v-if="d.size" class="upd-date">{{ mb(d.size) }} MB</span>
      </div>

      <div class="upd-notes" v-if="d.notes">{{ d.notes }}</div>
      <div class="upd-notes muted" v-else>这一版没有写更新说明。</div>

      <!-- 下载中：一条真实的进度（服务端按字节算的，不是假动画） -->
      <div class="upd-prog" v-if="state === 'downloading'">
        <div class="upd-bar"><i :style="{ width: pct + '%' }" /></div>
        <div class="upd-pct">
          <span v-if="d.prog.total">{{ pct }}% · {{ mb(d.prog.got) }} / {{ mb(d.prog.total) }} MB</span>
          <span v-else>已下 {{ mb(d.prog.got) }} MB</span>
        </div>
      </div>
      <div class="upd-note err" v-if="state === 'error'">{{ d.prog.error }}</div>
      <div class="upd-note ok" v-if="state === 'ready' && !installing">
        装好之后软件会自动重启；文库、批注、问答都不会动（它们不在安装目录里）。
      </div>
      <div class="upd-note" v-if="installing">安装器已经拉起，这个窗口可以关了。</div>

      <div class="f-actions">
        <button v-if="!d.required" @click="close">稍后</button>
        <button v-if="state === 'idle' || state === 'error'" class="primary" @click="startDownload">
          {{ state === 'error' ? '重新下载' : (d.packaged ? '下载并安装' : '下载安装包') }}
        </button>
        <button v-if="state === 'ready' && d.packaged" class="primary" @click="install">立即重启并安装</button>
        <button v-if="state === 'ready' && !d.packaged" @click="openFolder">打开安装包所在文件夹</button>
      </div>
    </div>
  </div>
  </Transition>
</template>
