// SVG 源码 -> 可安全写进 CSS url() 的 data-URI
//
// 坑（2026-10-22 实测）：encodeURIComponent 有一组保留字符不转义——
// A-Za-z0-9 与 - _ . ! ~ * ' ( ) 。
// 本项目所有内联 SVG 都用单引号写属性，生成的 data-URI 因此带着裸 ' ；
// 迷你走势线的渐变引用还会带裸 ( )（url(#sparkFill)）。
// 而 CSS 里「未加引号的 url()」不允许出现引号、括号、空白，
// 一旦出现，整条 background-image 声明会被解析成非法值丢弃。
// 现象：图标 / 迷你走势线 / K 线图 / 空态插画全部不显示，且控制台无任何报错。
//
// 两种改法：给 url() 加引号，或把这几个字符转义。
// 前者走不通——WXML 的 style="..." 属性本身用双引号分隔，
// 里面再套 url("...") 会提前闭合属性；用 url('...') 又和 URI 内的 ' 冲突。
// 所以统一走转义：' -> %27，( -> %28，) -> %29。
// 解码后仍是原字符，SVG 本身不受影响。
function svgToDataUri(svg) {
  return (
    'data:image/svg+xml,' +
    encodeURIComponent(svg)
      .replace(/'/g, '%27')
      .replace(/\(/g, '%28')
      .replace(/\)/g, '%29')
  );
}

export { svgToDataUri };
