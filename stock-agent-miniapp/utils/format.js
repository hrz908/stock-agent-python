// 数字格式化：金融卡片统一入口（执行指南第 6 节）
// 规则：
// - 保留两位小数、亿/万单位换算、正负号显式
// - 涨跌方向由数值正负推导（direction），页面不手写 up/down class
// - 所有走这里的数据在 WXML 上一律配 class="num"（等宽，见 styles/base.wxss）

// 统一数值解析：null / undefined / 非数字串都归一到 null，由调用方展示 '--'
function toNum(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = parseFloat(v);
  return isNaN(n) ? null : n;
}

// 千分位 + 固定小数位：1234567.891 -> 1,234,567.89；非法值 -> '--'
function fmtNum(v, digits) {
  const d = digits === undefined ? 2 : digits;
  const n = toNum(v);
  if (n === null) return '--';
  const parts = n.toFixed(d).split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return parts.join('.');
}

// 带显式正负号：1.2 -> '+1.20'，-0.5 -> '-0.50'，0 -> '0.00'
function fmtSign(v, digits) {
  const d = digits === undefined ? 2 : digits;
  const n = toNum(v);
  if (n === null) return '--';
  return (n > 0 ? '+' : '') + n.toFixed(d);
}

// 涨跌幅：'+1.23%' / '-0.65%' / '0.00%'
function fmtPct(v, digits) {
  return fmtSign(v, digits) + '%';
}

// 水平值百分比：不带正负号。毛利率 / 净利率 / ROE / 股息率 / 振幅 / 持股比例
// 这类指标没有方向，用 fmtPct 会渲染成「+91.78%」，读起来像同比增速
function fmtPctPlain(v, digits) {
  const d = digits === undefined ? 2 : digits;
  const n = toNum(v);
  if (n === null) return '--';
  return fmtNum(n, d) + '%';
}

// 亿/万单位换算（金额类大数，不带正负号——金额本身没有方向）：
// >= 1 万亿 -> X.XX万亿；>= 1 亿 -> X.XX亿；>= 1 万 -> X.XX万；其余原样千分位
function fmtAmount(v) {
  const n = toNum(v);
  if (n === null) return '--';
  const abs = Math.abs(n);
  if (abs >= 1e12) return fmtNum(n / 1e12) + '万亿';
  if (abs >= 1e8) return fmtNum(n / 1e8) + '亿';
  if (abs >= 1e4) return fmtNum(n / 1e4) + '万';
  return fmtNum(n);
}

// 价格：千分位 + 两位小数
function fmtPrice(v) {
  return fmtNum(v, 2);
}

// 涨跌方向：>0 up（红）、<0 down（绿）、0/空 flat（灰）。
// WXML 侧：class="{{direction(v)}}" 直接拿 'up'/'down'/'flat' 当类名
function direction(v) {
  const n = toNum(v);
  if (n === null || n === 0) return 'flat';
  return n > 0 ? 'up' : 'down';
}

// 全项目统一 ESM：页面、服务、组件一律 import，不再混用 require/module.exports
export { toNum, fmtNum, fmtSign, fmtPct, fmtPctPlain, fmtAmount, fmtPrice, direction };
