/**
 * 单元测试的最小加载器（零依赖）。
 *
 * 小程序源码是 ESM，而测试跑在 Node 的 CJS 里（package.json 没有 type: module）：
 * 直接 require 会在 import/export 语法处报错。这里沿用 tests/newchat/harness.js 的思路，
 * 只做两件事，绝不转译业务代码本身：
 *   1. 顶层 import -> __req('模块')，模块由调用方注入替身；
 *   2. export { a, b } / export default X -> module.exports。
 *
 * 于是被测代码一行未改，测的就是真源码。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const RE_IMPORT = /^import\s+([\w{}\s,]+?)\s+from\s+['"]([^'"]+)['"];?/gm;
const RE_EXPORT_LIST = /^export\s*\{([\s\S]*?)\};?/m;
const RE_EXPORT_DEFAULT = /^export\s+default\s+([\w$]+);?/m;

function toCommonJS(src) {
  return src
    .replace(RE_IMPORT, (_, clause, mod) => {
      const c = clause.trim();
      if (c.startsWith('{')) {
        const names = c
          .slice(1, -1)
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean)
          .map((s) => s.replace(/^(\w+)\s+as\s+(\w+)$/, '$1: $2'));
        return 'const { ' + names.join(', ') + " } = __req('" + mod + "');";
      }
      return 'const ' + c + " = __req('" + mod + "').default;";
    })
    .replace(RE_EXPORT_LIST, (_, names) => 'module.exports = {' + names + '};')
    .replace(RE_EXPORT_DEFAULT, (_, name) => 'module.exports = ' + name + ';');
}

/**
 * 加载一个源码模块。
 * - modules: { '相对路径': 替身对象 }，键与源码里 from 后面的字符串一致
 * - globals: 额外注入的全局（如 wx）
 */
function loadModule(filePath, modules, globals) {
  const source = toCommonJS(fs.readFileSync(filePath, 'utf8'));
  const sandbox = Object.assign(
    {
      __req: (id) => {
        const m = (modules || {})[id];
        if (m === undefined) throw new Error('缺少模块替身: ' + id);
        return m;
      },
      module: { exports: {} },
      console: console,
      setTimeout: setTimeout,
      clearTimeout: clearTimeout
    },
    globals || {}
  );
  vm.runInNewContext(source, sandbox, { filename: path.basename(filePath) });
  return sandbox.module.exports;
}

module.exports = { loadModule, toCommonJS };
