// 卡片画廊（原 K 线详情页）：八张金融卡的样式测试页。
//
// 用途与约定（详见 DESIGN.md「卡片画廊」节）：
// - 不接后端、不读接口，全部用手写假数据渲染八张卡，用于看卡片实例样式；
// - 本页与 chat 主页面**共用同一批组件**（components/finance/*）和同一个
//   卡片外层类 `.msg-card`（已上收到 styles/token.wxss）；
// - 因此调卡片样式只需改组件 wxss / token，两边自动同步，不存在两份样式；
// - 字段口径与 chat.js 的卡片工厂保持一致，方便对照真实链路的数据形状。
//
// 【只允许开发版进入】页面用的是假行情与假研报：体验版/正式版一旦可达，
// 就是「向用户展示虚构金融信息」，审核与合规都过不去。这里双保险——
// onLoad 强制回首页（app 里仍注册着本页，方便开发版调试），
// 数据本身也去掉了真实机构名，并挂了一条醒目水印。
//
// 入口：watchlist 股票行点击、chat 页行情卡点击（仅 isDev）。
import { isDev } from '../../config';

Page({
  onLoad() {
    if (!isDev) wx.reLaunch({ url: '/pages/chat/chat' });
  },

  data: {
    // 与 chat 页消息结构一致：{ role, status, card, content, <cardKey> }
    cards: [
      {
        role: 'assistant',
        status: 'complete',
        card: 'quote',
        content: [],
        quote: {
          name: '贵州茅台',
          code: '600519.SH',
          status: '交易中',
          price: 1523.66,
          change: 23.44,
          changePct: 1.56,
          open: 1502.1,
          high: 1528.88,
          low: 1498.2,
          amount: 4612000000,
          turnoverRate: 0.37,
          totalCap: 1914800000000,
          // 分时迷你走势（测试用假数据）
          points: [1502, 1505, 1498, 1508, 1512, 1510, 1515, 1506, 1518, 1520, 1514, 1523.66],
          // 悬浮标签（测试用假数据）
          tags: [
            { text: '北向重仓', theme: 'gold' },
            { text: '融资标的', theme: '' }
          ],
          note: '行情时间 2026-10-22 15:00'
        }
      },
      {
        role: 'assistant',
        status: 'complete',
        card: 'kline',
        content: [],
        // 与组件 MAX_BARS 对齐：只保留最近 15 个交易日（图区只画 15 根）
        kline: [
          { d: '09-30', o: 1420, h: 1438, l: 1415, c: 1435, v: 30100 },
          { d: '10-09', o: 1435, h: 1455, l: 1430, c: 1450, v: 33500 },
          { d: '10-10', o: 1450, h: 1462, l: 1440, c: 1458, v: 28900 },
          { d: '10-11', o: 1458, h: 1470, l: 1452, c: 1466, v: 31200 },
          { d: '10-12', o: 1466, h: 1468, l: 1448, c: 1452, v: 35800 },
          { d: '10-13', o: 1452, h: 1462, l: 1445, c: 1460, v: 27600 },
          { d: '10-14', o: 1460, h: 1478, l: 1455, c: 1475, v: 33100 },
          { d: '10-15', o: 1475, h: 1490, l: 1470, c: 1486, v: 36400 },
          { d: '10-16', o: 1486, h: 1498, l: 1480, c: 1494, v: 29800 },
          { d: '10-17', o: 1494, h: 1502, l: 1482, c: 1488, v: 32100 },
          { d: '10-18', o: 1488, h: 1496, l: 1478, c: 1482, v: 28700 },
          { d: '10-19', o: 1482, h: 1500, l: 1475, c: 1496, v: 34500 },
          { d: '10-20', o: 1496, h: 1512, l: 1490, c: 1508, v: 37200 },
          { d: '10-21', o: 1508, h: 1515, l: 1500, c: 1512, v: 30400 },
          { d: '10-22', o: 1512, h: 1530, l: 1505, c: 1523.66, v: 41300 }
        ],
        klineMa: {
          ma5: [1427, 1435, 1443, 1450, 1455, 1453, 1456, 1462, 1469, 1475, 1478, 1481, 1488, 1495, 1510.33],
          ma10: [1426, 1431, 1438, 1444, 1449, 1450, 1452, 1457, 1463, 1468, 1471, 1474, 1480, 1487, 1494.6],
          ma20: [1422, 1426, 1432, 1437, 1441, 1442, 1444, 1448, 1453, 1457, 1460, 1462, 1466, 1470, 1460.25]
        },
        klineSub: '最近 15 个交易日 · 前复权',
        // 区间统计按这 15 根的口径重算：首 1435 → 末 1523.66；高 1530 / 低 1415
        klinePct: 6.18,
        klineAmplitude: 8.01,
        klineVolumeRatio: 1.36,
        // 口径类说明按需求移除
        klineNote: '',
        // 支撑 / 压力位（测试用假数据）
        klineSupport: 1480,
        klineResistance: 1550
      },
      {
        role: 'assistant',
        status: 'complete',
        card: 'valuation',
        content: [],
        valuation: {
          peStatic: 28.6,
          peTtm: 24.18,
          pb: 8.42,
          ps: 11.6,
          peg: 1.47,
          dividendYield: 3.28,
          totalCap: 1914800000000,
          note: '600519.SH · 数据日期 2026-10-22'
        }
      },
      {
        role: 'assistant',
        status: 'complete',
        card: 'fundflow',
        content: [],
        fundflow: {
          mainNet: 128600000,
          mainNetPct: 4.2,
          tiers: [
            { label: '超大单', value: 86200000 },
            { label: '大单', value: 52400000 },
            { label: '中单', value: -18600000 },
            { label: '小单', value: -28400000 }
          ],
          // 北向持股（测试用假数据）
          northPct: 7.86,
          northChangePct: 0.42,
          // 连续净流入天数（测试用假数据）
          streakDays: 3,
          // 近 5 日主力净流入（测试用假数据）
          days5: [-32000000, 56000000, 92000000, 41000000, 128600000],
          // 龙虎榜（测试用假数据）
          lhb: [
            { seat: '沪股通专用', buy: 382000000, sell: 156000000 },
            { seat: '机构专用', buy: 214000000, sell: 98000000 },
            { seat: '某券商营业部', buy: 128000000, sell: 142000000 }
          ],
          note: '600519.SH · 数据日期 2026-10-22'
        }
      },
      {
        role: 'assistant',
        status: 'complete',
        card: 'report',
        content: [],
        report: {
          periodLabel: '2026 三季报',
          revenue: { value: 130246000000, yoy: 16.85 },
          netProfit: { value: 64680000000, yoy: 15.22 },
          cashFlow: { value: 42180000000, yoy: 9.36 },
          grossMargin: 91.78,
          netMargin: 52.34,
          roe: 24.68,
          eps: 51.42,
          // 一致预期对比（测试用假数据：净利超预期）
          beat: '超预期',
          note: '600519.SH · 数据日期 2026-09-30'
        }
      },
      {
        role: 'assistant',
        status: 'complete',
        card: 'research',
        content: [],
        research: {
          // 机构名一律用占位：假数据挂着真实机构名会构成「虚构研报」
          org: '示例券商',
          rating: '买入',
          targetPrice: 1780,
          publishDate: '2026-10-18',
          reportType: '深度报告',
          summary: '公司三季度收入与利润增速符合预期，直销渠道占比提升带动吨价，预收账款维持高位，全年业绩确定性强。',
          forecastDir: '上调',
          upside: 16.82,
          sourceName: '示例券商研究部',
          sourceUrl: 'https://example.com/research/600519-2026q3'
        }
      },
      {
        role: 'assistant',
        status: 'complete',
        card: 'event',
        content: [],
        event: {
          events: [
            { type: '解禁', date: '2026-11-06', title: '限售股解禁 4.15 亿股', desc: '占总股本 3.31%，解禁类型为定向增发机构配售股份', important: true, sourceUrl: 'https://example.com/notice/unlock' },
            { type: '增减持', date: '2026-10-09', title: '控股股东增持计划进展', desc: '集团累计增持 68.3 万股，占公司总股本 0.05%', important: false, sourceUrl: 'https://example.com/notice/increase' },
            { type: '分红', date: '2026-10-15', title: '2026 年中期分红派息实施', desc: '每股派发现金红利 23.882 元，股权登记日 10 月 24 日', important: false, sourceUrl: 'https://example.com/notice/dividend' }
          ],
          range: '近 30 天 · 3 条',
          note: '事件来源：交易所公告'
        }
      },
      {
        role: 'assistant',
        status: 'complete',
        card: 'compare',
        content: [],
        // 字段名必须与 compare-card 的 METRICS 对齐（changePct / pb / totalMv）：
        // 早前用的是 periodPct / roe / dividendYield，组件读不到就整张表渲染成 '--'
        compare: {
          items: [
            { name: '贵州茅台', price: 1523.66, changePct: 1.56, pe: 24.18, pb: 8.42, totalMv: 1914800000000 },
            { name: '五粮液', price: 148.32, changePct: 0.84, pe: 16.42, pb: 4.15, totalMv: 575600000000 },
            { name: '泸州老窖', price: 132.86, changePct: -1.24, pe: 15.28, pb: 6.73, totalMv: 195500000000 }
          ],
          note: '数据日期 2026-10-22'
        }
      }
    ]
  },

  // 旧入口会带 code / name 参数，本页是样式测试页，不按代码取数，参数忽略即可

  onBack() {
    // 返回上一页（chat 行情卡入口 或 watchlist 行点击入口）。
    // 兜底：navigateBack 失败时（无上一页）用 reLaunch 回到主页，
    // 避免从分包子页卡死。chat 不是 tabBar 页，switchTab 会失败，故用 reLaunch。
    wx.navigateBack({ fail: () => wx.reLaunch({ url: '/pages/chat/chat' }) });
  }
});
