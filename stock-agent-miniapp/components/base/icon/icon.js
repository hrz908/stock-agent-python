// 字体图标：复刻 antd-mini Icon 的用法（type + style），但不再依赖 antd-mini 整包。
// 字体仍是 antd-mini 3.4.3 的官方 TTF（MIT），已 base64 内联在 styles/antd-icon.wxss；
// 组件只负责「type -> 码点」这一层，码点表见 icon.wxss。
// 换图标集时：新码点加进 icon.wxss，并同步 styles/antd-icon.wxss 里的字体文件。
Component({
  properties: {
    // 图标名（与旧 ant-icon 的 type 完全一致，页面无需改调用方式）
    type: { type: String, value: '' },
    // 行内样式透传：尺寸与颜色仍由调用方写 font-size / color
    style: { type: String, value: '' },
    // 预留的类名透传（与旧组件的 className 参数对齐）
    className: { type: String, value: '' }
  }
});
