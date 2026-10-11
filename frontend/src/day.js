/* 日键（YYYY-MM-DD）：日历格、"今天读过没有"、阅读打点都用它。
   两处各写一份 padStart 拼串时，月份补零这类细节迟早有一处漏。 */

export function dayKey(yy, mm, d) {
  return `${yy}-${String(mm).padStart(2, '0')}-${String(d).padStart(2, '0')}`
}

export function todayKey(now = new Date()) {
  return dayKey(now.getFullYear(), now.getMonth() + 1, now.getDate())
}
