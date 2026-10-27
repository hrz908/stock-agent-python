// 走势与 K 线卡：回答「最近走势怎么样」「站上 20 日线了吗」
// 字段口径见设计文档第 2 节②。
// 图表用 SVG 手绘（蜡烛 + MA5/10/20 + 成交量柱），整段编码成 data-URI 背景图。
// 选型说明：执行指南第 2 节把「完整 K 线」定为 uCharts（分包，约 170KB），
// 但对话流内的 K 线概览不需要十字光标 / 缩放手势，且引入 uCharts 需要
// 新建分包 + 构建 npm，与当前「主包 ≤2MB、零新增依赖」的改造节奏冲突；
// 这里按指南对「迷你走势线」的同一思路（SVG polyline 手写）扩展到 K 线，
// 数据字段与版式与设计稿一致，后续若要交互式图表可平滑替换图表区。
import { toNum, fmtNum, fmtPct, fmtPctPlain, direction } from '../../../utils/format';
import { svgToDataUri } from '../../../utils/svg-uri';
import { CHART_SERIES, PALETTE, TREND_COLORS } from '../../../utils/color';

// viewBox 与分区（对齐视觉稿：K 线区 ≥180rpx，下方成交量）
const VB_W = 340;
const VB_H = 200;
const PRICE_TOP = 10;
const PRICE_BOTTOM = 138;
const VOL_TOP = 150;
const VOL_BOTTOM = 192;

// 色值一律取自 utils/color.js（与 styles/token.wxss 的令牌同步），组件内不写裸色
// 国内习惯：涨红跌绿
const COLOR_UP = TREND_COLORS.up;
// 跌绿取令牌里的深绿：早前用的浅绿在白卡上仅 3.3:1，加深后 5.0:1
const COLOR_DOWN = TREND_COLORS.down;
// 均线三色是数据系列标识色（RESTYLE-BLOCKED 有裁决），不随 UI 色板走
const MA_COLORS = CHART_SERIES;
// 网格跟随 --border
const GRID_COLOR = PALETTE.border;

function num(v) {
  const n = parseFloat(v);
  return isNaN(n) ? null : n;
}

// MA 折线按 null 断开成若干段
function maSegments(arr, count, xOf, yOf) {
  const segs = [];
  let cur = [];
  for (let i = 0; i < count; i++) {
    const v = num(arr && arr[i]);
    if (v === null) {
      if (cur.length > 1) segs.push(cur);
      cur = [];
    } else {
      cur.push(xOf(i).toFixed(1) + ',' + yOf(v).toFixed(1));
    }
  }
  if (cur.length > 1) segs.push(cur);
  return segs;
}

// 图区最多画多少根 K 线：小图上数据太密会糊成一片，统一只画最近 MAX_BARS 根，
// MA 同步按同一区间截取，保证均线长度与蜡烛数对齐（否则均线会画到区间外）
const MAX_BARS = 15;

function buildChart(klinesRaw, maRaw) {
  const all = klinesRaw || [];
  // 只保留最后 MAX_BARS 根
  const start = Math.max(0, all.length - MAX_BARS);
  const klines = all.slice(start);
  const ma = {};
  if (maRaw) {
    Object.keys(maRaw).forEach((key) => {
      ma[key] = (maRaw[key] || []).slice(start);
    });
  }

  const n = klines.length;
  if (n < 2) return '';

  const highs = klines.map((k) => num(k.h)).filter((v) => v !== null);
  const lows = klines.map((k) => num(k.l)).filter((v) => v !== null);
  if (!highs.length || !lows.length) return '';

  const max = Math.max.apply(null, highs);
  const min = Math.min.apply(null, lows);
  const span = max - min || 1;
  const step = VB_W / n;
  const bodyW = Math.max(1.2, step * 0.62);
  const xOf = (i) => step * (i + 0.5);
  const yPrice = (v) => PRICE_TOP + (1 - (v - min) / span) * (PRICE_BOTTOM - PRICE_TOP);

  // 显式写 width/height：无固有尺寸的 SVG 作背景图时，各引擎兜底尺寸不一致
  let svg =
    "<svg xmlns='http://www.w3.org/2000/svg' width='" +
    VB_W +
    "' height='" +
    VB_H +
    "' viewBox='0 0 " +
    VB_W +
    ' ' +
    VB_H +
    "' preserveAspectRatio='none'>";

  // 价格区参考线（3 等分）
  for (let g = 1; g <= 2; g++) {
    const gy = (PRICE_TOP + (PRICE_BOTTOM - PRICE_TOP) * g) / 3;
    svg +=
      "<line x1='0' y1='" +
      gy.toFixed(1) +
      "' x2='" +
      VB_W +
      "' y2='" +
      gy.toFixed(1) +
      "' stroke='" +
      GRID_COLOR +
      "' stroke-width='0.6'/>";
  }

  // 蜡烛：实体 + 影线，涨红跌绿
  klines.forEach((k, i) => {
    const o = num(k.o);
    const h = num(k.h);
    const l = num(k.l);
    const c = num(k.c);
    if (o === null || h === null || l === null || c === null) return;
    const color = c >= o ? COLOR_UP : COLOR_DOWN;
    const x = xOf(i);
    svg +=
      "<line x1='" +
      x.toFixed(1) +
      "' y1='" +
      yPrice(h).toFixed(1) +
      "' x2='" +
      x.toFixed(1) +
      "' y2='" +
      yPrice(l).toFixed(1) +
      "' stroke='" +
      color +
      "' stroke-width='0.8'/>";
    const top = yPrice(Math.max(o, c));
    const hgt = Math.max(0.8, yPrice(Math.min(o, c)) - top);
    svg +=
      "<rect x='" +
      (x - bodyW / 2).toFixed(1) +
      "' y='" +
      top.toFixed(1) +
      "' width='" +
      bodyW.toFixed(1) +
      "' height='" +
      hgt.toFixed(1) +
      "' fill='" +
      color +
      "'/>";
  });

  Object.keys(MA_COLORS).forEach((key) => {
    const segs = maSegments(ma && ma[key], n, xOf, yPrice);
    segs.forEach((pts) => {
      svg +=
        "<polyline fill='none' stroke='" +
        MA_COLORS[key] +
        "' stroke-width='1' stroke-linejoin='round' points='" +
        pts.join(' ') +
        "'/>";
    });
  });

  // 成交量柱：颜色跟随当日涨跌，压低不抢主图
  const vols = klines.map((k) => num(k.v)).filter((v) => v !== null);
  if (vols.length) {
    const vMax = Math.max.apply(null, vols) || 1;
    klines.forEach((k, i) => {
      const v = num(k.v);
      const c = num(k.c);
      const o = num(k.o);
      if (v === null) return;
      const hgt = Math.max(0.6, (v / vMax) * (VOL_BOTTOM - VOL_TOP));
      const color = c !== null && o !== null && c < o ? COLOR_DOWN : COLOR_UP;
      svg +=
        "<rect x='" +
        (xOf(i) - bodyW / 2).toFixed(1) +
        "' y='" +
        (VOL_BOTTOM - hgt).toFixed(1) +
        "' width='" +
        bodyW.toFixed(1) +
        "' height='" +
        hgt.toFixed(1) +
        "' fill='" +
        color +
        "' fill-opacity='0.55'/>";
    });
  }

  svg += '</svg>';
  return svgToDataUri(svg);
}

Component({
  externalClasses: ['card-class'],
  properties: {
    name: { type: String, value: '' },
    code: { type: String, value: '' },
    // 自定义标题：多卡并列时（自选K线页的五个周期）用它写「日K线」这类周期名，
    // 留空则退回「<股票名> 走势 / K线走势」的对话内写法
    title: { type: String, value: '' },
    // 空态文案：给了就占住图区高度写这句话，没数据时卡片不会塌成一条
    placeholder: { type: String, value: '' },
    // 副标题（code 为空时展示，如「最近 15 个交易日 · 前复权」）
    subtitle: { type: String, value: '' },
    // OHLC 数组 [{ d, o, h, l, c, v }]
    klines: { type: Array, value: [] },
    // 均线 { ma5, ma10, ma20 }，与 klines 等长（可含 null）
    ma: { type: Object, value: null },
    // 区间涨跌幅（% 数值）/ 振幅（% 数值）/ 量比 / 支撑位 / 压力位。
    // 这几个必须是 type: null（任意类型）：声明成 Number 时 null 会被组件框架
    // 强转成 0，空态卡片上就会显示「0.00%」而不是「--」
    periodPct: { type: null, value: null },
    amplitude: { type: null, value: null },
    volumeRatio: { type: null, value: null },
    support: { type: null, value: null },
    resistance: { type: null, value: null },
    note: { type: String, value: '' }
  },
  data: {
    chartSrc: '',
    pctText: '--',
    pctDir: 'flat',
    amplitudeText: '--',
    volumeRatioText: '--',
    ma5Text: '--',
    supportText: '--',
    resistanceText: '--'
  },
  lifetimes: {
    // 只在 ready 算一次：attached 时 wx:if 首帧创建可能还没拿到父组件传值，
    // 与 ready 各算一遍会让 SVG 构建（字符串拼接 + encodeURIComponent）白做一轮
    ready() {
      // 显式赋值判断，避免父组件没传值时白跑一遍 SVG 构建
      if (this.data.klines && this.data.klines.length) this.refreshChart();
      this.refreshStats();
    }
  },
  observers: {
    'klines, ma'() {
      this.refreshChart();
    },
    'periodPct, amplitude, volumeRatio, ma, support, resistance'() {
      this.refreshStats();
    }
  },
  methods: {
    refreshChart() {
      this.setData({ chartSrc: buildChart(this.data.klines, this.data.ma) });
    },
    refreshStats() {
      const d = this.data;
      const ma5 = d.ma && d.ma.ma5;
      // MA5 取最后一根的有效值
      let ma5Last = null;
      if (ma5) {
        for (let i = ma5.length - 1; i >= 0; i--) {
          const v = num(ma5[i]);
          if (v !== null) {
            ma5Last = v;
            break;
          }
        }
      }
      this.setData({
        // 空态不显示「--%」：没有数据时连百分号一起去掉
        pctText: toNum(d.periodPct) === null ? '--' : fmtPct(d.periodPct),
        pctDir: direction(d.periodPct),
        // 振幅没有方向，不带正负号
        amplitudeText: fmtPctPlain(d.amplitude),
        volumeRatioText: fmtNum(d.volumeRatio),
        ma5Text: ma5Last === null ? '--' : fmtNum(ma5Last),
        supportText: fmtNum(d.support),
        resistanceText: fmtNum(d.resistance)
      });
    }
  }
});
