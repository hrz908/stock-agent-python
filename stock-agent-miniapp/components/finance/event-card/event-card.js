// 公司事件卡：回答「三季报什么时候出」「有解禁吗」「为什么停牌」
// 字段口径见设计文档第 2 节⑧。时间轴样式，圆点按事件重要度变色
// （金色=需关注，蓝色=常规）。合规：事件卡需提供原文链接（设计文档四-4）。
Component({
  externalClasses: ['card-class'],
  properties: {
    // [{ type, date, title, desc, important, sourceUrl }]
    // type: 财报/分红/解禁/增减持/停复牌/诉讼/回购
    events: { type: Array, value: [] },
    // 头部副标题，如「近7天 · 3条」
    range: { type: String, value: '' },
    note: { type: String, value: '' }
  },
  data: {
    rows: [],
    hasImportant: false
  },
  lifetimes: {
    // observer 不会在 attached 时对初始值触发（微信官方行为），
    // 首帧必须手动映射一次，否则「有数据但列表空白」
    attached() {
      this.mapEvents();
    },

    // ready 再兜一次：wx:if 首帧创建时 properties 可能尚未就绪，
    // attached 读到的是初始值（observer 对首值也不触发），ready 时必然已就绪
    ready() {
      this.mapEvents();
    }
  },
  observers: {
    events() {
      this.mapEvents();
    }
  },
  methods: {
    mapEvents() {
      // 同一标题只渲染一条：资讯源常把同一条消息挂在不同频道下重复返回，
      // 后端已按标题去重，这里再兜一层（wx:key="title" 撞 key 会直接渲染重影）
      const seen = Object.create(null);
      const rows = (this.data.events || [])
        .filter((e) => e && e.title)
        .filter((e) => {
          const key = String(e.title).replace(/\s+/g, '');
          if (seen[key]) return false;
          seen[key] = true;
          return true;
        })
        .map((e) => ({
          type: e.type || '',
          date: e.date || '',
          title: e.title,
          desc: e.desc || '',
          important: !!e.important,
          sourceUrl: e.sourceUrl || ''
        }));
      this.setData({
        rows: rows,
        hasImportant: rows.some((r) => r.important)
      });
    },
    // 原文链接：复制到剪贴板（小程序不直接打开外链）
    onSourceTap(e) {
      const url = e.currentTarget.dataset.url;
      if (!url) return;
      wx.setClipboardData({
        data: url,
        success: () => this.triggerEvent('copylink', { url: url })
      });
    }
  }
});
