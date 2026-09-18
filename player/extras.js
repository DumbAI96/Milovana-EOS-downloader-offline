// EOS offline extras — the shared feature layer of the offline player.
// Loaded by player.html (module, after the app bundle). Runs INSIDE the player iframe.
// Needs one tiny documented bundle patch: window.__EOS_HOST__ = engine instance.
// State policy: all persisted state (settings, future saves) lives in FILES under
// offline/state/ via our server - never in browser storage. See eosState.* below.
//
// Features are added here over time — NOT in the (minified) app bundle.
// Current features:
//   1) Auto-advance: simulates a SPACE keydown (the engine's own "continue" input)
//      every N seconds, but ONLY while a `say` bubble is actually waiting. Prompts,
//      choices, notification buttons and timers cannot be affected by design.
//      Fallback: if a synthetic key event can't be built (which !== 32), the engine's
//      continue event is used directly - same net effect as pressing SPACE.
//   2) NoDelay: with window.__EOSX_NO_DELAY__ set, EVERY say renders as a normal
//      "pause" bubble (one engine patch forces the mode) - click-through + arrow, and
//      Auto can detect/advance them. A second small patch keeps an already-showing
//      delayed bubble skippable at toggle time. Engine-level; off = original.
//   3) Timer controls [Hidden]/[All]/[⏭ Now]: flags consulted by the timer activity
//      component (engine patch), checked every frame => live, even skips waits already
//      in progress. __EOSX_SKIP_TIMER_NOW__ is one-shot (consumed on use; the button
//      auto-clears it if no timer consumes it).
//   4) Nav panel [Nav]: jump to any page (current page highlighted) + MANUAL checkpoints
//      ([+ Save] at moments that matter; click a row to restore vars + storage + page).
//      Uses engine APIs only (no bundle patch). Checkpoints: state/<tease>/checkpoints.json.
//   5) Timer multiplier (x field in the Timers group): scales the LENGTH of every timer
//      that STARTS after the value is set (classic/async/NyX/notification timers):
//      0.5 = half length (faster), 2 = twice the length (slower), 1 = normal.
//      Engine patches = duration wrap at timer creation (PATCHES.txt 1f + 4).
//      Skip toggles are unaffected and always override (skip wins).
//   6) Back + group dividers: [<- Back] (right end of the bar) leaves the player and
//      returns to the landing page (tease list); dim spade dividers separate the bar
//      groups (Auto / NoDelay / Timers / Nav+Back). Pure UI - no engine involvement.
(function () {
  "use strict";

  var host = null;
  var tries = 0;

  // ---------- folder-based state (offline/state/ via our server) ----------
  var TEASE_FOLDER = (function () {
    try {
      var d = new URLSearchParams(location.search).get("data") || "";
      return d.split("/").pop() || "";
    } catch (e) { return ""; }
  })();

  function stateGet(path) {
    return fetch("/state/" + path)
      .then(function (r) { return r.ok ? r.text() : null; })
      .catch(function () { return null; });
  }

  function stateSet(path, text) {
    return fetch("/state/" + path, { method: "POST", body: text })
      .catch(function (e) { console.warn("[extras] state write failed:", e); });
  }

  // helper for future features: global + per-tease state files
  window.eosState = {
    get: function (name) { return stateGet("global/" + name + ".json"); },
    set: function (name, obj) { return stateSet("global/" + name + ".json", JSON.stringify(obj)); },
    getTease: function (name) {
      return TEASE_FOLDER ? stateGet(encodeURIComponent(TEASE_FOLDER) + "/" + name + ".json")
                          : Promise.resolve(null);
    },
    setTease: function (name, obj) {
      return TEASE_FOLDER ? stateSet(encodeURIComponent(TEASE_FOLDER) + "/" + name + ".json",
                                     JSON.stringify(obj))
                          : Promise.resolve();
    }
  };

  // ---------- settings (persisted as FILES under offline/state/global/) ----------
  var S = { auto: false, autoSeconds: 3, noDelay: false, skipHidden: false, skipAll: false, timerMult: 1 };

  function clampSeconds(v) {
    v = parseFloat(v);
    if (!isFinite(v)) v = 3;
    v = Math.round(v * 10) / 10;   // one decimal place
    if (v < 0.1) v = 0.1;
    if (v > 120) v = 120;
    return v;
  }

  function clampMult(v) {
    v = parseFloat(v);
    if (!isFinite(v)) v = 1;
    v = Math.round(v * 10) / 10;   // one decimal place
    if (v < 0.1) v = 0.1;
    if (v > 10) v = 10;
    return v;
  }

  function loadSettings(done) {
    stateGet("global/settings.json").then(function (txt) {
      if (txt) {
        try {
          var v = JSON.parse(txt);
          if (typeof v.auto === "boolean") S.auto = v.auto;
          if (v.autoSeconds !== undefined) S.autoSeconds = clampSeconds(v.autoSeconds);
          if (typeof v.noDelay === "boolean") S.noDelay = v.noDelay;
          if (typeof v.skipHidden === "boolean") S.skipHidden = v.skipHidden;
          if (typeof v.skipAll === "boolean") S.skipAll = v.skipAll;
          if (v.timerMult !== undefined) S.timerMult = clampMult(v.timerMult);
        } catch (e) { /* ignore broken settings */ }
      }
      if (done) done();
    });
  }

  function saveSettings() {
    stateSet("global/settings.json", JSON.stringify(S));
  }

  function applyFlags() {
    window.__EOSX_NO_DELAY__ = !!S.noDelay;
    window.__EOSX_SKIP_HIDDEN_TIMERS__ = !!S.skipHidden;
    window.__EOSX_SKIP_ALL_TIMERS__ = !!S.skipAll;
    window.__EOSX_TIMER_MULT__ = S.timerMult;
  }

  // ---------- UI (top bar cluster) ----------
  var bar = null, btn = null, btn2 = null, btnH = null, btnA = null, btnN = null, btnNav = null, input = null, inputM = null, btnBack = null;

  function buildUI() {
    var style = document.createElement("style");
    style.textContent =
      ".eosx-bar{display:flex;align-items:center;gap:6px;margin-right:10px;font:12px/1.2 'Segoe UI',sans-serif;}" +
      ".eosx-btn{padding:4px 9px;border:1px solid #555;border-radius:6px;background:#222;color:#aaa;cursor:pointer;font:inherit;}" +
      ".eosx-btn:hover{color:#eee;border-color:#888;}" +
      ".eosx-btn.on{background:#2e7d32;border-color:#4caf50;color:#fff;}" +
      ".eosx-btn:active{transform:translateY(1px);}" +
      ".eosx-sec{width:46px;padding:3px 2px;border:1px solid #555;border-radius:6px;background:#222;color:#eee;text-align:center;font:inherit;}" +
      ".eosx-unit{color:#888;}" +
      ".eosx-sep{color:#666;padding:0 2px;user-select:none;}" +
      ".eosx-panel{position:fixed;top:40px;right:10px;width:340px;max-height:76vh;display:flex;flex-direction:column;background:rgba(10,10,10,.92);border:1px solid #444;border-radius:10px;z-index:10000;font:12px/1.35 'Segoe UI',sans-serif;color:#ddd;overflow:hidden;}" +
      ".eosx-ph{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border-bottom:1px solid #333;font-weight:bold;}" +
      ".eosx-x{cursor:pointer;color:#999;padding:0 4px;}" +
      ".eosx-x:hover{color:#fff;}" +
      ".eosx-sub{padding:6px 10px;color:#888;border-bottom:1px solid #2a2a2a;display:flex;justify-content:space-between;align-items:center;gap:6px;}" +
      ".eosx-filter{flex:1;min-width:0;background:#1c1c1c;border:1px solid #555;border-radius:6px;color:#eee;padding:3px 6px;font:inherit;}" +
      ".eosx-list{overflow-y:auto;}" +
      ".eosx-list.eosx-pages{max-height:34vh;}" +
      ".eosx-list.eosx-hist{max-height:26vh;border-top:1px solid #333;}" +
      ".eosx-row{padding:4px 10px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}" +
      ".eosx-row:hover{background:#222;}" +
      ".eosx-row.eosx-cur{background:#1e3a1e;color:#c8f0c8;font-weight:bold;}" +
      ".eosx-row .t{color:#777;}" +
      ".eosx-btn.eosx-resume{font-size:11px;padding:2px 8px;}" +
      ".eosx-del{cursor:pointer;color:#955;padding:0 6px;}" +
      ".eosx-del:hover{color:#f66;}" +
      ".eosx-status{padding:6px 10px;color:#9aa;border-top:1px solid #333;font-size:11px;min-height:14px;white-space:normal;}";
    document.head.appendChild(style);

    bar = document.createElement("div");
    bar.className = "eosx-bar";

    btn = document.createElement("button");
    btn.className = "eosx-btn";
    btn.title = "Auto-advance: continues text lines for you (never answers prompts / choices / buttons)";
    btn.addEventListener("click", function () {
      S.auto = !S.auto;
      saveSettings();
      render();
    });

    input = document.createElement("input");
    input.type = "number";
    input.min = "0.1";
    input.max = "120";
    input.step = "0.1";
    input.className = "eosx-sec";
    input.title = "Seconds between auto-advances (one decimal; 0.1 = skip dialog)";
    input.addEventListener("change", function () {
      S.autoSeconds = clampSeconds(input.value);
      saveSettings();
      render();
    });

    var unit = document.createElement("span");
    unit.className = "eosx-unit";
    unit.textContent = "s";

    btn2 = document.createElement("button");
    btn2.className = "eosx-btn";
    btn2.title = "NoDelay: make delayed text lines click-through (engine-level, all teases)";
    btn2.addEventListener("click", function () {
      S.noDelay = !S.noDelay;
      applyFlags();
      saveSettings();
      render();
    });

    var sepT = document.createElement("span");
    sepT.className = "eosx-unit";
    sepT.textContent = "Timers:";

    btnH = document.createElement("button");
    btnH.className = "eosx-btn";
    btnH.textContent = "Hidden";
    btnH.title = "Instantly skip HIDDEN timers (invisible waits) - live, works on a running wait too";
    btnH.addEventListener("click", function () {
      S.skipHidden = !S.skipHidden;
      applyFlags();
      saveSettings();
      render();
    });

    btnA = document.createElement("button");
    btnA.className = "eosx-btn";
    btnA.textContent = "All";
    btnA.title = "Instantly skip ALL timers, visible countdowns included (supersedes Hidden) - live";
    btnA.addEventListener("click", function () {
      S.skipAll = !S.skipAll;
      applyFlags();
      saveSettings();
      render();
    });

    btnN = document.createElement("button");
    btnN.className = "eosx-btn";
    btnN.textContent = "\u23ED Now";
    btnN.title = "Skip the timer that is running right now (one-shot)";
    btnN.addEventListener("click", function () {
      window.__EOSX_SKIP_TIMER_NOW__ = true;
      setTimeout(function () { window.__EOSX_SKIP_TIMER_NOW__ = false; }, 1500);
    });

    var unitM = document.createElement("span");
    unitM.className = "eosx-unit";
    unitM.textContent = "\u00D7";

    inputM = document.createElement("input");
    inputM.type = "number";
    inputM.min = "0.1";
    inputM.max = "10";
    inputM.step = "0.1";
    inputM.className = "eosx-sec";
    inputM.title = "Timer multiplier: scales every timer's length (0.5 = half length / faster, 2 = twice / slower; 1 = normal). Applies to timers that start after the change; the skip buttons still override.";
    inputM.addEventListener("change", function () {
      S.timerMult = clampMult(inputM.value);
      applyFlags();
      saveSettings();
      render();
    });

    function mkSep() {
      var s = document.createElement("span");
      s.className = "eosx-sep";
      s.textContent = "\u2660";
      return s;
    }

    btnBack = document.createElement("button");
    btnBack.className = "eosx-btn";
    btnBack.textContent = "\u2190 Back";
    btnBack.title = "Back to the landing page (the tease list)";
    btnBack.addEventListener("click", function () {
      var url = "/";
      try { url = new URL("..", location.href).href; } catch (e) {}
      try { window.top.location.href = url; }
      catch (e2) { window.location.href = url; }
    });

    btnNav = document.createElement("button");
    btnNav.className = "eosx-btn";
    btnNav.textContent = "Nav";
    btnNav.title = "Navigation: jump to any page / history checkpoints";
    btnNav.addEventListener("click", function () { togglePanel(); });

    bar.appendChild(btn);
    bar.appendChild(input);
    bar.appendChild(unit);
    bar.appendChild(mkSep());
    bar.appendChild(btn2);
    bar.appendChild(mkSep());
    bar.appendChild(sepT);
    bar.appendChild(btnH);
    bar.appendChild(btnA);
    bar.appendChild(btnN);
    bar.appendChild(unitM);
    bar.appendChild(inputM);
    bar.appendChild(mkSep());
    bar.appendChild(btnNav);
    bar.appendChild(btnBack);

    render();
  }

  function render() {
    if (!bar) return;
    btn.textContent = S.auto ? "Auto \u25CF" : "Auto \u25B6";
    btn.classList.toggle("on", S.auto);
    input.value = S.autoSeconds.toFixed(1);
    inputM.value = S.timerMult.toFixed(1);
    btn2.textContent = "NoDelay";
    btn2.classList.toggle("on", S.noDelay);
    btnH.classList.toggle("on", S.skipHidden);
    btnA.classList.toggle("on", S.skipAll);
  }

  function placeBar() {
    // Preferred: inside the real top bar, right before the player icons.
    var spacer = document.querySelector('div[style*="flex: 1 1 0%"]');
    if (spacer && spacer.parentNode) {
      spacer.parentNode.insertBefore(bar, spacer.nextSibling);
      return true;
    }
    // Fallback anchor: the author button's header container.
    var authorBtn = document.querySelector('button[class*="authorButton"]');
    if (authorBtn && authorBtn.parentNode && authorBtn.parentNode.parentNode) {
      authorBtn.parentNode.parentNode.appendChild(bar);
      return true;
    }
    return false;
  }

  function ensurePlaced() {
    if (!bar) return;
    var spacer = document.querySelector('div[style*="flex: 1 1 0%"]');
    var inHeader = spacer && spacer.parentNode && spacer.parentNode.contains(bar);
    if (inHeader && document.body.contains(bar)) return;
    if (placeBar()) {
      // reset fallback styles when we land in the header
      bar.style.position = ""; bar.style.top = ""; bar.style.right = "";
      bar.style.zIndex = ""; bar.style.background = "";
      bar.style.padding = ""; bar.style.borderRadius = "";
      return;
    }
    if (!document.body.contains(bar)) {
      // last resort: floating overlay, top-right (below the bar)
      bar.style.position = "fixed"; bar.style.top = "44px"; bar.style.right = "10px";
      bar.style.zIndex = "9999"; bar.style.background = "rgba(0,0,0,.55)";
      bar.style.padding = "4px 8px"; bar.style.borderRadius = "8px";
      document.body.appendChild(bar);
    }
  }

  // ---------- auto-advance loop ----------
  var lastEmit = 0;

  function waitingForContinue() {
    return !!document.querySelector('[class*="continueIndicator"]');
  }

  // Simulate a real SPACE key press (the engine's own continue input).
  // Dispatching ON document.body targets only the engine's keydown listener;
  // nothing else (React handlers, focused inputs, buttons) is involved.
  function pressSpace() {
    var ev = null;
    try {
      ev = new KeyboardEvent("keydown", {
        key: " ", code: "Space", keyCode: 32, which: 32,
        bubbles: true, cancelable: true
      });
    } catch (e) { ev = null; }
    if (ev && ev.which === 32) {
      document.body.dispatchEvent(ev);
      return;
    }
    // fallback: engine's continue event (same net effect as SPACE)
    try { host.emit("continue"); } catch (e2) { console.error("[extras] continue failed", e2); }
  }

  function tick() {
    ensurePlaced();
    if (host && S.auto && waitingForContinue()) {
      var now = Date.now();
      if (now - lastEmit >= S.autoSeconds * 1000) {
        lastEmit = now;
        pressSpace();
      }
    }
    setTimeout(tick, 100);
  }

  // ---------- NAV: page jump + rolling history checkpoints (engine APIs only) ----------
  var CHECK_MAX = 100;
  var NAV = { currentId: null };
  var CHECK = { list: [] };   // manual checkpoints, newest first
  var panel = null, panelOpen = false, pagesEl = null, histEl = null, filtEl = null,
      chkLabel = null, chkStatus = null, lastCurRow = null;

  function navInit() {
    if (!host) return;
    NAV.currentId = (host.currentPage && host.currentPage.id) || null;
    try {
      host.on("page", function (id) {
        NAV.currentId = id;
        if (panelOpen) renderNavLists();
      });
    } catch (e) { console.warn("[extras] nav: page events unavailable", e); }
    try {
      if (host.virtualMachine && host.virtualMachine.on) {
        host.virtualMachine.on("init", captureSystemNames);
      }
    } catch (e) { console.warn("[extras] nav: VM init hook unavailable", e); }
    if (!SYS_NAMES) captureSystemNames();
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && panelOpen) closePanel();
    });
    loadCheckpoints();
  }

  function vmEval(code) {
    try { return host.virtualMachine.eval(code); } catch (e) { return undefined; }
  }
  // host.virtualMachine is a WRAPPER class; the real interpreter (globalObject,
  // pseudoToNative, setProperty, ...) lives at ._interpreter.
  function vmInterp() {
    var vmw = host.virtualMachine;
    return (vmw && vmw._interpreter) || vmw;
  }
  function vmGlobal() {
    var vm = vmInterp();
    return (vm && (vm.globalObject || (vm.globalScope && vm.globalScope.object))) || null;
  }
  function vmNative(v) {
    try { return vmInterp().pseudoToNative(v); } catch (e) { return undefined; }
  }

  // System globals = everything that exists in the VM at the engine's "init" moment
  // (engine + module infrastructure: EventTarget, console, teaseStorage, pages, ...).
  // NEVER dump or restore these: overwriting infrastructure (e.g. EventTarget) breaks
  // the engine's own event dispatch and freezes the tease until a page reload.
  var SYS_NAMES = null;
  function captureSystemNames() {
    try {
      var gobj = vmGlobal();
      var props = gobj && gobj.properties;
      if (!props) return;
      var set = {};
      for (var n in props) {
        if (Object.prototype.hasOwnProperty.call(props, n)) set[n] = 1;
      }
      SYS_NAMES = set;
      console.info("[extras] nav: captured " + Object.keys(set).length + " system global names");
    } catch (e) { console.warn("[extras] captureSystemNames failed:", e); }
  }
  function isSystemName(name) {
    if (VM_SKIP[name] || name.indexOf("__") === 0) return true;
    return !!(SYS_NAMES && SYS_NAMES[name]);
  }

  var VM_SKIP = {};
  ["console", "teaseStorage", "Notification", "Sound", "pages", "Math", "JSON", "Object",
   "Array", "String", "Number", "Boolean", "Date", "RegExp", "Error", "parseInt",
   "parseFloat", "isNaN", "isFinite", "NaN", "Infinity", "undefined", "eval", "this",
   "global", "globals", "EventTarget", "PageManager"].forEach(function (n) { VM_SKIP[n] = 1; });

  function dumpVars() {
    var out = {};
    try {
      // NOTE: script variables live on the REAL interpreter's global object
      // (host.virtualMachine._interpreter.globalObject). Never use getScope() -
      // it only works mid-run and throws when the VM is idle.
      var gobj = vmGlobal();
      var props = gobj && gobj.properties;
      if (!props) { console.warn("[extras] dumpVars: no global object found"); return out; }
      for (var name in props) {
        if (!Object.prototype.hasOwnProperty.call(props, name)) continue;
        if (isSystemName(name)) continue;
        var nat = vmNative(props[name]);
        if (nat === undefined || typeof nat === "function") continue;
        try { out[name] = JSON.parse(JSON.stringify(nat)); } catch (e) { /* skip */ }
      }
    } catch (e) { console.warn("[extras] var dump failed:", e); }
    return out;
  }

  function readStorageObj() {
    var res = vmNative(vmEval(
      "JSON.stringify((function(){var o={};try{for(var i=0;i<teaseStorage.length;i++){" +
      "var k=teaseStorage.key(i);o[k]=teaseStorage.getItem(k);}}catch(e){}return o;})())"));
    return (typeof res === "string" && res) ? res : "{}";
  }

  function setStatus(msg) {
    if (chkStatus) chkStatus.textContent = msg || "";
    if (msg) console.info("[extras] " + msg);
  }

  function saveCheckpoint() {
    if (!host || !host.virtualMachine) return;
    var page = NAV.currentId || (host.currentPage && host.currentPage.id) || "?";
    var label = ((chkLabel && chkLabel.value) || "").trim() || page;
    try {
      var vars = dumpVars();
      var storage = readStorageObj();
      var stCount = 0;
      try { stCount = Object.keys(JSON.parse(storage || "{}")).length; } catch (e) {}
      CHECK.list.unshift({ label: label, page: page, time: Date.now(), vars: vars, storage: storage });
      if (CHECK.list.length > CHECK_MAX) CHECK.list = CHECK.list.slice(0, CHECK_MAX);
      saveCheckpoints();
      if (chkLabel) chkLabel.value = "";
      renderNavLists();
      setStatus("checkpoint saved: " + label + " (page " + page + ", " +
                Object.keys(vars).length + " vars, " + stCount + " storage keys)");
    } catch (e) {
      console.warn("[extras] checkpoint save failed:", e);
      setStatus("checkpoint save FAILED (see console)");
    }
  }

  function deleteCheckpoint(idx) {
    CHECK.list.splice(idx, 1);
    saveCheckpoints();
    renderNavLists();
    setStatus("checkpoint deleted");
  }

  function saveCheckpoints() {
    if (!TEASE_FOLDER) return;
    stateSet(encodeURIComponent(TEASE_FOLDER) + "/checkpoints.json", JSON.stringify(CHECK.list));
  }

  function loadCheckpoints() {
    if (!TEASE_FOLDER) return;
    stateGet(encodeURIComponent(TEASE_FOLDER) + "/checkpoints.json").then(function (txt) {
      if (!txt) return;
      try {
        var arr = JSON.parse(txt);
        if (Object.prototype.toString.call(arr) === "[object Array]") {
          CHECK.list = arr.slice(0, CHECK_MAX);
          if (panelOpen) renderNavLists();
        }
      } catch (e) {}
    });
  }

  function jumpTo(pageId) {
    try {
      if (!host.pages || !host.pages[pageId]) { console.warn("[extras] no such page:", pageId); return; }
      Promise.resolve(host.showPage(pageId)).catch(function (e) { console.warn("[extras] jump failed:", e); });
    } catch (e) { console.warn("[extras] jump failed:", e); }
  }

  function restoreCheckpoint(chk) {
    if (!chk || !host) return;
    var problems = [];
    var stCount = 0, varCount = 0;
    // phase 1: storage values back into the live teaseStorage (memory + file)
    try {
      var st = JSON.parse(chk.storage || "{}") || {};
      Object.keys(st).forEach(function (k) {
        vmEval("teaseStorage.setItem(" + JSON.stringify(k) + "," + JSON.stringify(st[k]) + ")");
        stCount++;
      });
    } catch (e) { problems.push("storage: " + e); }
    // phase 2: VM globals (via the GLOBAL object - getScope() only works mid-run)
    var skippedSys = 0;
    try {
      var vm = vmInterp();
      var gobj = vmGlobal();
      if (!gobj) throw new Error("no global object");
      Object.keys(chk.vars || {}).forEach(function (k) {
        if (isSystemName(k)) { skippedSys++; return; }
        try { vm.setProperty(gobj, k, vm.nativeToPseudo(chk.vars[k])); varCount++; } catch (e) {}
      });
    } catch (e) { problems.push("vars: " + e); }
    // phase 3: jump - ALWAYS attempted, independent of the phases above
    try { jumpTo(chk.page); } catch (e) { problems.push("jump: " + e); }
    var msg = "restored: " + (chk.label || chk.page) + " (page " + chk.page + ", " +
              varCount + " vars, " + stCount + " storage keys" +
              (skippedSys ? ", skipped " + skippedSys + " system globals" : "") + ")";
    setStatus(problems.length ? msg + " - problems: " + problems.join(" | ") : msg);
    if (problems.length) console.warn("[extras] restore problems:", problems);
  }

  function fmtTime(t) {
    try {
      var d = new Date(t);
      return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2) +
             ":" + ("0" + d.getSeconds()).slice(-2);
    } catch (e) { return "?"; }
  }

  function buildPanel() {
    panel = document.createElement("div");
    panel.className = "eosx-panel";
    panel.style.display = "none";

    var head = document.createElement("div");
    head.className = "eosx-ph";
    var ht = document.createElement("span"); ht.textContent = "Navigation";
    var hx = document.createElement("span"); hx.className = "eosx-x"; hx.textContent = "\u00D7";
    hx.title = "Close (Esc)";
    hx.addEventListener("click", closePanel);
    head.appendChild(ht); head.appendChild(hx);

    var sub1 = document.createElement("div");
    sub1.className = "eosx-sub";
    var l1 = document.createElement("span"); l1.textContent = "Jump to page";
    filtEl = document.createElement("input");
    filtEl.className = "eosx-filter";
    filtEl.placeholder = "filter pages...";
    filtEl.addEventListener("input", renderNavLists);
    sub1.appendChild(l1); sub1.appendChild(filtEl);

    pagesEl = document.createElement("div");
    pagesEl.className = "eosx-list eosx-pages";

    var sub2 = document.createElement("div");
    sub2.className = "eosx-sub";
    var l2 = document.createElement("span"); l2.textContent = "Checkpoints";
    chkLabel = document.createElement("input");
    chkLabel.className = "eosx-filter";
    chkLabel.placeholder = "label (optional)";
    var chkSave = document.createElement("button");
    chkSave.className = "eosx-btn eosx-resume";
    chkSave.textContent = "+ Save";
    chkSave.title = "Save the CURRENT page + variables + progress as a checkpoint";
    chkSave.addEventListener("click", saveCheckpoint);
    sub2.appendChild(l2); sub2.appendChild(chkLabel); sub2.appendChild(chkSave);

    histEl = document.createElement("div");
    histEl.className = "eosx-list eosx-hist";

    chkStatus = document.createElement("div");
    chkStatus.className = "eosx-status";

    panel.appendChild(head);
    panel.appendChild(sub1);
    panel.appendChild(pagesEl);
    panel.appendChild(sub2);
    panel.appendChild(histEl);
    panel.appendChild(chkStatus);
    document.body.appendChild(panel);
  }

  function renderNavLists() {
    if (!panel) return;
    var filter = (filtEl.value || "").toLowerCase();
    var names = (host && host.getPageNames) ? host.getPageNames() : [];
    pagesEl.innerHTML = "";
    lastCurRow = null;
    names.forEach(function (n) {
      if (filter && n.toLowerCase().indexOf(filter) === -1) return;
      var row = document.createElement("div");
      row.className = "eosx-row" + (n === NAV.currentId ? " eosx-cur" : "");
      row.textContent = n;
      row.title = "Jump to " + n;
      row.addEventListener("click", function () { jumpTo(n); });
      pagesEl.appendChild(row);
      if (n === NAV.currentId) lastCurRow = row;
    });
    histEl.innerHTML = "";
    CHECK.list.forEach(function (chk, idx) {
      var row2 = document.createElement("div");
      row2.className = "eosx-row";
      var lbl = document.createElement("span");
      lbl.textContent = chk.label + "  ";
      var meta = document.createElement("span");
      meta.className = "t";
      meta.textContent = "[" + chk.page + " \u00B7 " + fmtTime(chk.time) + "]";
      var del = document.createElement("span");
      del.className = "eosx-del";
      del.textContent = "\u00D7";
      del.title = "Delete this checkpoint";
      del.addEventListener("click", function (ev) {
        ev.stopPropagation();
        deleteCheckpoint(idx);
      });
      row2.appendChild(lbl); row2.appendChild(meta); row2.appendChild(del);
      row2.title = "Restore: " + chk.label + " (page " + chk.page + ")";
      row2.addEventListener("click", function () { restoreCheckpoint(chk); });
      histEl.appendChild(row2);
    });
    if (!CHECK.list.length) {
      var e = document.createElement("div");
      e.className = "eosx-row";
      e.textContent = "(no checkpoints yet - hit [+ Save] at a moment you care about)";
      histEl.appendChild(e);
    }
  }

  function togglePanel() {
    if (!panel) buildPanel();
    panelOpen = !panelOpen;
    panel.style.display = panelOpen ? "flex" : "none";
    if (btnNav) btnNav.classList.toggle("on", panelOpen);
    if (panelOpen) {
      NAV.currentId = NAV.currentId || (host.currentPage && host.currentPage.id) || null;
      renderNavLists();
      if (lastCurRow && lastCurRow.scrollIntoView) {
        try { lastCurRow.scrollIntoView({ block: "center" }); }
        catch (e) { try { lastCurRow.scrollIntoView(); } catch (e2) {} }
      }
    }
  }
  function closePanel() {
    if (panelOpen) {
      panelOpen = false;
      panel.style.display = "none";
      if (btnNav) btnNav.classList.remove("on");
    }
  }

  // ---------- boot ----------
  function start() {
    buildUI();
    navInit();
    loadSettings(function () { applyFlags(); render(); });
    ensurePlaced();
    tick();
    console.info("[extras] feature layer loaded (state -> offline/state/ files)");
  }

  (function waitForHost() {
    if (window.__EOS_HOST__) { host = window.__EOS_HOST__; start(); return; }
    if (++tries <= 200) { setTimeout(waitForHost, 100); }
    else { console.warn("[extras] EosHost not found — extras disabled"); }
  })();
})();
