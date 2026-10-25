<script setup>
import { ref, watch } from 'vue'
import { api, toast } from '../store'
import { t, isEn } from '../i18n'
import { modalFocus } from '../modalFocus'

/* 文库多选「转发」：选一种版式 → 后端把文件复制上 Windows 剪贴板（文件列表）→
   到微信 / QQ 聊天框 Ctrl+V 即发。应用够不着"替用户挑聊天对象"，文件上剪贴板
   是离聊天输入框最近的一步。缺该版式的篇由后端跳过并点名。 */
const props = defineProps({ open: Boolean, ids: { type: Array, default: () => [] } })
const emit = defineEmits(['update:open'])
const maskEl = ref(null)
modalFocus(maskEl, () => props.open, () => maskEl.value)

const KINDS = [
  { k: 'orig', zh: '原文', ex: '未翻译的原件' },
  { k: 'mono', zh: '译文', ex: '整版中文' },
  { k: 'seq', zh: '双语 · 顺排', ex: '一页原文、一页译文逐页相间' },
  { k: 'lr', zh: '双语 · 并排', ex: '每页左原文、右译文' },
]
const picked = ref('orig')
const busy = ref(false)
watch(() => props.open, v => { if (v) picked.value = 'orig' })

function close() { if (!busy.value) emit('update:open', false) }

async function doForward() {
  busy.value = true
  try {
    const r = await api.forward(props.ids, picked.value)
    toast(r.skipped?.length
      ? t('已复制 {n} 个，{m} 篇没有该版式；到聊天框 Ctrl+V', { n: r.copied.length, m: r.skipped.length })
      : t('已复制 {n} 个 PDF，到聊天框 Ctrl+V', { n: r.copied.length }))
    emit('update:open', false)
  } catch (e) { toast(e.message) }
  busy.value = false
}
</script>

<template>
  <Transition name="pop" appear>
    <div class="modal-mask" ref="maskEl" v-if="open" tabindex="-1" @keydown.esc="close" @click.self="close">
      <div class="modal cite-modal">
        <div class="modal-head">
          <h3>{{ t('转发 PDF') }}</h3>
          <button class="modal-x" :title="t('关闭')" @click="close">×</button>
        </div>
        <div class="cite-body">
          <div class="cite-grp">
            <div class="cite-grp-name">{{ t('版式') }}</div>
            <!-- 英文模式与译文/双语入口同口径：只留原文 -->
            <label v-for="kd in KINDS.filter(x => isEn() ? x.k === 'orig' : true)" :key="kd.k" class="cite-row"
                   :class="{ on: picked === kd.k }" @click.prevent="picked = kd.k">
              <input type="radio" :checked="picked === kd.k" @change="picked = kd.k" />
              <span class="c-fmt">{{ t(kd.zh) }}</span>
              <span class="c-ex">{{ t(kd.ex) }}</span>
            </label>
          </div>
          <div class="fwd-hint">{{ t('复制后在微信 / QQ 聊天框粘贴发送') }}</div>
        </div>
        <div class="f-actions">
          <button @click="close">{{ t('取消') }}</button>
          <button class="primary" :disabled="busy" @click="doForward">
            {{ busy ? t('复制中…') : t('复制 {n} 篇', { n: ids.length }) }}
          </button>
        </div>
      </div>
    </div>
  </Transition>
</template>
