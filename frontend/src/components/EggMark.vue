<script setup>
/* 品牌标记 = 一枚印章：椭圆环 + 三条文字条。
 *
 * 为什么不再画"一只蛋"：直接画蛋（哪怕加裂缝）是字面图标——缩到 16px 只剩一坨，
 * 而且永远会被读成水滴或卡通蛋。印章是批注本自己的语言：环是纸的边界，里面是几行字，
 * 最后一行自然短一截（真实的段末就是短的）。椭圆环又刚好是蛋的轮廓——名字里的 egg
 * 靠形状带出来，不靠画。契约也简单：一个环 + 三条字条，怎么缩都不散。
 *
 * **全站只有这一个几何**（大图小图同形）：椭圆环（上半 ry 34 / 下半 ry 42，钝端朝上）
 * + 三条字条（宽 30/36/22，末条自然短一截）。三条线是标识的一部分，不能因为"大图放得下"
 * 就加到四条——首页大图曾经用四条宽条的"full 稿"，和 exe/托盘图标（三条）对不上，
 * 用户一眼就看出来"首页还是四条杠"。backend/mark.py 与这里的数字逐项对应，改这边要同步那边。
 */
const BARS = [
  { y: 32, w: 30 }, { y: 44, w: 36 }, { y: 56, w: 22 },
]
const BAR_H = 7
</script>

<template>
  <svg viewBox="6 10 84 84" width="100%" height="100%" role="img" aria-label="eggpaper">
    <!-- 环：上下两个半椭圆相切，钝端朝上 -->
    <path d="M18 48 A30 34 0 0 1 78 48 A30 42 0 0 1 18 48 Z"
          fill="none" stroke="currentColor" stroke-width="8.5" stroke-linejoin="round" />
    <!-- 里面的字条：宽度跟着椭圆轮廓收，末行短一截 -->
    <g fill="currentColor">
      <rect v-for="(b, i) in BARS" :key="i"
            :x="48 - b.w / 2" :y="b.y - BAR_H / 2"
            :width="b.w" :height="BAR_H" :rx="BAR_H / 2" />
    </g>
  </svg>
</template>
