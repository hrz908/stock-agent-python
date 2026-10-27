// 极简 LRU（无依赖）：语音同传的译文与 TTS 音频路径都用它缓存。
//
// 这两类缓存都要在「省一次插件调用」和「别把内存 / 临时文件堆爆」之间取平衡，
// 淘汰顺序必须确定。Map 的插入顺序天然就是顺序表：命中后先删后插，
// 队首即最久未使用，超容时从队首淘汰。
//
// 用法：
//   const cache = createLRU(24, { onEvict: (value) => ... });
//   cache.set('k', 'v'); cache.get('k'); cache.size();
function createLRU(limit, options) {
  // 容量至少 1：传 0 / 负数 / 非数字都按 1 处理，调用方不用先校验
  const max = Math.max(1, Math.floor(Number(limit) || 0) || 1);
  const onEvict = options && typeof options.onEvict === 'function' ? options.onEvict : null;
  const map = new Map();

  function get(key) {
    if (!map.has(key)) return undefined;
    const value = map.get(key);
    map.delete(key);
    map.set(key, value);
    return value;
  }

  function set(key, value) {
    if (map.has(key)) map.delete(key);
    map.set(key, value);
    // 一次可能淘汰多条（调用方把 limit 调小时），所以用 while
    while (map.size > max) {
      const oldestKey = map.keys().next().value;
      const evicted = map.get(oldestKey);
      map.delete(oldestKey);
      if (onEvict) onEvict(evicted, oldestKey);
    }
    return value;
  }

  function has(key) {
    return map.has(key);
  }

  function remove(key) {
    return map.delete(key);
  }

  function clear() {
    map.clear();
  }

  function size() {
    return map.size;
  }

  return { get, set, has, delete: remove, clear, size, limit: max };
}

export { createLRU };
