<script setup>
/* 品牌标记 = 一枚印章：椭圆环 + 几行文字。
 *
 * 为什么不再画"一只蛋"：直接画蛋（哪怕加裂缝）是字面图标——缩到 16px 只剩一坨，
 * 而且永远会被读成水滴或卡通蛋。印章是批注本自己的语言：环是纸的边界，里面是几行字，
 * 最后一行自然短一截（真实的段末就是短的）。椭圆环又刚好是蛋的轮廓——名字里的 egg
 * 靠形状带出来，不靠画。契约也简单：一个环 + 一叠字条，怎么缩都不散。
 *
 * 几何（96 视图）：上下两个半椭圆在最宽处相切（上 ry 34 / 下 ry 42，所以钝端朝上、
 * 下端收），字条的行宽按椭圆在该高度的内接宽度递减，保证任何一档都不顶到环。
 * 小尺寸另备简化稿：环加粗、字条减到三条——16px 画七八条只会糊成一片。
 * （形状试过好几版：掀盖的蛋读起来像"戴帽子的蛋"，实心蛋+横切一刀像贝雷帽，
 *   最后由"文字条堆成蛋形"收敛到"印章里放着几行字"——同一件事，但更耐看也更好缩。）
 */
defineProps({ compact: { type: Boolean, default: false } })

const FULL = [
  { y: 30, w: 36 }, { y: 42, w: 42 }, { y: 54, w: 40 }, { y: 66, w: 22 },
]
const SMALL = [
  { y: 32, w: 30 }, { y: 44, w: 36 }, { y: 56, w: 22 },
]
const H = { full: 5.5, small: 7 }
</script>

<template>
  <svg viewBox="6 10 84 84" width="100%" height="100%" role="img" aria-label="eggpaper">
    <!-- 环：上下两个半椭圆相切，钝端朝上 -->
    <path d="M18 48 A30 34 0 0 1 78 48 A30 42 0 0 1 18 48 Z"
          fill="none" stroke="currentColor" :stroke-width="compact ? 8.5 : 6"
          stroke-linejoin="round" />
    <!-- 里面的字条：宽度跟着椭圆轮廓收，末行短一截 -->
    <g fill="currentColor">
      <rect v-for="(b, i) in (compact ? SMALL : FULL)" :key="i"
            :x="48 - b.w / 2" :y="b.y - (compact ? H.small : H.full) / 2"
            :width="b.w" :height="compact ? H.small : H.full" :rx="(compact ? H.small : H.full) / 2" />
    </g>
  </svg>
</template>
