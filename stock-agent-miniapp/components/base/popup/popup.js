// 自研弹层：center 对话框 / left 抽屉 / bottom 底部面板（替代 t-popup + t-dialog）
// 设计取舍：
// - center 模式只做淡入淡出，不加 transform：原生 input 在父容器 transform 期间
//   会错位（placeholder 下坠），TDesign 时代已踩过，见 watchlist 弹窗注释
// - 抽屉 / 底部面板内没有原生输入框，才用位移动画
// - 开合由页面 visible 驱动；确认/取消只发事件不自动关（校验失败要能留在原地）
Component({
  options: {
    // 默认插槽（抽屉/底部面板）+ content 插槽（对话框主体）
    multipleSlots: true
  },
  externalClasses: ['popup-class'],
  properties: {
    visible: { type: Boolean, value: false },
    // center：居中对话框；left：左侧抽屉；bottom：底部面板
    mode: { type: String, value: 'center' },
    title: { type: String, value: '' },
    // 对话框标题下的补充说明（可选）
    desc: { type: String, value: '' },
    confirmText: { type: String, value: '' },
    cancelText: { type: String, value: '' },
    // 确认按钮走危险色（删除 / 退出登录类操作）
    confirmDanger: { type: Boolean, value: false },
    // 过渡时长 ms（与 popup.wxss 的 240ms 动画对齐；改这里也要改 wxss）
    // 点遮罩只发 mask 事件，关不关由页面决定（校验失败要能留在原地）
    duration: { type: Number, value: 240 }
  },
  data: {
    innerVisible: false,
    leaving: false,
    hasFooter: false
  },
  observers: {
    visible(v) {
      this._toggle(!!v);
    },
    'confirmText, cancelText'(confirmText, cancelText) {
      this.setData({ hasFooter: !!(confirmText || cancelText) });
    }
  },
  lifetimes: {
    detached() {
      if (this._leaveTimer) clearTimeout(this._leaveTimer);
    }
  },
  methods: {
    _toggle(v) {
      if (v) {
        if (this._leaveTimer) {
          clearTimeout(this._leaveTimer);
          this._leaveTimer = 0;
        }
        this.setData({ innerVisible: true, leaving: false });
        return;
      }
      if (!this.data.innerVisible || this.data.leaving) return;
      this.setData({ leaving: true });
      this._leaveTimer = setTimeout(() => {
        this._leaveTimer = 0;
        this.setData({ innerVisible: false, leaving: false });
        this.triggerEvent('leaved');
      }, this.properties.duration);
    },
    onMask() {
      this.triggerEvent('mask');
    },
    // 只用于 catchtouchmove 拦截滚动穿透，不做任何事
    onMaskMove() {},
    onConfirm() {
      this.triggerEvent('confirm');
    },
    onCancel() {
      this.triggerEvent('cancel');
    }
  }
});
