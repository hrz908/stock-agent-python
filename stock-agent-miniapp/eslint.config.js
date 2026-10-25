/**
 * ESLint 扁平配置（仅开发期使用，不参与小程序打包）。
 *
 * 目标不是统一风格（项目没引 formatter），而是拦住「跑起来才炸」的硬错误：
 * 未声明变量、重复声明、重复键、不可达代码、typeof 拼写。
 * 风格类规则一概不开：这批代码里的 `== null`、空 catch、单行 if 都是有意的。
 */
const MP_GLOBALS = {
  wx: 'readonly',
  Page: 'readonly',
  Component: 'readonly',
  App: 'readonly',
  Behavior: 'readonly',
  getApp: 'readonly',
  getCurrentPages: 'readonly',
  requirePlugin: 'readonly',
  module: 'writable',
  exports: 'writable',
  require: 'readonly',
  // 小程序 JS 运行环境自带，不属于 ECMAScript 标准全局
  console: 'readonly',
  setTimeout: 'readonly',
  clearTimeout: 'readonly',
  setInterval: 'readonly',
  clearInterval: 'readonly'
};

const RULES = {
  'no-undef': 'error',
  'no-redeclare': 'error',
  'no-dupe-keys': 'error',
  'no-dupe-args': 'error',
  'no-dupe-class-members': 'error',
  'no-unreachable': 'error',
  'no-fallthrough': 'error',
  'no-cond-assign': 'error',
  'no-self-assign': 'error',
  'no-self-compare': 'error',
  'valid-typeof': 'error',
  'no-unsafe-negation': 'error',
  // 未使用变量只提示：历史代码里存在有意保留的解构/占位参数。
  // caughtErrors: none —— 大量 `catch (e) { /* 忽略 */ }` 是有意的降级，不该刷警告
  'no-unused-vars': ['warn', { args: 'none', caughtErrors: 'none', varsIgnorePattern: '^_' }]
};

module.exports = [
  {
    ignores: ['node_modules/**', 'miniprogram_npm/**']
  },
  {
    files: ['**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: MP_GLOBALS
    },
    rules: RULES
  },
  {
    // 测试跑在 Node 里，是 CJS：单独一套全局
    files: ['tests/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'commonjs',
      globals: Object.assign({}, MP_GLOBALS, {
        require: 'readonly',
        module: 'writable',
        exports: 'writable',
        Buffer: 'readonly',
        process: 'readonly',
        __dirname: 'readonly',
        setImmediate: 'readonly',
        console: 'readonly'
      })
    },
    rules: RULES
  }
];
