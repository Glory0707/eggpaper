<script setup>
import { nextTick, ref, watch } from 'vue'
import { dlg, dlgOk, dlgCancel } from '../dialog'
import { t } from '../i18n'
import { vDrag } from '../drag'
import { modalFocus } from '../modalFocus'

const inputEl = ref(null)
const primaryEl = ref(null)
const maskEl = ref(null)
/* 焦点由 modalFocus 统一接管：开弹落输入框/主按钮，Tab 圈闭不出遮罩，关弹归还焦点。 */
modalFocus(maskEl, () => dlg.open, () => (dlg.kind === 'input' ? inputEl.value : primaryEl.value))
function onKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); dlgCancel() }
  else if (e.key === 'Enter') { e.preventDefault(); dlgOk() }
}
watch(() => dlg.open, v => {
  if (v && dlg.kind === 'input') nextTick(() => inputEl.value?.select())   // 输入框默认全选，直接打字就覆盖
})
</script>

<template>
  <Transition name="pop" appear>
    <div class="modal-mask" ref="maskEl" v-if="dlg.open" @click.self="dlgCancel" @keydown="onKey" tabindex="-1">
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
