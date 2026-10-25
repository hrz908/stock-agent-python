// 取色工具：把 CSS 变量名解析成具体色值。
// 背景：SVG data-URI（图标 / 迷你走势线）在编码时颜色已固化，
// 里面的 var(--xx) 不会被页面样式解析，必须先在这里映射成 hex。
// WXML 里可以继续写 var(--xx)（t-icon 时代的写法），由本工具统一翻译。
// 2026-10-22：随 shadcn 主题重构同步（zinc 色阶），取值与 styles/token.wxss 保持一致。
// 改上面 token.wxss 的色值时必须同步改这里，否则「页面换肤、SVG 图标没换」。
const COLOR_VARS = {
  'var(--ink)': '#09090b',
  'var(--ink-2)': '#71717a',
  'var(--ink-3)': '#a1a1aa',
  'var(--up)': '#dc2626',
  'var(--down)': '#16a34a',
  'var(--flat)': '#71717a',
  'var(--accent)': '#2559EB',
  'var(--line)': '#e4e4e7',
  'var(--bg)': '#ffffff',
  'var(--surface)': '#ffffff',
  'var(--surface-2)': '#f4f4f5',
  // 主按钮文字色（zinc-50）：深底上的反白图标要用它（如语音态胶囊里的模式键）
  'var(--primary-fg)': '#fafafa'
};

// 涨跌方向 -> 色值（国内红涨绿跌）
const TREND_COLORS = {
  up: '#dc2626',
  down: '#16a34a',
  flat: '#71717a'
};

// 入参可以是 var(--xx)（仅支持上表里的名字）、trend 名或直接 hex
function resolveColor(color, fallback) {
  const raw = color || fallback || '#5F6875';
  if (COLOR_VARS[raw]) return COLOR_VARS[raw];
  if (TREND_COLORS[raw]) return TREND_COLORS[raw];
  return raw;
}

module.exports = {
  COLOR_VARS: COLOR_VARS,
  TREND_COLORS: TREND_COLORS,
  resolveColor: resolveColor
};
