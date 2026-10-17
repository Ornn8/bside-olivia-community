// Test helper, preloaded into every Node process the tests start and added to every browser page they open
// (see tests/conftest.py).
//
// The injected UI scripts call a local t(zh, vars). Harnesses that run a slice of a script start after that
// definition, so a global t(zh, vars) with the Chinese-mode behaviour stands in: the Chinese source text with
// its {name} placeholders filled. Node harnesses that run the slice in a fresh vm context get it there too.
(() => {
  const helper = (zh, vars) => String(zh).replace(/\{(\w+)\}/g, (match, name) => (vars && name in vars ? vars[name] : match));
  const define = (target) => {
    if (target && typeof target === 'object' && !('t' in target)) {
      Object.defineProperty(target, 't', {value: helper, configurable: true, writable: true});
    }
    return target;
  };
  define(globalThis);
  if (typeof require === 'function') {
    const vm = require('node:vm');
    const createContext = vm.createContext;
    vm.createContext = (context, options) => createContext(define(context === undefined ? {} : context), options);
    const runInNewContext = vm.runInNewContext;
    vm.runInNewContext = (code, context, options) => runInNewContext(code, define(context === undefined ? {} : context), options);
    const scriptRun = vm.Script.prototype.runInNewContext;
    vm.Script.prototype.runInNewContext = function (context, options) {
      return scriptRun.call(this, define(context === undefined ? {} : context), options);
    };
  }
})();
