// 走势与 K 线卡：回答「最近走势怎么样」「站上 20 日线了吗」
// 字段口径见设计文档第 2 节②。
// 图表用 SVG 手绘（蜡烛 + MA5/10/20 + 成交量柱），整段编码成 data-URI 背景图。
// 选型说明：执行指南第 2 节把「完整 K 线」定为 uCharts（分包，约 170KB），
// 但对话流内的 K 线概览不需要十字光标 / 缩放手势，且引入 uCharts 需要
// 新建分包 + 构建 npm，与当前「主包 ≤2MB、零新增依赖」的改造节奏冲突；
// 这里按指南对「迷你走势线」的同一思路（SVG polyline 手写）扩展到 K 线，
// 数据字段与版式与设计稿一致，后续若要交互式图表可平滑替换图表区。
const { fmtNum, fmtPct, direction } = require('../../../utils/format');
const { svgToDataUri } = require('../../../utils/svg-uri');

// viewBox 与分区（对齐视觉稿：K 线区 ≥180rpx，下方成交量）
const VB_W = 340;
const VB_H = 200;
const PRICE_TOP = 10;
const PRICE_BOTTOM = 138;
const VOL_TOP = 150;
const VOL_BOTTOM = 192;

// 国内习惯：涨红跌绿；均线三色（蓝 / 琥珀 / 紫）——
// 浅色主题下金色与深灰在白底上对比度不足，换成蓝/琥珀/紫这一组常规均线配色
const COLOR_UP = '#dc2626';
const COLOR_DOWN = '#16a34a';
const MA_COLORS = { ma5: '#2559EB', ma10: '#F5A623', ma20: '#8B5CF6' };
const GRID_COLOR = '#e4e4e7';

// 默认周期枚举（设计文档②）
const DEFAULT_PERIODS = [
  { key: 'fenshi', label: '分时' },
  { key: 'day5', label: '5日' },
  { key: 'day', label: '日K' },
  { key: 'week', label: '周K' },
  { key: 'month', label: '月K' }
];

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

  // 均线：MA5 金 / MA10 蓝 / MA20 灰
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
    // 副标题（code 为空时展示，如「最近 30 个交易日 · 前复权」）
    subtitle: { type: String, value: '' },
    // [{ key, label }]，为空用默认周期
    periods: { type: Array, value: [] },
    // 当前周期 key（受控）
    activePeriod: { type: String, value: 'day' },
    // OHLC 数组 [{ d, o, h, l, c, v }]
    klines: { type: Array, value: [] },
    // 均线 { ma5, ma10, ma20 }，与 klines 等长（可含 null）
    ma: { type: Object, value: null },
    // 区间涨跌幅（% 数值）/ 振幅（% 数值）/ 量比
    periodPct: { type: Number, value: null },
    amplitude: { type: Number, value: null },
    volumeRatio: { type: Number, value: null },
    // 支撑位 / 压力位
    support: { type: Number, value: null },
    resistance: { type: Number, value: null },
    note: { type: String, value: '' }
  },
  data: {
    periodList: DEFAULT_PERIODS,
    periodIndex: 2,
    activeLabel: '日K',
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
    // observer 不会在 attached 时对初始值触发：首帧三个口径各手动算一次
    attached() {
      this.refreshPeriods();
      this.refreshChart();
      this.refreshStats();
    },

    // ready 再兜一次：wx:if 首帧创建时 properties 可能尚未就绪，
    // attached 读到的是初始值（observer 对首值也不触发），ready 时必然已就绪
    ready() {
      this.refreshPeriods();
      this.refreshChart();
      this.refreshStats();
    }
  },
  observers: {
    'periods, activePeriod'() {
      this.refreshPeriods();
    },
    'klines, ma'() {
      this.refreshChart();
    },
    'periodPct, amplitude, volumeRatio, ma, support, resistance'() {
      this.refreshStats();
    }
  },
  methods: {
    refreshPeriods() {
      const list = this.data.periods && this.data.periods.length ? this.data.periods : DEFAULT_PERIODS;
      const idx = list.findIndex((p) => p.key === this.data.activePeriod);
      const index = idx >= 0 ? idx : 0;
      this.setData({
        periodList: list,
        periodIndex: index,
        activeLabel: (list[index] && list[index].label) || ''
      });
    },
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
        pctText: fmtPct(d.periodPct),
        pctDir: direction(d.periodPct),
        amplitudeText: fmtPct(d.amplitude),
        volumeRatioText: fmtNum(d.volumeRatio),
        ma5Text: ma5Last === null ? '--' : fmtNum(ma5Last),
        supportText: fmtNum(d.support),
        resistanceText: fmtNum(d.resistance)
      });
    },
    // 周期切换：只上报，不自己拉数据（组件内不做业务计算）
    onChipTap(e) {
      const index = Number(e.currentTarget.dataset.index);
      const item = this.data.periodList[index];
      if (!item || index === this.data.periodIndex) return;
      this.triggerEvent('change', { period: item.key, index: index });
    }
  }
});
