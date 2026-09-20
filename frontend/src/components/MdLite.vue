<script setup>
import { computed } from 'vue'
import { mdSegs } from '../md'

const props = defineProps({
  text: { type: String, default: '' },
  /* 可选的引用闸门：(run) => boolean。返回 false 的引用不渲染成按钮——
     比如提问里的《别篇》¶n 认不出那篇时，按钮点了也不该有反应，不如诚实给纯文本。 */
  citeOk: { type: Function, default: null },
})
const emit = defineEmits(['cite'])
const lines = computed(() => mdSegs(props.text))
const clickable = r => r.cite && (!props.citeOk || props.citeOk(r))
</script>

<template>
  <div class="md">
    <template v-for="(ln, li) in lines" :key="li">
      <div v-if="ln.blank" class="md-gap"></div>
      <div v-else-if="ln.table" class="md-twrap">
        <table class="md-table">
          <thead>
            <tr>
              <th v-for="(c, ci) in ln.head" :key="ci">
                <template v-for="(r, ri) in c" :key="ri">
                  <button v-if="clickable(r)" class="md-cite" @click="emit('cite', { n: r.cite, ref: r.ref })">{{ r.text }}</button>
                  <code v-else-if="r.code">{{ r.text }}</code>
                  <b v-else-if="r.bold">{{ r.text }}</b>
                  <template v-else>{{ r.text }}</template>
                </template>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, rri) in ln.rows" :key="rri">
              <td v-for="(c, ci) in row" :key="ci">
                <template v-for="(r, ri) in c" :key="ri">
                  <button v-if="clickable(r)" class="md-cite" @click="emit('cite', { n: r.cite, ref: r.ref })">{{ r.text }}</button>
                  <code v-else-if="r.code">{{ r.text }}</code>
                  <b v-else-if="r.bold">{{ r.text }}</b>
                  <template v-else>{{ r.text }}</template>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <component v-else :is="'p'" class="md-line"
                 :class="{ 'md-h': ln.head, 'md-l1': ln.head === 1, 'md-l2': ln.head === 2, 'md-li': ln.bullet }">
        <template v-for="(r, ri) in ln.runs" :key="ri">
          <button v-if="clickable(r)" class="md-cite" @click="emit('cite', { n: r.cite, ref: r.ref })">{{ r.text }}</button>
          <code v-else-if="r.code">{{ r.text }}</code>
          <b v-else-if="r.bold">{{ r.text }}</b>
          <template v-else>{{ r.text }}</template>
        </template>
      </component>
    </template>
  </div>
</template>
