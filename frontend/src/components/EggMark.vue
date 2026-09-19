<script setup>
import { computed } from 'vue'

/* 节日换装：几何与三色纪律全部来自 egg-preview.html（换装草图 v5，用户定稿）。
 * 有皮肤时三条字条整体上移（28.5/40.5/52.5），给下半身腾出装扮区；
 * 读书日/中秋/植树节/劳动节这四个「末行变身」皮肤只有前两条字条——第三行就是装扮本身。
 * 装饰只占头顶与下巴两个安全区，不压字条；铅笔画在环之前（被压住的是真被压住）。
 * 无皮肤 = 品牌原样（32/44/56，currentColor），基准几何一分不改。 */
const props = defineProps({ skin: { type: String, default: '' } })

const NORMAL = [
  { y: 32, w: 30 }, { y: 44, w: 36 }, { y: 56, w: 22 },
]
const RAISED = [
  { y: 28.5, w: 30 }, { y: 40.5, w: 36 }, { y: 52.5, w: 22 },
]
const BAR_H = 7
const LAST_BAR_SKINS = ['book', 'midautumn', 'sprout', 'flask']   // 第三行被装扮替换

const bars = computed(() => {
  const base = props.skin ? RAISED : NORMAL
  return LAST_BAR_SKINS.includes(props.skin) ? base.slice(0, 2) : base
})
/* 春节：整枚印章换朱砂（红蛋彩头），几何零改动 */
const sealColor = computed(() => (props.skin === 'spring' ? '#b8462e' : 'currentColor'))
</script>

<template>
  <svg viewBox="6 10 84 84" width="100%" height="100%" role="img" aria-label="eggpaper">
        <g v-if="skin === 'pencil'" transform="translate(70.5 24.3) rotate(25)">
      <path d="M-3.5 -7.5 L0 -15 L3.5 -7.5 Z" fill="#e9e2d0" stroke="#55524a" stroke-width="1.3"/>
      <path d="M-1.4 -12.1 L0 -15 L1.4 -12.1 Z" fill="#1d1b17"/>
      <rect x="-3.5" y="-7.5" width="7" height="24.5" fill="#55524a"/>
    </g>

        <path d="M18 48 A30 34 0 0 1 78 48 A30 42 0 0 1 18 48 Z"
          fill="none" :stroke="sealColor" stroke-width="8.5" stroke-linejoin="round" />
        <g :fill="sealColor">
      <rect v-for="(b, i) in bars" :key="i"
            class="bar" :class="'b' + i"
            :x="48 - b.w / 2" :y="b.y - BAR_H / 2"
            :width="b.w" :height="BAR_H" :rx="BAR_H / 2" />
    </g>

        <g v-if="skin === 'scarf'">
      <path d="M16.5 64.5 A32 12.5 0 0 0 79.5 64.5" fill="none" stroke="#b8462e" stroke-width="10" stroke-linecap="round"/>
      <g transform="rotate(7 67 72)">
        <circle cx="68.5" cy="70" r="5.6" fill="#b8462e"/>
        <rect x="63.4" y="70.5" width="10.2" height="19" rx="5" fill="#b8462e"/>
      </g>
    </g>

        <g v-else-if="skin === 'lantern'">
      <path d="M19 74 Q22 93 48 93.4 Q74 93 77 74 Z" fill="#ffffff" stroke="#1d1b17" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M15.5 71.5 L80.5 71.5" stroke="#1d1b17" stroke-width="4.2" stroke-linecap="round"/>
      <path d="M30 83.5 Q48 89 66 83.5" fill="none" stroke="#b8462e" stroke-width="3" stroke-linecap="round" opacity=".85"/>
    </g>

        <g v-else-if="skin === 'christmas'" transform="rotate(8 48 20)">
      <path d="M35 20.5 L58 20.5 Q63.5 20.2 61.5 14.5 Q60 10.5 52 11.8 Q42 13.5 35 20.5 Z" fill="#b8462e"/>
      <rect x="33.2" y="18.6" width="26" height="7.2" rx="3.6" fill="#f6f6f5" stroke="rgba(29,27,23,.22)" stroke-width="1.4"/>
      <circle cx="60.8" cy="13.6" r="3.4" fill="#f6f6f5" stroke="rgba(29,27,23,.22)" stroke-width="1.4"/>
    </g>

        <g v-else-if="skin === 'newyear'">
      <path d="M38.5 21.5 L48 13 L57.5 21.5 Z" fill="#1d4e5f"/>
      <circle cx="48" cy="12.6" r="2.6" fill="#b8462e"/>
      <circle cx="45.7" cy="17.4" r="1.5" fill="#ffffff" opacity=".9"/>
      <circle cx="50.3" cy="16.8" r="1.5" fill="#ffffff" opacity=".9"/>
      <path d="M40.5 21.5 L55.5 21.5" stroke="#123a47" stroke-width="2.4" stroke-linecap="round"/>
    </g>

        <g v-else-if="skin === 'halloween'">
      <g fill="#b8462e" transform="translate(0 0.6)">
        <ellipse cx="43.4" cy="16.4" rx="3.4" ry="5"/>
        <ellipse cx="52.6" cy="16.4" rx="3.4" ry="5"/>
        <ellipse cx="48" cy="15.9" rx="4.8" ry="5.7"/>
      </g>
      <path d="M47.4 11 C47.1 10.65 48.9 10.65 48.6 11 L48.3 12.6 L47.7 12.6 Z" fill="#1d1b17"/>
      <g fill="#f6f6f5" transform="translate(0 0.6)">
        <path d="M44.4 14.1 L47 14.1 L45.7 16.4 Z"/>
        <path d="M49 14.1 L51.6 14.1 L50.3 16.4 Z"/>
        <path d="M44.2 18.1 L46 19.2 L47.6 18.1 L49.2 19.2 L50.8 18.1 L51.8 18.1 L51.8 19 L49.2 20.3 L47.6 19.2 L46 20.3 L44.2 19 Z"/>
      </g>
    </g>

        <g v-else-if="skin === 'scholar'">
      <path d="M67.2 17 L70.8 17 L70.8 26.5 L67.2 26.5 Z" fill="#1d1b17"/>
      <path d="M67.2 26.5 L70.8 26.5 L70.8 28.1 Q70.8 29.4 69.8 29.4 L68.2 29.4 Q67.2 29.4 67.2 28.1 Z" fill="#b8462e"/>
      <path d="M34 19.5 L34 26.45 L48 29.2 L62 26.45 L62 19.5 L48 24 Z" fill="#55524a"/>
      <path d="M48 10.5 L69 17 L48 23.5 L27 17 Z" fill="#1d1b17"/>
    </g>

        <g v-else-if="skin === 'book'">
      <path d="M46.9 53.2 C43.2 50.7 38.3 49.9 33.8 51.4 L33.8 60.6 C38.3 59.2 43.2 59.9 46.9 62.1 Z" fill="currentColor"/>
      <path d="M49.1 53.2 C52.8 50.7 57.7 49.9 62.2 51.4 L62.2 60.6 C57.7 59.2 52.8 59.9 49.1 62.1 Z" fill="currentColor"/>
    </g>

        <g v-else-if="skin === 'flower'">
      <g fill="#b8462e">
        <circle cx="72.9" cy="15.4" r="2.7"/><circle cx="69.5" cy="12.9" r="2.7"/><circle cx="66.1" cy="15.4" r="2.7"/>
        <circle cx="67.4" cy="19.4" r="2.7"/><circle cx="71.6" cy="19.4" r="2.7"/>
      </g>
      <circle cx="69.5" cy="16.5" r="1.7" fill="#f6f6f5"/>
    </g>

        <g v-else-if="skin === 'sprout'">
      <path d="M48 81 C48 73 48 64 48 57" fill="none" stroke="#4a7f42" stroke-width="2.6" stroke-linecap="round"/>
      <path d="M48 68 C43 68.5 39.5 65.5 38.7 60.5 C44.5 60 47.2 63 48 68 Z" fill="#5e9c52"/>
      <path d="M48 62 C53 62.5 56.5 59.5 57.3 54.5 C51.5 54 48.8 57 48 62 Z" fill="#5e9c52"/>
    </g>

        <g v-else-if="skin === 'flask'">
      <rect x="45.4" y="49.4" width="5.2" height="1.9" rx="0.95" fill="#123a47"/>
      <path d="M45.8 50.8 L50.2 50.8 L50.2 55.6 L55.4 62.2 Q56.8 64.2 54.4 64.2 L41.6 64.2 Q39.2 64.2 40.6 62.2 L45.8 55.6 Z" fill="#123a47"/>
      <path d="M43 59.8 L53 59.8" stroke="rgba(255,255,255,.4)" stroke-width="1.5" stroke-linecap="round" fill="none"/>
      <circle cx="46.6" cy="56.6" r="1" fill="rgba(255,255,255,.35)"/>
      <circle cx="49.4" cy="58.2" r=".8" fill="rgba(255,255,255,.35)"/>
    </g>

        <g v-else-if="skin === 'balloon'">
      <path d="M81.5 11.7 a5.7 5.7 0 0 1 5.7 5.7 c0 4.6 -2.7 7.9 -5.7 7.9 c-3 0 -5.7 -3.3 -5.7 -7.9 a5.7 5.7 0 0 1 5.7 -5.7 Z" fill="#b8462e"/>
      <path d="M78.7 14.4 a2.3 2.3 0 0 1 2.1 -1.5" fill="none" stroke="#f6f6f5" stroke-width="1.3" stroke-linecap="round" opacity=".8"/>
    </g>

        <g v-else-if="skin === 'duanwu'">
      <path d="M15.5 69 Q48 81.5 80.5 69 C76 101.4 20 101.4 15.5 69 Z" fill="#123a47"/>
      <path d="M26 73.5 Q48 85 70 73.5" fill="none" stroke="rgba(255,255,255,.30)" stroke-width="1.6" stroke-linecap="round"/>
      <path d="M24 78 Q48 90 72 78" fill="none" stroke="#b8462e" stroke-width="2.2" stroke-linecap="round"/>
    </g>

        <g v-else-if="skin === 'qixi'">
      <path d="M48 16.2 C38 7.2 25.5 11.2 28.5 20.2 C30.6 26.2 41 25.8 48 16.2 Z" fill="#b8462e"/>
      <path d="M48 16.2 C58 7.2 70.5 11.2 67.5 20.2 C65.4 26.2 55 25.8 48 16.2 Z" fill="#b8462e"/>
      <path d="M45.5 18.5 C44.5 21.5 44.2 24 44.5 26.4 M50.5 18.5 C52 21.4 53.6 23.8 55.8 26" fill="none" stroke="#b8462e" stroke-width="1.9" stroke-linecap="round"/>
      <circle cx="48" cy="16.4" r="3" fill="#8f3822"/>
    </g>

        <g v-else-if="skin === 'midautumn'">
      <g fill="currentColor">
        <circle cx="48" cy="62.5" r="10.6"/>
        <circle cx="57.8" cy="62.5" r="3.4"/><circle cx="54.9" cy="69.4" r="3.4"/><circle cx="48" cy="72.3" r="3.4"/><circle cx="41.1" cy="69.4" r="3.4"/>
        <circle cx="38.2" cy="62.5" r="3.4"/><circle cx="41.1" cy="55.6" r="3.4"/><circle cx="48" cy="52.7" r="3.4"/><circle cx="54.9" cy="55.6" r="3.4"/>
      </g>
      <rect x="44.4" y="58.9" width="7.2" height="7.2" rx="1.2" fill="none" stroke="#ffffff" stroke-width="1.5" transform="rotate(45 48 62.5)"/>
      <circle cx="48" cy="62.5" r="1.9" fill="#b8462e"/>
    </g>
  </svg>
</template>
