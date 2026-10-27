// markdown -> WXML 渲染结构（AI 回答的纯文本部分）
// 背景：后端把结构化数据（行情卡 / K 线表）走独立 WS 事件，token 流里只有散文，
// 因此这里只覆盖回答文本的实用子集：标题 / 段落 / 列表 / 引用 / 代码块 /
// 分割线 / 表格 / 行内样式（粗体、斜体、行内代码、链接、删除线）。
// 与 TDesign chat-markdown 同一思路：marked 的 Lexer 只负责切 token，
// 渲染结构自绘（WXML 侧按 tag 分发，见 chat.wxml 的 md-* 类名）。
import { Lexer } from 'marked';

// 与 TDesign chat-markdown 默认选项保持一致：breaks: true 让单换行也换行。
// 注意：Lexer 实例带状态（tokens 会累积到实例上，复用会让第二次解析
// 返回第一次的结果），因此每次解析都要新建，不能提成模块级单例
const LEXER_OPTIONS = { gfm: true, pedantic: false, breaks: true };

// marked 的强调规则按 ASCII 标点判定词边界，中文标点夹在闭合标记后面时会漏判：
// 「涨幅 **1.23%**，收盘」整句被当成纯文本，界面上就直接露出 ** 星号。
// 这里对「已经被 lex 成纯文本、但仍残留标记」的片段做一次兜底切分。
// 只处理 ** / ` / ~~ 三种：单 * 与 _ 在正文里可能真的是乘号或下划线，误判代价更大。
// 正则提到模块级复用（流式下每次解析要跑几百次），用前重置 lastIndex 保证可重入
const LEFTOVER_RE = /\*\*[^*\n]+\*\*|`[^`\n]+`|~~[^~\n]+~~/g;

function leftoverSegments(text, st) {
  const re = LEFTOVER_RE;
  re.lastIndex = 0;
  const out = [];
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      out.push({ t: 'text', text: text.slice(last, m.index), strong: st.strong, em: st.em, code: st.code, del: st.del });
    }
    const tok = m[0];
    if (tok.charAt(0) === '*' && tok.charAt(1) === '*') {
      out.push({ t: 'text', text: tok.slice(2, -2), strong: true, em: st.em, code: st.code, del: st.del });
    } else if (tok.charAt(0) === '`') {
      out.push({ t: 'text', text: tok.slice(1, -1), strong: st.strong, em: st.em, code: true, del: st.del });
    } else {
      out.push({ t: 'text', text: tok.slice(2, -2), strong: st.strong, em: st.em, code: st.code, del: true });
    }
    last = m.index + tok.length;
  }
  if (!out.length) return null;
  if (last < text.length) {
    out.push({ t: 'text', text: text.slice(last), strong: st.strong, em: st.em, code: st.code, del: st.del });
  }
  return out;
}

function pushText(out, text, st) {
  const segs = leftoverSegments(String(text == null ? '' : text), st);
  if (segs) out.push.apply(out, segs);
  else out.push({ t: 'text', text: text || '', strong: st.strong, em: st.em, code: st.code, del: st.del });
}

// 行内 token 摊平成「带样式标记的文本段」。
// 嵌套格式（em 套 strong 等）通过合并标记保留视觉效果，WXML 侧按标记组合加 class。
// 换行（br）合并进下一个文本段的开头，配合容器 white-space: pre-wrap 渲染
function flattenInline(tokens, style) {
  const base = style || {};
  const out = [];
  (tokens || []).forEach((tk) => {
    const st = {
      strong: !!base.strong,
      em: !!base.em,
      code: !!base.code,
      del: !!base.del
    };
    switch (tk.type) {
      case 'text':
        if (tk.tokens && tk.tokens.length) {
          out.push.apply(out, flattenInline(tk.tokens, st));
        } else {
          pushText(out, tk.text, st);
        }
        break;
      case 'strong':
        out.push.apply(out, flattenInline(tk.tokens || [{ type: 'text', text: tk.text }], merge(st, { strong: true })));
        break;
      case 'em':
        out.push.apply(out, flattenInline(tk.tokens || [{ type: 'text', text: tk.text }], merge(st, { em: true })));
        break;
      case 'del':
        out.push.apply(out, flattenInline(tk.tokens || [{ type: 'text', text: tk.text }], merge(st, { del: true })));
        break;
      case 'codespan':
        out.push({ t: 'text', text: tk.text || '', strong: st.strong, em: st.em, del: st.del, code: true });
        break;
      case 'link': {
        const inner = flattenInline(tk.tokens, st);
        inner.forEach((n) => {
          n.href = tk.href;
        });
        out.push.apply(out, inner);
        break;
      }
      case 'br':
        out.push({ t: 'br' });
        break;
      case 'escape':
        pushText(out, tk.text, st);
        break;
      case 'image':
        // 回答文本不渲染图片：保留 alt 文案，不引入 image 加载失败态
        pushText(out, tk.text || '[图片]', st);
        break;
      default:
        if (tk.text) pushText(out, tk.text, st);
        break;
    }
  });
  return out;
}

function merge(a, b) {
  return Object.assign({}, a, b);
}

// br 合并进下一段文本的开头（没有下一段就丢掉），容器 pre-wrap 负责换行
function mergeBreaks(inlines) {
  const out = [];
  (inlines || []).forEach((n) => {
    if (n.t === 'br') {
      if (out.length) {
        out[out.length - 1].text += '\n';
      }
      return;
    }
    out.push(n);
  });
  return out;
}

// 折叠连续换行：视图容器开了 pre-wrap，'\n\n' 会撑出一整行空白。
// marked 的列表项 text token 自带尾部 '\n'，再叠上分段拼接的那一个，就成了空行；
// 段间距统一交给 .md-p 等块的 margin，这里只保证「一次换行 = 一次换行」。
// 返回新数组：blocks 可能已经 patch 到视图层，不能原地改写其中的对象
function tidy(inlines) {
  const items = [];
  (inlines || []).forEach((n) => {
    if (n.t === 'br') return; // br 已由 mergeBreaks 并入前一段文本
    const text = String(n.text == null ? '' : n.text).replace(/\n{2,}/g, '\n');
    items.push(text === n.text ? n : Object.assign({}, n, { text: text }));
  });
  // 块首/块尾的换行与块间距重复，裁掉；裁空的段一并丢弃
  while (items.length && !items[0].text.replace(/^\n+/, '')) items.shift();
  while (items.length && !items[items.length - 1].text.replace(/\n+$/, '')) items.pop();
  if (items.length) {
    // 只在真的需要裁剪时才克隆：blocks 可能已 patch 到视图层，不能原地改写
    const head = items[0].text.replace(/^\n+/, '');
    if (head !== items[0].text) items[0] = Object.assign({}, items[0], { text: head });
    const lastIdx = items.length - 1;
    const tail = items[lastIdx].text.replace(/\n+$/, '');
    if (tail !== items[lastIdx].text) items[lastIdx] = Object.assign({}, items[lastIdx], { text: tail });
  }
  // 视图层不渲染 undefined：空块兜底成一个空文本段
  return items.length ? items : [{ t: 'text', text: '' }];
}

function inlineOf(token) {
  return tidy(mergeBreaks(flattenInline(token.tokens || [{ type: 'text', text: token.text || '' }])));
}

// 列表：嵌套列表摊平成带 depth 的条目（视觉缩进由 depth 决定），
// 条目内多段用换行连接。AI 回答的列表嵌套一般不超过一层
function listBlock(tk) {
  const items = [];
  let pending = null;

  const pushText = (b) => {
    if (!pending) pending = { depth: 0, inlines: [] };
    if (pending.inlines.length) pending.inlines.push({ t: 'text', text: '\n' });
    pending.inlines.push.apply(pending.inlines, mergeBreaks(flattenInline(b.tokens || [{ type: 'text', text: b.text || '' }])));
  };

  (tk.items || []).forEach((it) => {
    pending = null;
    (it.tokens || []).forEach((b) => {
      if (b.type === 'list') {
        if (pending) {
          items.push(pending);
          pending = null;
        }
        listBlock(b).items.forEach((nested) => {
          items.push({ depth: nested.depth + 1, inlines: nested.inlines });
        });
        return;
      }
      if (b.type === 'text' || b.type === 'paragraph') pushText(b);
    });
    if (pending) items.push(pending);
  });

  return {
    tag: tk.ordered ? 'ol' : 'ul',
    start: tk.start || 1,
    items: items.map((it) => ({ depth: it.depth, inlines: tidy(it.inlines) }))
  };
}

function cellOf(c) {
  return tidy(mergeBreaks(flattenInline(c.tokens || [{ type: 'text', text: c.text || '' }])));
}

function tableBlock(tk) {
  const head = (tk.header || []).map(cellOf);
  const rows = (tk.rows || []).map((r) => r.map(cellOf));
  // 不输出 tk.align：WXML 侧没有对应的对齐实现，带出去只会变成一份没人读的数据
  return { tag: 'table', head: head, rows: rows };
}

// 入口：markdown 源码 -> blocks 数组。
// 解析失败（极端不完整语法）降级为单段纯文本，绝不让气泡空着
function parseMarkdown(md) {
  const src = String(md || '');
  if (!src.trim()) return [];
  let tokens;
  try {
    tokens = new Lexer(LEXER_OPTIONS).lex(src);
  } catch (e) {
    return [{ tag: 'p', inlines: [{ t: 'text', text: src }] }];
  }

  const blocks = [];
  (tokens || []).forEach((tk) => {
    switch (tk.type) {
      case 'heading':
        blocks.push({ tag: 'h', depth: tk.depth || 2, inlines: inlineOf(tk) });
        break;
      case 'paragraph':
      case 'text':
        blocks.push({ tag: 'p', inlines: inlineOf(tk) });
        break;
      case 'list':
        blocks.push(listBlock(tk));
        break;
      case 'blockquote': {
        // 引用块摊平为一段（内部多段用换行连接），左竖线样式见 .md-quote
        const inlines = [];
        (tk.tokens || []).forEach((b) => {
          if (b.type !== 'paragraph' && b.type !== 'text') return;
          if (inlines.length) inlines.push({ t: 'text', text: '\n' });
          inlines.push.apply(inlines, mergeBreaks(flattenInline(b.tokens || [{ type: 'text', text: b.text || '' }])));
        });
        if (inlines.length) blocks.push({ tag: 'quote', inlines: tidy(inlines) });
        break;
      }
      case 'code':
        blocks.push({ tag: 'code', text: tk.text || '', lang: tk.lang || '' });
        break;
      case 'hr':
        blocks.push({ tag: 'hr' });
        break;
      case 'table':
        blocks.push(tableBlock(tk));
        break;
      case 'space':
      case 'html':
        // 空格跳过；原始 HTML 不渲染（安全 + 富文本能力之外）
        break;
      default:
        break;
    }
  });
  return blocks;
}

// ===== 流式增量解析 =====
// 流式回答是逐帧揭示的。若每帧都对整段文本跑 parseMarkdown，视图层每帧都要收下整篇
// blocks（长答几十 KB × 每秒几十次跨线程传输），已读完的段落还会被反复重建、版式跟着抖。
// 这里把文本切成「已定型的段落」+「正在写的尾段」：
//   - 遇到空行边界就把这一段解析一次、永久定格（每个字全生命周期只解析一次）；
//   - 尾段每帧重解析，但它只有最后一段，长度有限。
// 视图层因此可以只 patch 尾部下标，长答也不会越写越慢。
const FENCE_LINE_RE = /^[ \t]*(?:```|~~~)/;

// 空行是否落在代码围栏内部：围栏行出现奇数次即「还没闭合」。
// 在那种位置切段会把一个代码块拆成两半（等宽块当场断掉）
function fenceClosed(text) {
  if (text.indexOf('```') === -1 && text.indexOf('~~~') === -1) return true;
  const lines = text.split('\n');
  let open = 0;
  for (let i = 0; i < lines.length; i++) {
    if (FENCE_LINE_RE.test(lines[i])) open += 1;
  }
  return open % 2 === 0;
}

// 在 [from, text.length) 里找最后一个可安全切段的空行，返回切点之后的下标（-1 没找到）
function stableEnd(text, from) {
  const fenced = text.indexOf('```') !== -1 || text.indexOf('~~~') !== -1;
  let at = text.length;
  while ((at = text.lastIndexOf('\n\n', at - 1)) >= from) {
    if (!fenced || fenceClosed(text.slice(0, at))) return at + 2;
  }
  return -1;
}

// 流式解析器：update(text) 返回 { blocks, tail, committed }
//   blocks    已定型段落（没有新段落定型时引用不变，可拿来判断能否只 patch 尾段）
//   tail      正在写的尾段，每帧重新给
//   committed 这次有段落被定型，视图层必须整段重写 blocks（下标整体后移了）
function createMarkdownStream() {
  let stableLen = 0;
  let blocks = [];
  return {
    update(text) {
      const src = String(text == null ? '' : text);
      const end = src.length > stableLen ? stableEnd(src, stableLen) : -1;
      const committed = end > stableLen;
      if (committed) {
        const added = parseMarkdown(src.slice(stableLen, end));
        if (added.length) blocks = blocks.concat(added);
        stableLen = end;
      }
      return { blocks: blocks, tail: parseMarkdown(src.slice(stableLen)), committed: committed };
    }
  };
}

// 段落去重的最小长度：短句重复是正常的（分点标签、过渡句），只清理成段内容
const DUP_PARAGRAPH_MIN_CHARS = 30;

// 正文去重：模型偶发把同一段结论写两遍（同轮工具数据重复时更常见）。
// 只做确定性去重——按空行分段、抹掉空白后完全相同的段落只留第一段；
// 含代码围栏时整段跳过，避免把代码里的空行切坏。
// 与后端 agent/core/sanitize.py 的 dedupe_answer_text 保持同一口径。
function dedupeParagraphs(text) {
  const src = String(text == null ? '' : text);
  if (!src || src.indexOf('```') !== -1) return src;
  const parts = src.split(/\n{2,}/);
  const seen = Object.create(null);
  const kept = [];
  let dropped = false;
  for (let i = 0; i < parts.length; i++) {
    const key = parts[i].replace(/\s+/g, '');
    if (key.length >= DUP_PARAGRAPH_MIN_CHARS) {
      if (seen[key]) {
        dropped = true;
        continue;
      }
      seen[key] = true;
    }
    kept.push(parts[i]);
  }
  return dropped ? kept.join('\n\n') : src;
}

export { parseMarkdown, createMarkdownStream, dedupeParagraphs };
