// 迷你走势线：SVG polyline 手写（执行指南第 2 节选型：几十个数据点，
// 不值得引图表库）。整段 SVG 编码成 data-URI 背景图，viewBox 固定、
// preserveAspectRatio="none" 拉伸铺满容器——WXSS 禁用 url() 本地图，
// data-URI 是唯一的内联方式。
// 颜色在编码时固化：trend（up/down/flat）或 color（hex / var(--xx)）二选一。
const { resolveColor } = require('../../../utils/color');
const { svgToDataUri } = require('../../../utils/svg-uri');

// viewBox 尺寸：与常见卡片内容宽（约 345px）接近，缩放失真最小
const VB_W = 340;
const VB_H = 88;
// 上下留白，避免折线贴边
const PAD = 6;
// 端点圆半径：左右各让出这么多，否则最后一个点会被 viewBox 裁掉半边
const DOT = 3;

function buildSrc(points, color, strokeWidth, fill) {
  const nums = (points || []).map(Number).filter((n) => !isNaN(n));
  if (nums.length < 2) return '';

  const min = Math.min.apply(null, nums);
  const max = Math.max.apply(null, nums);
  const span = max - min || 1;
  const pts = nums.map((v, i) => {
    const x = DOT + (i / (nums.length - 1)) * (VB_W - DOT * 2);
    const y = PAD + (1 - (v - min) / span) * (VB_H - PAD * 2);
    return [Number(x.toFixed(1)), Number(y.toFixed(1))];
  });
  const coords = pts.map((p) => p[0] + ',' + p[1]).join(' ');
  const last = pts[pts.length - 1];

  const sw = strokeWidth || 3;
  // 面积填充的折线要闭合到viewBox底边
  const areaCoords = coords + ' ' + VB_W + ',' + VB_H + ' 0,' + VB_H;
  const svg =
    // 显式写 width/height：无固有尺寸的 SVG 作背景图时，各引擎兜底尺寸不一致
    "<svg xmlns='http://www.w3.org/2000/svg' width='" +
    VB_W +
    "' height='" +
    VB_H +
    "' viewBox='0 0 " +
    VB_W +
    ' ' +
    VB_H +
    "' preserveAspectRatio='none'>" +
    // 面积填充用平铺淡色而非渐变：渐变在 96rpx 高度上只会糊成一层灰
    (fill
      ? "<polyline fill='" +
        color +
        "' fill-opacity='0.08' stroke='none' points='" +
        areaCoords +
        "'/>"
      : '') +
    "<polyline fill='none' stroke='" +
    color +
    "' stroke-width='" +
    sw +
    "' stroke-linecap='round' stroke-linejoin='round' points='" +
    coords +
    "'/>" +
    // 末端圆点：给走势一个「现在」的落点，也是整条线唯一的实心记号
    "<circle cx='" +
    last[0] +
    "' cy='" +
    last[1] +
    "' r='" +
    DOT +
    "' fill='" +
    color +
    "'/>" +
    '</svg>';

  return svgToDataUri(svg);
}

Component({
  externalClasses: ['spark-class'],
  properties: {
    // 分时点位数组（数值即可，归一化在组件内做）
    points: { type: Array, value: [] },
    // 趋势：up 红 / down 绿 / flat 灰；与 color 二选一，color 优先
    trend: { type: String, value: '' },
    // 直接指定颜色（hex 或 var(--xx)）
    color: { type: String, value: '' },
    // 线宽（viewBox 单位）
    strokeWidth: { type: Number, value: 3 },
    // 是否填充线下渐变面积
    fill: { type: Boolean, value: true }
  },
  data: {
    src: ''
  },
  observers: {
    'points, trend, color, strokeWidth, fill'() {
      this.render();
    }
  },
  methods: {
    render() {
      const nums = (this.data.points || []).map(Number).filter((n) => !isNaN(n));
      if (nums.length < 2) {
        this.setData({ src: '' });
        return;
      }
      const color = resolveColor(this.data.color, resolveColor(this.data.trend, '#dc2626'));
      this.setData({ src: buildSrc(nums, color, this.data.strokeWidth, this.data.fill) });
    }
  }
});
