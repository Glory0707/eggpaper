<script setup>
/* 品牌标记。两副面孔，共用同一套画布：
 *
 * 【素净·默认】一枚印章：椭圆环 + 几行文字。
 * 为什么不再画"一只蛋"：直接画蛋（哪怕加裂缝）是字面图标——缩到 16px 只剩一坨，
 * 而且永远会被读成水滴或卡通蛋。印章是批注本自己的语言：环是纸的边界，里面是几行字，
 * 最后一行自然短一截（真实的段末就是短的）。椭圆环又刚好是蛋的轮廓——名字里的 egg
 * 靠形状带出来，不靠画。契约也简单：一个环 + 一叠字条，怎么缩都不散。
 *
 * 几何（96 视图）：上下两个半椭圆在最宽处相切（上 ry 34 / 下 ry 42，所以钝端朝上、
 * 下端收），字条的行宽按椭圆在该高度的内接宽度递减，保证任何一档都不顶到环。
 * 小尺寸另备简化稿：环加粗、字条减到三条——16px 画七八条只会糊成一片。
 *
 * 【蛋仔·可选皮肤】圆滚滚一颗，带一道高光。这是玩心，不是主视觉：只在设置里
 * 主动打开时出现，也不学游戏那套糖果色和表情（商标与调性两笔账都不划算）。
 */
import { computed } from 'vue'
import { store } from '../store'

defineProps({ compact: { type: Boolean, default: false } })

const eggy = computed(() => store.viewer.skin === 'egg')

const FULL = [
  { y: 30, w: 36 }, { y: 42, w: 42 }, { y: 54, w: 40 }, { y: 66, w: 22 },
]
const SMALL = [
  { y: 32, w: 30 }, { y: 44, w: 36 }, { y: 56, w: 22 },
]
const H = { full: 5.5, small: 7 }
// 蛋壳里的字条：圆角拉满才显得软
const EGG_BARS = [{ y: 37, w: 28 }, { y: 49, w: 36 }, { y: 61, w: 20 }]
</script>

<template>
  <svg viewBox="6 10 84 84" width="100%" height="100%" role="img" aria-label="eggpaper">
    <!-- ============= 蛋仔皮肤：一颗圆滚的蛋 ============= -->
    <g v-if="eggy" transform="rotate(-7 48 48)">
      <path d="M48 12 C64.5 12 78 30.5 78 50.5 C78 70.5 64.5 86 48 86 C31.5 86 18 70.5 18 50.5 C18 30.5 31.5 12 48 12 Z"
            fill="currentColor" />
      <ellipse cx="33" cy="31" rx="8" ry="5" fill="#fff" opacity="0.4" transform="rotate(-26 33 31)" />
      <g fill="#fff" opacity="0.94">
        <rect v-for="(b, i) in EGG_BARS" :key="i" :x="48 - b.w / 2" :y="b.y - 3.5"
              :width="b.w" height="7" rx="3.5" />
      </g>
    </g>

    <!-- ============= 素净（默认）：一枚印章 ============= -->
    <template v-else>
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
    </template>
  </svg>
</template>
