// 空状态：字体图标 + 文案，零本地图片/SVG 依赖
Component({
  externalClasses: ['empty-class'],
  properties: {
    text: { type: String, value: '暂无数据' },
    // 图标下的一行补充说明（可选）
    sub: { type: String, value: '' },
    // 空态图标：components/base/icon 的图标名；默认用文档图标，列表类页面传更贴题的类型
    icon: { type: String, value: 'FileOutline' }
  }
});
