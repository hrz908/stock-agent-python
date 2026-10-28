// 自研轻提示（绕开 wx.showToast 的固定样式）
// 用法：<base-toast id="base-toast" /> + this.selectComponent('#base-toast').show('文案')
// 只承载纯文字反馈；关键反馈走 base-popup 对话框，不用 toast 兜（执行指南禁止项）
Component({
  externalClasses: ['toast-class'],
  properties: {
    // 单次展示时长 ms
    duration: { type: Number, value: 2000 }
  },
  data: {
    visible: false,
    text: ''
  },
  methods: {
    show(text, duration) {
      this.setData({ visible: true, text: text || '' });
      if (this._timer) clearTimeout(this._timer);
      this._timer = setTimeout(() => {
        this._timer = 0;
        this.setData({ visible: false });
      }, duration || this.properties.duration);
    },
    hide() {
      if (this._timer) clearTimeout(this._timer);
      this._timer = 0;
      this.setData({ visible: false });
    }
  },
  lifetimes: {
    detached() {
      if (this._timer) clearTimeout(this._timer);
    }
  }
});
