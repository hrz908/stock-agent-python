// 稳定片段切分：同传要在「识别还在继续」的时候就发起翻译。
//
// 插件 onRecognize 给的是整段累计文本，尾部一两个字会被反复改写，
// 所以只切「已经派发出去之后新增的那一截」，并且：
//   - 短于 minLen 不派发：一两个字翻出来没意义，还白烧插件配额；
//   - 超过 preferLen 时优先切在最后一个标点之后，别把一句话拦腰截断；
//   - 一直没标点的长句到 maxLen 硬切，否则翻译永远等不到触发点；
//   - 硬切落在英文单词中间时退到空格，避免翻出半个词。
// 切多了、切偏了都不影响最终阅读：原文与译文各自按片段顺序拼接。

const SENTENCE_END = /[。！？!?；;]/;
const CLAUSE_END = /[，,、：:]/;

function isWordChar(ch) {
  return /[A-Za-z0-9]/.test(ch);
}

// text: 累计识别文本；dispatched: 已经派发出去的字数
// 返回 { segment, consumed } 或 null（还没攒够）
function takeStableSegment(text, dispatched, options) {
  const opts = options || {};
  const minLen = Math.max(1, opts.minLen === undefined ? 4 : opts.minLen);
  const preferLen = Math.max(minLen, opts.preferLen === undefined ? 14 : opts.preferLen);
  const maxLen = Math.max(preferLen, opts.maxLen === undefined ? 28 : opts.maxLen);

  const src = String(text === null || text === undefined ? '' : text);
  const start = Math.max(0, Math.min(Math.floor(Number(dispatched) || 0), src.length));
  const delta = src.slice(start);
  if (delta.length < minLen) return null;

  let cut = delta.length;
  if (delta.length > preferLen) {
    let lastBreak = -1;
    for (let i = 0; i < delta.length; i++) {
      if (SENTENCE_END.test(delta[i]) || CLAUSE_END.test(delta[i])) lastBreak = i;
    }
    if (lastBreak >= minLen - 1) cut = lastBreak + 1;
    else if (delta.length >= maxLen) cut = maxLen;
  }

  // 英文硬切不能落在词内：往回退到最近一个空白
  if (cut < delta.length && isWordChar(delta[cut - 1]) && isWordChar(delta[cut])) {
    let space = cut - 1;
    while (space > 0 && !/\s/.test(delta[space])) space--;
    if (space > 0 && space + 1 >= minLen) cut = space + 1;
  }

  const segment = delta.slice(0, cut);
  if (!segment.trim()) return null;
  return { segment, consumed: cut };
}

export { takeStableSegment };
