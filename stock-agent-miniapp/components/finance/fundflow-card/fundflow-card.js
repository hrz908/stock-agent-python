// 资金流向卡：回答「主力资金流入还是流出」「北向加仓了吗」
// 字段口径见设计文档第 2 节⑥。主数字放大（22px 级），四档拆分逐行展开，
// 正负值自动着色（涨跌方向由数值正负推导，不手写 class）。
const { fmtAmount, fmtSign, fmtPct, fmtPctPlain, direction } = require('../../../utils/format');

Component({
  externalClasses: ['card-class'],
  properties: {
    // 主力净流入额（元）
    mainNet: { type: Number, value: null },
    // 占成交额比（% 数值）
    mainNetPct: { type: Number, value: null },
    // 四档拆分 [{ label, value }]（元）
    tiers: { type: Array, value: [] },
    // 北向持股比例（% 数值）及变动（pct）
    northPct: { type: Number, value: null },
    northChangePct: { type: Number, value: null },
    // 连续流入 / 流出天数：正数=流入，负数=流出
    streakDays: { type: Number, value: null },
    // 近 5 日流向趋势（数值数组，画迷你线）
    days5: { type: Array, value: [] },
    // 龙虎榜 [{ seat, buy, sell }]（元）
    lhb: { type: Array, value: [] },
    note: { type: String, value: '' }
  },
  data: {
    mainNetText: '--',
    mainNetDir: 'flat',
    mainNetPctText: '--',
    tierRows: [],
    northText: '',
    streakText: '',
    days5Dir: 'flat',
    lhbRows: []
  },
  lifetimes: {
    // observer 不会在 attached 时对初始值触发：首帧手动算一次
    attached() {
      this.refresh();
    },

    // ready 再兜一次：wx:if 首帧创建时 properties 可能尚未就绪，
    // attached 读到的是初始值（observer 对首值也不触发），ready 时必然已就绪
    ready() {
      this.refresh();
    }
  },
  observers: {
    'mainNet, mainNetPct, tiers, northPct, northChangePct, streakDays, days5, lhb'() {
      this.refresh();
    }
  },
  methods: {
    refresh() {
      const d = this.data;
      const streak = d.streakDays;
      const streakAbs = streak == null || isNaN(streak) ? 0 : Math.abs(Math.round(streak));
      this.setData({
        mainNetText: fmtAmount(d.mainNet),
        mainNetDir: direction(d.mainNet),
        mainNetPctText: d.mainNetPct == null ? '' : '占成交额 ' + fmtPct(d.mainNetPct),
        tierRows: (d.tiers || []).map((t) => ({
          label: t.label,
          value: fmtAmount(t.value),
          dir: direction(t.value)
        })),
        // 持股比例是水平值（不带号），括号里的变动才有方向
        northText:
          d.northPct == null
            ? ''
            : fmtPctPlain(d.northPct) + (d.northChangePct == null ? '' : '（' + fmtSign(d.northChangePct) + 'pct）'),
        streakText: streakAbs ? '连续' + streakAbs + '日净' + (streak > 0 ? '流入' : '流出') : '',
        days5Dir: direction(d.days5 && d.days5.length ? d.days5[d.days5.length - 1] : null),
        // 龙虎榜三个量：席位 / 买入 / 卖出。正负号在 JS 里拼（wxml 不再写死 +-)：
        // 买侧行的 sell、卖侧行的 buy 没有数据，传 null 渲染 '--' 而不是 "+--"
        lhbRows: (d.lhb || []).map((r) => ({
          seat: r.seat,
          buy: r.buy == null ? '--' : '+' + fmtAmount(r.buy),
          sell: r.sell == null ? '--' : '-' + fmtAmount(r.sell)
        }))
      });
    }
  }
});
