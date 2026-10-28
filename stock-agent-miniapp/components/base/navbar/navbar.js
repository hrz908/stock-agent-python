// 自研导航栏：状态栏高度 + 胶囊安全区计算
// 硬约束（执行指南 1.3）：标题不得被右上角微信胶囊遮挡。
// 做法：标题层铺满整行、水平居中于屏幕，但 max-width 限制在
// 「左侧按钮右界 ~ 胶囊左界」之间，且 pointer-events: none 保证不挡点击。
// 组件自身在文档流内占位（非 fixed），页面无需再测 placeholder 高度：
// 高度 = 状态栏高度 + 44px 内容区，完全确定。
Component({
  options: {
    // 命名插槽必须显式开启 multipleSlots，否则 <slot name="left"> 收不到
    // 页面传入的菜单按钮（实测：不开时聊天页的菜单按钮整块不渲染，
    // 侧边抽屉无从打开）
    multipleSlots: true
  },
  externalClasses: ['navbar-class'],
  properties: {
    title: { type: String, value: '' },
    // 显示左侧返回箭头（子页面用）；主页放菜单按钮则走 left 插槽
    leftArrow: { type: Boolean, value: false },
    // 左侧插槽内容的可读描述（无障碍）
    leftAriaLabel: { type: String, value: '返回' }
  },
  data: {
    statusBarHeight: 20,
    // 标题可用宽度（px）：窗口宽 - 左按钮区 - 胶囊安全区
    titleMaxWidth: 240,
    barHeight: 44
  },
  lifetimes: {
    attached() {
      let statusBarHeight = 20;
      let windowWidth = 375;
      let capsuleLeft = 281;
      try {
        const win = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync();
        statusBarHeight = win.statusBarHeight || 20;
        windowWidth = win.windowWidth || 375;
        const rect = wx.getMenuButtonBoundingClientRect ? wx.getMenuButtonBoundingClientRect() : null;
        if (rect && rect.left) capsuleLeft = rect.left;
      } catch (e) {
        // 拿不到系统信息时用常见机型的兜底值
      }
      // 左按钮区 88rpx(=44px) + 左右各 12px 内边距；胶囊左界再让 12px
      const leftZone = 44 + 24;
      const titleMaxWidth = Math.max(120, windowWidth - leftZone - (windowWidth - capsuleLeft) - 24);
      this.setData({ statusBarHeight: statusBarHeight, titleMaxWidth: titleMaxWidth });
    }
  },
  methods: {
    onLeftTap() {
      if (this.properties.leftArrow) {
        this.triggerEvent('back');
      }
    }
  }
});
