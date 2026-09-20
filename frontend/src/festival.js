/* 节日换装：按日期给蛋换上节日装扮（EggMark 的 skin 皮肤名）。
 *
 * 换装窗口与冲突规则（换装草图 v5 定稿）：
 * - 单日节压过窗口节（6/1 气球、端午小舟那天不戴学士帽；12/20 圣诞帽接围巾的班）；
 * - 农历节日的公历日期逐年漂移，本地优先的应用不背农历历法——内置 2026–2030
 *   对照表（香港天文台公农历对照核算；2028 闰五月端午取第一个五月初五 5/28），
 *   表外年份不换装、也不猜；
 * - 庄重与国家的日子（清明/国庆/重阳/母亲节父亲节/愚人节…）主动不换装。
 *
 * 手动预览：localStorage.setItem('eggpaper:skin', '"christmas"') 后刷新；
 * 清掉（removeItem 或存空串）恢复按日期。
 */

const DAY = 24 * 3600 * 1000

const LUNAR = {
  // 正月初一（春节）；窗口 = 除夕（前一天）到正月初七
  spring: ['2026-02-17', '2027-02-06', '2028-01-26', '2029-02-13', '2030-02-03'],
  // 正月十五（元宵）= 春节 + 14 天，表里直接列出
  lantern: ['2026-03-03', '2027-02-20', '2028-02-09', '2029-02-27', '2030-02-17'],
  // 五月初五（端午）；2028 闰五月，只过第一个
  duanwu: ['2026-06-19', '2027-06-09', '2028-05-28', '2029-06-16', '2030-06-05'],
  // 七月初七（七夕）
  qixi: ['2026-08-19', '2027-08-08', '2028-08-26', '2029-08-16', '2030-08-05'],
  // 八月十五（中秋）
  midautumn: ['2026-09-25', '2027-09-15', '2028-10-03', '2029-09-22', '2030-09-12'],
}

function toDates(list) {
  return list.map(s => typeof s === 'string'
    ? (([y, m, d]) => new Date(y, m - 1, d))(s.split('-').map(Number))
    : s)
}

/* now 落在 [base-before, base+after]（按天）算 true */
function near(now, base, before, after) {
  const t = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  return t >= base.getTime() - before * DAY && t <= base.getTime() + after * DAY
}

function nearAny(now, list, before, after) {
  return toDates(list).some(d => near(now, d, before, after))
}

function between(now, from, to) {
  const k = (now.getMonth() + 1) * 100 + now.getDate()
  const [f, t] = [from, to].map(s => {
    const [m, d] = s.split('-').map(Number)
    return m * 100 + d
  })
  return k >= f && k <= t
}

export function festivalSkin(now = new Date()) {
  let manual = ''
  try { manual = JSON.parse(localStorage.getItem('eggpaper:skin')) || '' } catch { /* 没设过 */ }
  if (manual) return manual

  const y = now.getFullYear()
  const is = (yy, m, d) => now.getFullYear() === yy && now.getMonth() === m - 1 && now.getDate() === d

  /* ---- 单日节（互相之间以及压过窗口节，按这里先后取先） ---- */
  if (is(y, 1, 1) || is(y, 12, 31)) return 'newyear'
  if (is(y, 2, 14)) return 'qixi'                    // 情人节与七夕同款红线结
  if (is(y, 3, 8) || is(y, 9, 10)) return 'flower'   // 妇女节 / 教师节
  if (is(y, 3, 12)) return 'sprout'                  // 植树节
  if (is(y, 4, 23)) return 'book'                    // 读书日
  if (is(y, 5, 1)) return 'flask'                    // 劳动节
  if (is(y, 6, 1)) return 'balloon'                  // 儿童节（压过毕业季）
  if (is(y, 10, 31)) return 'halloween'
  /* ---- 农历窗口 ---- */
  if (nearAny(now, LUNAR.spring, 1, 6)) return 'spring'      // 除夕 – 正月初七
  if (nearAny(now, LUNAR.lantern, 0, 0)) return 'lantern'
  if (nearAny(now, LUNAR.qixi, 0, 0)) return 'qixi'
  if (nearAny(now, LUNAR.midautumn, 0, 0)) return 'midautumn'
  if (nearAny(now, LUNAR.duanwu, 0, 0)) return 'duanwu'
  /* ---- 基督教窗口节 ---- */
  if (between(now, '12-20', '12-26')) return 'christmas'
  /* ---- 窗口节（最长留到最后） ---- */
  if (between(now, '05-15', '06-30')) return 'scholar'       // 毕业季
  if (between(now, '11-07', '12-31') || between(now, '01-01', '02-04')) return 'scarf'  // 立冬 – 立春
  return ''
}
