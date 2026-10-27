// 取色工具：把 CSS 变量名解析成具体色值。
// 背景：SVG data-URI（图标 / 迷你走势线）在编码时颜色已固化，
// 里面的 var(--xx) 不会被页面样式解析，必须先在这里映射成 hex。
// WXML 里可以继续写 var(--xx)（t-icon 时代的写法），由本工具统一翻译。
// 2026-10-27：随「宣纸 · 墨 · 一点青」色板同步（#F2EDE6 / #DCD1CB / #36311B / #15803D），
// 取值与 styles/token.wxss 保持一致。--ink / --ink-2 / --ink-3 在 token 里同指主字色，这里同值。
// 改下面 token.wxss 的色值时必须同步改这里，否则「页面换肤、SVG 图标没换」。
const COLOR_VARS = {
  'var(--ink)': '#36311B',
  'var(--ink-2)': '#36311B',
  'var(--ink-3)': '#36311B',
  'var(--up)': '#dc2626',
  'var(--down)': '#15803D',
  'var(--flat)': 'rgba(54, 49, 27, 0.45)',
  'var(--line)': '#DCD1CB',
  'var(--bg)': '#F2EDE6',
  'var(--surface)': '#FFFFFF',
  'var(--surface-2)': 'rgba(54, 49, 27, 0.12)',
  'var(--primary-fg)': '#FFFFFF'
};

// 涨跌方向 -> 色值（国内红涨绿跌）
const TREND_COLORS = {
  up: '#dc2626',
  down: '#15803D',
  flat: 'rgba(54, 49, 27, 0.45)'
};

// 入参可以是 var(--xx)（仅支持上表里的名字）、trend 名或直接 hex
function resolveColor(color, fallback) {
  const raw = color || fallback || 'rgba(54, 49, 27, 0.45)';
  if (COLOR_VARS[raw]) return COLOR_VARS[raw];
  if (TREND_COLORS[raw]) return TREND_COLORS[raw];
  return raw;
}

export { COLOR_VARS, TREND_COLORS, resolveColor };
