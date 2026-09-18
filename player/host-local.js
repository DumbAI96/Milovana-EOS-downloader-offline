// Offline RPC host for the shared Eos player.
// Stand-in for milovana.com's eos.outer.js (reference copy: player-source/eos.outer.js).
// Reads ?tease=<relative path to the tease folder> from this page's URL, e.g.
//   /player/?tease=../teases/73009 How many lessons do you need
// State policy: tease progress is stored as FILES under offline/state/ (via our
// server), never in browser storage.
(function () {
  "use strict";

  var iframe = document.getElementsByClassName("eosIframe")[0];
  var qs = new URLSearchParams(location.search);
  var tease = (qs.get("tease") || "").replace(/\/+$/, "");

  var loaded = null; // { title, author, script, preview }
  var teaseId = "unknown";

  // storage path for THIS tease:  /state/<tease folder>/storage.json
  function stateUrl(name) {
    var folder = tease.split("/").pop() || "unknown";
    return "/state/" + encodeURIComponent(folder) + "/" + name;
  }

  function loadScript() {
    if (loaded) return Promise.resolve(loaded);
    return Promise.all([
      fetch(tease + "/tease.json").then(function (r) { return r.json(); }),
      fetch(tease + "/tease-meta.json")
        .then(function (r) { return r.json(); })
        .catch(function () { return {}; })
    ]).then(function (res) {
      var meta = res[1] || {};
      teaseId = (meta.id != null) ? meta.id : tease;
      loaded = {
        title: meta.title || "",
        author: meta.author || "",
        script: res[0],
        preview: false
      };
      return loaded;
    });
  }

  var rpcMethods = {
    ready: function () { return true; },
    load: function () { return loadScript(); },
    loadStorage: function () {
      return fetch(stateUrl("storage.json"))
        .then(function (r) { return r.ok ? r.text() : "{}"; })
        .catch(function () { return "{}"; });
    },
    saveStorage: function (params) {
      return fetch(stateUrl("storage.json"), { method: "POST", body: params.state })
        .then(function (r) { return r.ok; })
        .catch(function (e) {
          console.error("[host] state save failed:", e);
          return false;
        });
    },
    openRatingDialog: function () { return true; }, // offline: noop
    goToAuthor: function () { return true; }         // offline: noop
  };

  window.addEventListener("message", function (event) {
    if (event.source !== iframe.contentWindow) {
      console.error("Received message from unknown source, ignoring");
      return;
    }
    var method = event.data.method;
    var params = event.data.params;
    var id = event.data.id;

    if (!Object.prototype.hasOwnProperty.call(rpcMethods, method)) {
      console.error('Unknown rpc method "' + method + '"');
      return;
    }
    Promise.resolve(rpcMethods[method](params))
      .then(function (result) {
        iframe.contentWindow.postMessage({ jsonrpc: "2.0", result: result, id: id }, "*");
      })
      .catch(function (error) {
        iframe.contentWindow.postMessage({ jsonrpc: "2.0", error: { message: error.message }, id: id }, "*");
      });
  });
})();
