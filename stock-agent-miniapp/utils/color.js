// 取色工具：把 CSS 变量名解析成具体色值。
// 背景：SVG data-URI（图标 / 迷你走势线 / K 线）在编码时颜色已固化，
// 里面的 var(--xx) 不会被页面样式解析，必须先在这里映射成 hex。
// WXML 里可以继续写 var(--xx)，由本工具统一翻译。
//
// 唯一 JS 侧色值镜像：色值只与 styles/token.wxss 的令牌块对应，
// 组件里不允许再出现裸 hex / rgba，需要新色时先加令牌、再加到这里。
// 2026-10-27：变量名随 skill 规范收敛（--text-primary / --bg-page / --primary ...）。
const PALETTE = {
  // 核心 9（zinc 中性色 + 冷蓝主色）
  primary: '#2363C7',
  success: '#16A34A',
  warning: '#F59E0B',
  danger: '#EF4444',
  textPrimary: '#09090B',
  textSecondary: '#71717A',
  textDisabled: '#A1A1AA',
  bgPage: '#FFFFFF',
  border: '#E4E4E7',
  // 扩展角色
  textHint: 'rgba(9, 9, 11, 0.45)',
  textInverse: '#FFFFFF',
  bgCard: '#FFFFFF',
  bgDisabled: '#F4F4F5',
  mask: 'rgba(9, 9, 11, 0.40)',
  // 数据语义（与 token.wxss 的 --up / --down 同值）
  up: '#DC2626',
  down: '#15803D'
};

// 图表数据系列标识色：不随 UI 色板走（浅色主题下金/灰在白底对比度不足，
// 换成蓝 / 琥珀 / 紫这一组常规均线配色）
const CHART_SERIES = {
  ma5: '#2559EB',
  ma10: '#F5A623',
  ma20: '#8B5CF6'
};

// WXML 里写的 var(--xx) -> 具体色值
const COLOR_VARS = {
  'var(--text-primary)': PALETTE.textPrimary,
  'var(--text-inverse)': PALETTE.textInverse,
  'var(--up)': PALETTE.up,
  'var(--down)': PALETTE.down,
  'var(--flat)': PALETTE.textHint,
  'var(--border)': PALETTE.border,
  'var(--bg-page)': PALETTE.bgPage,
  'var(--bg-card)': PALETTE.bgCard,
  'var(--bg-disabled)': PALETTE.bgDisabled
};

// 涨跌方向 -> 色值（国内红涨绿跌）
// 涨比 --danger 深一档（数字按正文读），跌绿比 --success 深一档，两组都不混用
const TREND_COLORS = {
  up: PALETTE.up,
  down: PALETTE.down,
  flat: PALETTE.textHint
};

// 入参可以是 var(--xx)（仅支持上表里的名字）、trend 名或直接 hex
function resolveColor(color, fallback) {
  const raw = color || fallback || PALETTE.textHint;
  if (COLOR_VARS[raw]) return COLOR_VARS[raw];
  if (TREND_COLORS[raw]) return TREND_COLORS[raw];
  return raw;
}

export { PALETTE, CHART_SERIES, COLOR_VARS, TREND_COLORS, resolveColor };
