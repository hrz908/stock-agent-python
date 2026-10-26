// 同业对比卡：回答「茅台和五粮液哪个好」「白酒板块谁估值低」
// 只对比两家同行（本股 + 2 家，共 3 列），标题形如「A vs B vs C」。
// 数据约定（agent 的 get_peer_compare）：全部维度取自当日行情快照，
// 不含任何历史区间或财务接口——因此对比维度为 价/涨跌幅/PE/PB/总市值。
// 着色规则：只有涨跌幅按正负着色（加号红 / 减号绿）。
// PE / PB 曾按「更低者优」标绿，2026-10-26 用户取消：国内习惯绿=跌，
// 给低估值标绿会被读成「在跌」，反而误导，估值只在数值本身比较。
// 卡内只陈述客观事实，不写买卖推荐（合规红线）。
import { fmtAmount, fmtNum, fmtPct, fmtPrice, direction } from '../../../utils/format';

// 最多三个标的：本股 + 两家同行（需求：只对比两家同行，标题 A vs B vs C）
const MAX_ITEMS = 3;

// 对比维度配置：better = 'high' 越高越优 / 'low' 越低越优 / 'none' 不比较高低；
// sign = true 的指标（涨跌幅）不比高低，按正负着色：加号红 / 减号绿
// PE / PB 用 'none'：不标赢家、不上绿色（2026-10-26 用户指定取消）
const METRICS = [
  { key: 'price', label: '最新价', better: 'none', fmt: fmtPrice },
  { key: 'changePct', label: '涨跌幅', better: 'none', sign: true, fmt: (v) => fmtPct(v) },
  { key: 'pe', label: 'PE(TTM)', better: 'none', fmt: (v) => fmtNum(v) },
  { key: 'pb', label: 'PB', better: 'none', fmt: (v) => fmtNum(v) },
  { key: 'totalMv', label: '总市值', better: 'none', fmt: (v) => fmtAmount(v) }
];

// 赢家 tone：越高越优的指标赢家标红（up），越低越优的标绿（down）
const WINNER_TONE = { high: 'up', low: 'down' };

Component({
  externalClasses: ['card-class'],
  properties: {
    // 对比标的 [{ name, price, changePct, pe, pb, totalMv }]（当日快照，元口径），
    // 只取前 3 个（本股 + 两家同行）
    items: { type: Array, value: [] },
    note: { type: String, value: '' }
  },
  data: {
    title: '',
    subtitle: '',
    // 表头列：与 rows 同一批标的（已截断），避免表头列数多于数据列
    cols: [],
    rows: []
  },
  observers: {
    items() {
      this.render();
    }
  },
  lifetimes: {
    attached() {
      this.render();
    },

    // ready 再兜一次：wx:if 首帧创建时 properties 可能尚未就绪，
    // attached 读到的是初始值（observer 对首值也不触发），ready 时必然已就绪
    ready() {
      this.render();
    }
  },
  methods: {
    render() {
      // 只对比两家同行：本股 + 两家同行 = 3 列
      const items = (this.data.items || []).filter((it) => it && it.name).slice(0, MAX_ITEMS);
      if (items.length < 2) {
        this.setData({ title: '', subtitle: '', cols: [], rows: [] });
        return;
      }

      const rows = METRICS.map((m) => {
        const values = items.map((it) => parseFloat(it[m.key]));
        let winner = -1;
        if (m.better !== 'none' && values.some((v) => !isNaN(v))) {
          const valid = values.map((v, i) => ({ v: isNaN(v) ? null : v, i: i })).filter((x) => x.v !== null);
          if (valid.length === items.length) {
            // 只有一个有效值时没有「对比」意义，不标赢家
            const best = valid.reduce((acc, cur) =>
              m.better === 'high' ? (cur.v > acc.v ? cur : acc) : cur.v < acc.v ? cur : acc
            );
            winner = best.i;
          }
        }
        return {
          label: m.label,
          cells: items.map((it, i) => ({
            text: m.fmt(it[m.key]),
            // 涨跌幅这类带方向的指标按正负着色：加号红 / 减号绿；其余指标标赢家
            tone: m.sign ? direction(it[m.key]) : i === winner ? WINNER_TONE[m.better] : ''
          }))
        };
      });

      // 标题：A vs B vs C（最多三个标的）
      const title = items.map((it) => it.name).join(' vs ');

      this.setData({
        title: title,
        subtitle: '同业对比',
        cols: items,
        rows: rows
      });
    }
  }
});
