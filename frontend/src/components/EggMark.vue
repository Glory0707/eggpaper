<script setup>
const BARS = [
  { y: 32, w: 30 }, { y: 44, w: 36 }, { y: 56, w: 22 },
]
const BAR_H = 7
</script>

<template>
  <svg viewBox="6 10 84 84" width="100%" height="100%" role="img" aria-label="eggpaper">
        <path d="M18 48 A30 34 0 0 1 78 48 A30 42 0 0 1 18 48 Z"
          fill="none" stroke="currentColor" stroke-width="8.5" stroke-linejoin="round" />
        <g fill="currentColor">
      <rect v-for="(b, i) in BARS" :key="i"
            class="bar" :class="'b' + i"
            :x="48 - b.w / 2" :y="b.y - BAR_H / 2"
            :width="b.w" :height="BAR_H" :rx="BAR_H / 2" />
    </g>
  </svg>
</template>
