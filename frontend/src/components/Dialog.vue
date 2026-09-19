<script setup>
import { nextTick, ref, watch } from 'vue'
import { dlg, dlgOk, dlgCancel } from '../dialog'
import { t } from '../i18n'
import { store } from '../store'
import { vDrag } from '../drag'

const inputEl = ref(null)
const primaryEl = ref(null)
function onKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); dlgCancel() }
  else if (e.key === 'Enter') { e.preventDefault(); dlgOk() }
}
watch(() => store.escTick, () => { if (dlg.open) dlgCancel() })
watch(() => dlg.open, v => {
  if (!v) return
  nextTick(() => {
    if (dlg.kind === 'input') { inputEl.value?.focus({ preventScroll: true }); inputEl.value?.select() }
    else primaryEl.value?.focus({ preventScroll: true })   // 确认框也把焦点接住：Enter 即确认，Tab 不再游走出遮罩
  })
})
</script>

<template>
  <Transition name="pop" appear>
    <div class="modal-mask" v-if="dlg.open" @click.self="dlgCancel" @keydown="onKey" tabindex="-1">
      <div class="modal dialog" v-drag>
        <div class="modal-head" data-drag>
          <h3>{{ dlg.title }}</h3>
          <button class="modal-x" :title="t('关闭（Esc）')" @click="dlgCancel">×</button>
        </div>
        <div class="dlg-body" v-if="dlg.body">{{ dlg.body }}</div>
        <input v-if="dlg.kind === 'input'" ref="inputEl" class="dlg-input" v-model="dlg.value"
               :placeholder="dlg.placeholder" @keydown="onKey" />
        <div class="f-actions">
          <button @click="dlgCancel">{{ dlg.cancel }}</button>
          <button class="primary" ref="primaryEl" :class="{ danger: dlg.danger }" @click="dlgOk">{{ dlg.ok }}</button>
        </div>
      </div>
    </div>
  </Transition>
</template>
