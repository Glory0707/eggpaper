<script setup>
defineProps({ cap: { type: Boolean, default: false } })
</script>

<template>
  <svg viewBox="0 0 100 120" fill="none" xmlns="http://www.w3.org/2000/svg">
    <!-- 蛋体 -->
    <path d="M50 9 C32 26 17 55 17 78 a33 35 0 0 0 66 0 C83 55 68 26 50 9 Z"
          fill="#FFFDF6" stroke="currentColor" stroke-width="6" stroke-linejoin="round" />
    <!-- 蛋黄：内核 -->
    <circle cx="50" cy="80" r="15" fill="#D08A1C" />
    <!-- 裂纹 -->
    <path d="M21 47 L30 39 L40 47 L50 39 L60 47 L70 39 L79 45"
          stroke="currentColor" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" />
    <!-- 揭开的壳盖（大尺寸才讲这个故事） -->
    <g v-if="cap">
      <path d="M24 44 C30 28 38 15 50 10 C62 15 72 28 76 44 L67 38 L58 45 L50 39 L42 45 L33 39 Z"
            transform="rotate(-10 50 40)" fill="#FFFDF6"
            stroke="currentColor" stroke-width="5" stroke-linejoin="round" />
    </g>
  </svg>
</template>
