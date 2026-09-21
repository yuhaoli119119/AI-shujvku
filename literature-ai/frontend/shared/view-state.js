/* =====================================================================
   view-state.js —— 列表页 URL 状态同步（共享模块，无第三方依赖、无构建）
   ---------------------------------------------------------------------
   目的：把列表页的筛选 / 排序 / 分页 / 页签写进地址栏，这样
   「筛选 → 进详情 → 返回」能回到原来的筛选视图；带参数的 URL 直接粘到
   新标签也能还原同样的视图。

   最小用法：

     const PARAMS = {
       q:    { id: "searchInput", type: "text",   default: "" },
       year: { id: "yearFilter",  type: "select", default: "" },
       page: { type: "state",     default: "1" }
     };

     // 1) 读 URL → 填控件 → 接管控件 → 返回初始状态（用它发第一次查询，只发一次）
     const view = ViewState.bind({ paramMap: PARAMS, onChange: (key, value) => {...} });

     // 2) 页面自己改了 type:"state" 的值（页码 / 页签等）后写回 URL
     view.page = "2";
     ViewState.write(view, PARAMS);

     // 3) 所有「进详情」的入口统一改写链接（点击那一刻按当时 URL 生成 return_to）
     ViewState.bindDetailLinks("literature_library");

   API：
     ViewState.read(paramMap, { defaults })  从 location.search 读状态，缺失项取默认值
     ViewState.apply(state, paramMap)        把状态写进控件（只写 URL 里真实出现过的项）
     ViewState.write(state, paramMap)        history.replaceState 写回 URL；等于默认值的项删掉；
                                             文本输入类控件 250ms 防抖
     ViewState.detailUrl(baseUrl, { from })  生成详情页链接，自动带 from= 与 return_to=
     ViewState.bind({ paramMap, defaults, onChange })
                                             读 URL → 填控件 → 接管控件变化，返回初始状态
     ViewState.bindDetailLinks(from)         统一改写 a[href*=paper_detail] 的 href

   paramMap 每项：{ id, type, default }（type 省略时按控件自动判断）
     type: "text"（input/textarea，输入防抖）| "select" | "check"（复选框）| "state"（无控件，页面自管）
   复选框 URL 取值：出现且为 1/true/on/yes 表示勾选；未勾选则删除该参数。
   ===================================================================== */
(function (global) {
  "use strict";

  var WRITE_DEBOUNCE_MS = 250;
  var RETURN_TO_MAX = 512;
  /* 允许作为 return_to 的列表页白名单（防开放重定向） */
  var RETURN_PAGE_RE = /^\/pages\/(literature_library|literature_screening|dft_database|mechanism_knowledge|visuals|review_center)\/index\.html$/;
  var TEXT_INPUT_TYPES = ["text", "search", "number", "url", "tel", "password", "email"];

  function str(value) { return value === null || value === undefined ? "" : String(value); }
  function byId(id) { return id ? document.getElementById(id) : null; }
  function truthy(value) {
    var text = str(value).trim().toLowerCase();
    return text === "1" || text === "true" || text === "on" || text === "yes";
  }
  function hasOwn(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  /* 把 paramMap 归一成内部描述表 */
  function specsOf(paramMap, defaults) {
    var map = paramMap || {};
    var defs = defaults || {};
    return Object.keys(map).map(function (key) {
      var entry = map[key];
      var spec = (entry && typeof entry === "object") ? entry : { id: entry };
      return {
        key: key,
        id: spec.id ? str(spec.id) : "",
        type: spec.type || (spec.id ? "auto" : "state"),
        def: hasOwn(defs, key) ? str(defs[key]) : str(spec.default === undefined ? "" : spec.default),
        numeric: spec.numeric === true,
        valid: typeof spec.valid === "function" ? spec.valid : null
      };
    });
  }

  /* 控件真实类型（type:"auto" 时按元素判断；没有控件就是 state） */
  function typeOf(spec) {
    if (spec.type && spec.type !== "auto") return spec.type;
    var node = byId(spec.id);
    if (!node) return "state";
    var tag = str(node.tagName).toUpperCase();
    if (tag === "SELECT") return "select";
    if (tag === "INPUT" && str(node.type).toLowerCase() === "checkbox") return "check";
    if (tag === "INPUT" || tag === "TEXTAREA") return "text";
    return "state";
  }

  /* 值是否可接受：数字类必须是非负整数，valid() 可以再收紧 */
  function acceptable(spec, value) {
    var text = str(value);
    if (text === "") return false;
    if (spec.numeric && !/^\d+$/.test(text)) return false;
    if (spec.valid) {
      try { return spec.valid(text) !== false; } catch (_) { return false; }
    }
    return true;
  }

  function readFromControl(spec, type) {
    var node = byId(spec.id);
    if (!node) return "";
    if (type === "check") return node.checked ? "1" : "";
    return str(node.value);
  }

  function writeToControl(spec, type, value) {
    var node = byId(spec.id);
    if (!node) return;
    if (type === "check") { node.checked = truthy(value); return; }
    node.value = str(value);
  }

  /* 读 location.search；缺失项取默认值 */
  function read(paramMap, options) {
    var opts = options || {};
    var specs = specsOf(paramMap, opts.defaults);
    var search = new URLSearchParams(global.location.search);
    var out = {};
    specs.forEach(function (spec) {
      var type = typeOf(spec);
      var raw = search.get(spec.key);
      var value = raw === null ? spec.def : raw;
      if (type === "check") value = truthy(value) ? "1" : (raw === null ? spec.def : "");
      if (!acceptable(spec, value)) value = spec.def;
      out[spec.key] = value;
    });
    return out;
  }

  /* 把状态写进控件；只写 URL 里真实出现过的项。
     返回「控件上真正生效的值」，用于页面发第一次查询。
     只写 present 的项是为了不覆盖那些「缺省时由页面自己决定」的控件
     （例如文献库下拉：URL 没带 library_name 时要保留页面解析出的当前库）。 */
  function apply(state, paramMap, options) {
    var opts = options || {};
    var specs = specsOf(paramMap, opts.defaults);
    var search = new URLSearchParams(global.location.search);
    var out = {};
    specs.forEach(function (spec) {
      var type = typeOf(spec);
      var value = str(state[spec.key]);
      if (type === "state") {
        out[spec.key] = acceptable(spec, value) ? value : spec.def;
        return;
      }
      if (search.get(spec.key) !== null && acceptable(spec, value)) writeToControl(spec, type, value);
      out[spec.key] = readFromControl(spec, type);
    });
    return out;
  }

  /* 用 replaceState 写回 URL；等于默认值的项删掉，保持链接短、可读 */
  function write(state, paramMap, options) {
    var opts = options || {};
    var specs = specsOf(paramMap, opts.defaults);
    var search = new URLSearchParams(global.location.search);
    specs.forEach(function (spec) {
      var value = str(state[spec.key]);
      if (typeOf(spec) === "check") value = truthy(value) ? "1" : "";
      if (value === "" || value === spec.def) search.delete(spec.key);
      else search.set(spec.key, value);
    });
    var query = search.toString();
    var url = global.location.pathname + (query ? "?" + query : "") + global.location.hash;
    global.history.replaceState(global.history.state, "", url);
  }

  /* 当前完整 URL（不含 hash）：详情页 return_to 用 */
  function currentUrl() {
    return global.location.origin + global.location.pathname + global.location.search;
  }

  /* 生成详情页链接：自动带 from=<来源页> 与 return_to=<当前完整 URL> */
  function detailUrl(baseUrl, options) {
    var opts = options || {};
    var base = str(baseUrl);
    var hash = "";
    var hashAt = base.indexOf("#");
    if (hashAt >= 0) { hash = base.slice(hashAt); base = base.slice(0, hashAt); }
    var qAt = base.indexOf("?");
    var path = qAt >= 0 ? base.slice(0, qAt) : base;
    var query = new URLSearchParams(qAt >= 0 ? base.slice(qAt + 1) : "");
    if (opts.from) query.set("from", str(opts.from));
    var returnTo = opts.returnTo === undefined ? currentUrl() : str(opts.returnTo);
    if (returnTo) query.set("return_to", returnTo);
    else query.delete("return_to");
    var qs = query.toString();
    return path + (qs ? "?" + qs : "") + hash;
  }

  /* 读 URL → 填控件 → 接管控件 change/input → 更新状态 → 写 URL → 调 onChange。
     返回的初始状态对象同时是模块内部状态，页面改完 type:"state" 的项后
     调 ViewState.write(state, paramMap) 即可写回。 */
  function bind(options) {
    var opts = options || {};
    var paramMap = opts.paramMap || {};
    var defaults = opts.defaults || {};
    var specs = specsOf(paramMap, defaults);
    var state = apply(read(paramMap, { defaults: defaults }), paramMap, { defaults: defaults });
    var timer = null;

    function scheduleWrite(delay) {
      if (timer) global.clearTimeout(timer);
      timer = global.setTimeout(function () {
        timer = null;
        write(state, paramMap, { defaults: defaults });
      }, delay);
    }

    specs.forEach(function (spec) {
      var type = typeOf(spec);
      if (type === "state") return;
      var node = byId(spec.id);
      if (!node) return;
      var handler = function () {
        state[spec.key] = readFromControl(spec, type);
        scheduleWrite(type === "text" ? WRITE_DEBOUNCE_MS : 0);
        if (typeof opts.onChange === "function") {
          try { opts.onChange(spec.key, state[spec.key], state); } catch (_) {}
        }
        /* onChange 可能又改了 state（例如筛选变化后页码回 1），按同一节奏再写一次 */
        scheduleWrite(type === "text" ? WRITE_DEBOUNCE_MS : 0);
      };
      node.addEventListener(type === "text" ? "input" : "change", handler);
    });

    /* 冷启动时把非法值 / 冗余的默认值顺手规整回 URL */
    scheduleWrite(0);
    return state;
  }

  /* 统一改写「进详情」链接：筛选可能在渲染后变化，href 必须在点击那一刻生成 */
  function bindDetailLinks(from) {
    document.addEventListener("click", function (event) {
      var node = event.target;
      while (node && node !== document) {
        if (node.tagName === "A" && /paper_detail\/index\.html/.test(str(node.getAttribute && node.getAttribute("href")))) break;
        node = node.parentNode;
      }
      if (!node || node === document || !node.getAttribute) return;
      node.setAttribute("href", detailUrl(node.getAttribute("href"), { from: from }));
    }, true);
  }

  /* 校验 return_to：同源 + 白名单页面 + 长度上限；不合法返回 "" */
  function safeReturnTo(raw) {
    var value = str(raw);
    if (!value || value.length > RETURN_TO_MAX) return "";
    var url;
    try { url = new URL(value, global.location.href); } catch (_) { return ""; }
    if (url.origin !== global.location.origin) return "";
    if (!RETURN_PAGE_RE.test(url.pathname)) return "";
    url.searchParams.delete("return_to");
    url.hash = "";
    return url.pathname + (url.search ? url.search : "");
  }

  global.ViewState = {
    read: read,
    apply: apply,
    write: write,
    detailUrl: detailUrl,
    bind: bind,
    bindDetailLinks: bindDetailLinks,
    safeReturnTo: safeReturnTo
  };
})(window);
