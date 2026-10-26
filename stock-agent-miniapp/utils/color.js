// 取色工具：把 CSS 变量名解析成具体色值。
// 背景：SVG data-URI（图标 / 迷你走势线）在编码时颜色已固化，
// 里面的 var(--xx) 不会被页面样式解析，必须先在这里映射成 hex。
// WXML 里可以继续写 var(--xx)（t-icon 时代的写法），由本工具统一翻译。
// 2026-10-26：随《设计令牌规范 · 最终版》同步（#333 / #666 / #999），取值与 styles/token.wxss 保持一致。
// 改上面 token.wxss 的色值时必须同步改这里，否则「页面换肤、SVG 图标没换」。
const COLOR_VARS = {
  'var(--ink)': '#333333',
  'var(--ink-2)': '#666666',
  'var(--ink-3)': '#999999',
  'var(--up)': '#dc2626',
  'var(--down)': '#16a34a',
  'var(--flat)': '#999999',
  'var(--line)': '#E5E5E5',
  'var(--bg)': '#F5F5F5',
  'var(--surface)': '#FFFFFF',
  'var(--surface-2)': '#F2F2F2',
  'var(--primary-fg)': '#FFFFFF'
};

// 涨跌方向 -> 色值（国内红涨绿跌）
const TREND_COLORS = {
  up: '#dc2626',
  down: '#16a34a',
  flat: '#999999'
};

// 入参可以是 var(--xx)（仅支持上表里的名字）、trend 名或直接 hex
function resolveColor(color, fallback) {
  const raw = color || fallback || '#999999';
  if (COLOR_VARS[raw]) return COLOR_VARS[raw];
  if (TREND_COLORS[raw]) return TREND_COLORS[raw];
  return raw;
}

export { COLOR_VARS, TREND_COLORS, resolveColor };
