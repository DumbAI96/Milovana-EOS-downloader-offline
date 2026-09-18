#!/usr/bin/env python3
# Offline Eos teases - local server (stdlib only, no pip packages).
# Lives in server/ and serves the PARENT folder (the "offline/" root):
#   /                      -> landing page: sortable + filterable tease table
#   /player/?tease=..      -> the shared player (one copy, all teases)
#   /teases/<name>/        -> redirects to the player with the right ?tease= param
#   /teases/<name>/...     -> tease data (tease.json, tease-meta.json, timg/...)
# Features:
#  - Access-Control-Allow-Origin: *  (the player fetches media with CORS)
#  - timg media falls back through a chain: requested tier -> original -> xl -> l
#    -> m -> s (first existing file wins) so ANY downloaded quality works with ANY tease
#  - no-store caching; port auto-retry; OPEN_BROWSER=1 (set by start-server.bat).
import json
import os
import sys
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # offline/
STATE = os.path.join(ROOT, "state")   # all player/tease state lives here (files, not browser)
PORT = int(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PORT", "8123"))


def safe_state_path(rel):
    """Resolve a /state/<rel> path safely inside offline/state/ (or None)."""
    rel = rel.replace("\\", "/").strip("/")
    if not rel or ".." in rel.split("/") or ":" in rel:
        return None
    target = os.path.normpath(os.path.join(STATE, rel))
    if not target.startswith(STATE + os.sep):
        return None
    return target

_LANDING = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Offline teases</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --topbar: 54px; --headh: 34px; }
html, body { margin: 0; padding: 0; background: #111; color: #eee;
  font-family: 'Segoe UI', system-ui, sans-serif; }
.top { position: sticky; top: 0; z-index: 30; box-sizing: border-box;
  display: flex; align-items: center; gap: 12px; height: var(--topbar);
  padding: 0 16px; background: #181818; border-bottom: 1px solid #333; }
.top h1 { font-size: 1.05em; margin: 0; white-space: nowrap; }
.cnt { color: #888; font-size: .85em; white-space: nowrap; }
#fq { flex: 0 1 480px; box-sizing: border-box; background: #222; border: 1px solid #444;
  border-radius: 8px; color: #eee; padding: 7px 10px; font: inherit; }
#fq:focus { outline: none; border-color: #5a8; }
#fclear { border: 0; background: #262626; color: #aaa; cursor: pointer; font: inherit;
  font-size: .85em; padding: 6px 10px; border-radius: 7px; }
#fclear:hover { color: #fff; background: #303030; }
table { width: 100%; min-width: 900px; border-collapse: separate; border-spacing: 0;
  table-layout: fixed; }
thead th { position: sticky; top: var(--topbar); z-index: 20; box-sizing: border-box;
  height: var(--headh); background: #1c1c1c; border-bottom: 1px solid #333;
  padding: 6px 10px; text-align: left; font-size: .78em; letter-spacing: .04em;
  text-transform: uppercase; color: #9ab; cursor: pointer; user-select: none;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
thead th:hover { color: #cde; }
thead tr.filters th { top: calc(var(--topbar) + var(--headh)); z-index: 19;
  height: 34px; background: #191919; border-bottom: 2px solid #3a3a3a;
  cursor: default; text-transform: none; letter-spacing: 0; padding: 4px 8px;
  overflow: visible; }
thead tr.filters th:hover { color: #9ab; }
thead tr.filters input { width: 100%; box-sizing: border-box; background: #141414;
  border: 1px solid #3a3a3a; border-radius: 6px; color: #ddd; padding: 3px 7px;
  font: inherit; font-size: .85em; }
thead tr.filters input:focus { outline: none; border-color: #5a8; }
tbody td { padding: 8px 10px; border-bottom: 1px solid #262626; font-size: .92em;
  vertical-align: top; }
tbody tr { cursor: pointer; }
tbody tr:hover { background: #1a1a1a; }
td.id { color: #889; font-family: Consolas, monospace; font-size: .85em;
  overflow: hidden; text-overflow: ellipsis; }
td.title a { color: #9ecbff; text-decoration: none; font-weight: 600; }
td.title a:hover { text-decoration: underline; }
td.tags, td.desc { white-space: normal; overflow-wrap: anywhere; }
td.desc { color: #bbb; }
.orphbadge { display: inline-block; margin-left: 6px; padding: 0 7px; border-radius: 999px;
  background: #3a2a4a; color: #d9b8ff; font-size: .72em; vertical-align: 1px; }
.chip { display: inline-block; margin: 1px 4px 1px 0; padding: 0 8px; border-radius: 999px;
  background: #22343a; color: #9fd8d8; font-size: .82em; white-space: nowrap; }
.empty { color: #555; }
</style></head><body>
<div class="top">
  <h1>Offline teases</h1>
  <input id="fq" type="search" autocomplete="off"
         placeholder="filter: any word, any column (e.g. bondage anal)">
  <button id="fclear" title="clear all filters">clear</button>
  <span class="cnt" id="cnt"></span>
</div>
<table>
  <colgroup>
    <col style="width:92px"><col style="width:26%"><col style="width:150px">
    <col style="width:22%"><col>
  </colgroup>
  <thead>
    <tr class="head">
      <th data-col="id">id</th>
      <th data-col="title">title</th>
      <th data-col="author">author</th>
      <th data-col="tags">tags</th>
      <th data-col="description">description</th>
    </tr>
    <tr class="filters">
      <th><input data-col="id" placeholder="filter..."></th>
      <th><input data-col="title" placeholder="filter..."></th>
      <th><input data-col="author" placeholder="filter..."></th>
      <th><input data-col="tags" placeholder="filter..."></th>
      <th><input data-col="description" placeholder="filter..."></th>
    </tr>
  </thead>
  <tbody id="tb"></tbody>
</table>
<script>
var DATA = __ITEMS_JSON__;
var COLS = ["id", "title", "author", "tags", "description"];
var state = { col: "id", dir: 1, q: "", cf: { id: "", title: "", author: "", tags: "", description: "" } };
var tb = document.getElementById("tb");
var cntEl = document.getElementById("cnt");
var fq = document.getElementById("fq");
var ths = {};

function low(v) { return String(v == null ? "" : v).toLowerCase(); }

function words(s) {
  return s.split(/\s+/).filter(function (w) { return w.length > 0; });
}

function matches(it) {
  if (state.q) {
    var hay = low([it.id, it.title, it.author, it.tags, it.description].join(" "));
    var ok = words(state.q).some(function (w) { return hay.indexOf(w) !== -1; });
    if (!ok) return false;
  }
  for (var i = 0; i < COLS.length; i++) {
    var f = state.cf[COLS[i]];
    if (f && low(it[COLS[i]]).indexOf(f) === -1) return false;
  }
  return true;
}

function key(it, col) {
  if (col === "id") {
    var n = parseInt(it.id, 10);
    return isNaN(n) ? null : n;
  }
  var v = low(it[col]);
  return v === "" ? null : v;
}

function cmp(a, b) {
  var ka = key(a, state.col), kb = key(b, state.col);
  if (ka === null && kb === null) return 0;
  if (ka === null) return 1;
  if (kb === null) return -1;
  if (ka < kb) return -state.dir;
  if (ka > kb) return state.dir;
  return 0;
}

function mkCell(cls, text) {
  var td = document.createElement("td");
  if (cls) td.className = cls;
  td.textContent = text;
  return td;
}

function mkEmpty() {
  var s = document.createElement("span");
  s.className = "empty";
  s.textContent = "\u2014";
  return s;
}

function buildRow(it) {
  var tr = document.createElement("tr");

  tr.appendChild(mkCell("id", it.id == null ? "" : String(it.id)));

  var tdT = document.createElement("td");
  tdT.className = "title";
  var a = document.createElement("a");
  a.href = it.link;
  a.textContent = it.title;
  tdT.appendChild(a);
  if (it.orphan) {
    var b = document.createElement("span");
    b.className = "orphbadge";
    b.textContent = "ORPHANED";
    tdT.appendChild(b);
  }
  tr.appendChild(tdT);

  var tdA = document.createElement("td");
  if (it.author) tdA.textContent = it.author; else tdA.appendChild(mkEmpty());
  tr.appendChild(tdA);

  var tdG = document.createElement("td");
  tdG.className = "tags";
  var tw = words(it.tags == null ? "" : String(it.tags));
  if (tw.length) {
    for (var i = 0; i < tw.length; i++) {
      var c = document.createElement("span");
      c.className = "chip";
      c.textContent = tw[i];
      tdG.appendChild(c);
    }
  } else {
    tdG.appendChild(mkEmpty());
  }
  tr.appendChild(tdG);

  var tdD = document.createElement("td");
  tdD.className = "desc";
  if (it.description) tdD.textContent = it.description; else tdD.appendChild(mkEmpty());
  tr.appendChild(tdD);

  tr.addEventListener("click", function (e) {
    if (e.target.closest("a")) return;
    if (String(window.getSelection())) return;
    location.href = it.link;
  });
  return tr;
}

var rows = DATA.map(function (it) { return { it: it, tr: buildRow(it) }; });

function setSort(col) {
  if (state.col === col) state.dir = -state.dir;
  else { state.col = col; state.dir = 1; }
  apply();
}

function apply() {
  var shown = 0;
  for (var i = 0; i < rows.length; i++) {
    var m = matches(rows[i].it);
    rows[i].tr.style.display = m ? "" : "none";
    if (m) shown++;
  }
  var sorted = rows.slice().sort(function (a, b) { return cmp(a.it, b.it); });
  for (var j = 0; j < sorted.length; j++) tb.appendChild(sorted[j].tr);
  for (var c in ths) {
    ths[c].textContent = c + (c === state.col ? (state.dir === 1 ? "   \u25B2" : "   \u25BC") : "");
  }
  cntEl.textContent = "shown " + shown + " / " + rows.length;
}

var headThs = document.querySelectorAll("thead tr.head th");
for (var i = 0; i < headThs.length; i++) {
  var th = headThs[i];
  ths[th.dataset.col] = th;
  th.addEventListener("click", (function (col) {
    return function () { setSort(col); };
  })(th.dataset.col));
}
var finps = document.querySelectorAll("thead tr.filters input");
for (var k = 0; k < finps.length; k++) {
  (function (inp) {
    inp.addEventListener("input", function () {
      state.cf[inp.dataset.col] = inp.value.toLowerCase();
      apply();
    });
  })(finps[k]);
}
function globalChanged() {
  state.q = fq.value.toLowerCase();
  apply();
}
fq.addEventListener("input", globalChanged);
fq.addEventListener("search", globalChanged);
document.getElementById("fclear").addEventListener("click", function () {
  fq.value = "";
  state.q = "";
  for (var i = 0; i < finps.length; i++) {
    finps[i].value = "";
    state.cf[finps[i].dataset.col] = "";
  }
  apply();
});

if (!rows.length) {
  var tr0 = document.createElement("tr");
  var td0 = document.createElement("td");
  td0.colSpan = 5;
  td0.className = "empty";
  td0.textContent = "(no teases found \u2014 add a folder under teases/)";
  tr0.appendChild(td0);
  tb.appendChild(tr0);
} else {
  apply();
}
</script>
</body></html>
"""


def landing_html():
    """The landing page: one row per tease in teases/, using each folder's
    tease-meta.json (id / title / author / tags / description; tolerant of broken
    or missing fields). The data is embedded as an escaped JSON array and the page
    renders a sortable/filterable table (all client-side, no extra requests)."""
    items = []
    teases_dir = os.path.join(ROOT, "teases")
    if os.path.isdir(teases_dir):
        for name in sorted(os.listdir(teases_dir)):
            folder = os.path.join(teases_dir, name)
            if not os.path.isdir(folder):
                continue
            it = {"id": "", "title": name, "author": "", "tags": "", "description": ""}
            try:
                with open(os.path.join(folder, "tease-meta.json"), encoding="utf-8") as f:
                    meta = json.load(f)
                if meta.get("id") is not None:
                    it["id"] = meta["id"]
                if meta.get("title"):
                    it["title"] = meta["title"]
                it["author"] = meta.get("author") or ""
                it["tags"] = meta.get("tags") or ""
                it["description"] = meta.get("description") or ""
            except Exception:
                pass
            it["orphan"] = str(it["title"]).strip().endswith("(ORPHANED)")
            it["link"] = "/player/?tease=" + quote("../teases/" + name, safe="/")
            items.append(it)
    data = json.dumps(items, ensure_ascii=False)
    # make the embedded JSON safe inside a <script> block:
    #  </  ->  <\/   (cannot close the script tag)   <!--  ->  <\!--   (comment quirk)
    # and escape U+2028 / U+2029 (valid JSON, but line terminators for old JS parsers)
    data = (data.replace("</", "<\\/")
                .replace("<!--", "<\\!--")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))
    return _LANDING.replace("__ITEMS_JSON__", data)


class Handler(SimpleHTTPRequestHandler):
    extensions_map = dict(SimpleHTTPRequestHandler.extensions_map)
    extensions_map.update({".woff2": "font/woff2", ".woff": "font/woff"})

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/teases", "/teases/"):
            return self.send_landing()

        parts = [p for p in unquote(path).strip("/").split("/") if p]

        # /teases/<name>/ -> straight to the player with the right tease param
        if len(parts) == 2 and parts[0] == "teases":
            self.send_response(302)
            self.send_header("Location", "/player/?tease=" + quote("../teases/" + parts[1], safe="/"))
            self.end_headers()
            return None

        # media fallback chain: requested tier -> original -> xl -> l -> m -> s
        if len(parts) >= 3 and parts[-3] == "timg" and parts[-2].startswith("tb_"):
            def _exists(ps):
                return os.path.isfile(os.path.join(ROOT, *ps))

            if not _exists(parts):
                for cand in (parts[:-2] + [parts[-1]],
                             parts[:-2] + ["tb_xl", parts[-1]],
                             parts[:-2] + ["tb_l", parts[-1]],
                             parts[:-2] + ["tb_m", parts[-1]],
                             parts[:-2] + ["tb_s", parts[-1]]):
                    if cand != parts and _exists(cand):
                        self.path = "/" + "/".join(quote(p) for p in cand)
                        break
        return super().send_head()

    def do_POST(self):
        # write access is allowed ONLY under /state/ (player + tease state files)
        path = self.path.split("?", 1)[0]
        if not path.startswith("/state/"):
            self.send_error(405, "Only POST /state/... is allowed")
            return
        target = safe_state_path(unquote(path[len("/state/"):]))
        if target is None:
            self.send_error(403, "Invalid state path")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 5 * 1024 * 1024:
            self.send_error(413, "Payload too large")
            return
        data = self.rfile.read(length)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_landing(self):
        body = landing_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return None

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    port = PORT
    httpd = None
    for _ in range(11):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=ROOT))
            break
        except OSError:
            port += 1
    if httpd is None:
        print("Could not bind any port %d..%d" % (PORT, PORT + 10))
        return

    url = "http://localhost:%d/" % port
    print("Offline teases server:  " + url)
    print("Serving: " + ROOT)
    print("Press Ctrl+C to stop.")
    if os.environ.get("OPEN_BROWSER") == "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
