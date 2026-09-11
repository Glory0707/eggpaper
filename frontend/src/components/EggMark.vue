<script setup>
// 蛋形：上下两个半椭圆在最宽处相切，所以轮廓连续无折点。
// 上半径 40 < 下半径 52 —— 钝端朝上、尖端朝下。水滴是反过来的（尖在上）。
// 裂缝从左腰爬向右上壳沿，是「剥开壳」的意思；蛋黄那一点是「论证的芯」。
defineProps({ title: { type: String, default: 'eggpaper' } })
</script>

<template>
  <svg viewBox="6 6 88 112" fill="none" xmlns="http://www.w3.org/2000/svg" role="img"
       :aria-label="title">
    <path d="M15 56 A35 40 0 0 1 85 56 A35 52 0 0 1 15 56 Z"
          fill="#FFFDF6" stroke="currentColor" stroke-width="6" stroke-linejoin="round" />
    <circle cx="50" cy="78" r="13.5" fill="#D08A1C" />
    <path class="crack" d="M16 54 L27 47 L36 41 L48 43 L58 35 L67 29 L73 26"
          stroke="currentColor" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" />
  </svg>
</template>
