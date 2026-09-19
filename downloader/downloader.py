#!/usr/bin/env python3
# Offline tease downloader (stdlib only - no pip packages).
# Spec: DOWNLOADER-SPEC.md (project root).
# Architecture: a clipboard watch dog. The site parsers live in parsers.py
# (THE patch point for markup changes); all knowledge lives in knowledge.json
# (meta pairs, simple-page records, stubs). Teases are only ever written from
# that knowledge; media is pulled later by R.
#
# Self-containment hard rule: everything this tool touches lives inside the
# offline/ folder - its own venv (downloader/venv), knowledge.json, tease data.
# Nothing in %TEMP%/%APPDATA%/registry.
import fnmatch
import json
import os
import random
import re
import shutil
import sys
import time
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import parsers

try:
    import ctypes          # Windows clipboard (the whole tool runs on it)
except ImportError:
    ctypes = None

try:
    import msvcrt           # console key polling (main loop / S mode)
except ImportError:
    msvcrt = None

try:
    import winsound         # warning beeps (Windows)
except ImportError:
    winsound = None

# ---------------------------------------------------------------- paths / config
HERE = os.path.dirname(os.path.abspath(__file__))       # offline/downloader
BASE = os.path.dirname(HERE)                            # offline/
TEASES = os.path.join(BASE, "teases")
Q_DEFAULTS = os.path.join(HERE, "q_defaults.json")      # remembered session defaults
DB_PATH = os.path.join(HERE, "knowledge.json")          # the knowledge DB (meta + pages + stubs)
PLACEHOLDER = {"title": "Unknown", "author": "Unknown",
               "tags": "no-tags", "description": "no-description"}
PLACEHOLDERS = set(PLACEHOLDER.values())

MEDIA = "https://media.milovana.com/timg/"
PAGE_URL = "https://milovana.com/webteases/showtease.php?id={id}"
SCRIPT_URL = "https://milovana.com/webteases/geteosscript.php?id={id}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Pacing is expressed as FILES PER SECOND (DOWNLOAD_RATE in start-downloader.bat).
# Internally converted to a per-request delay. Default 1.0 = one file per second.
_rate_env = os.environ.get("DOWNLOAD_RATE")
_delay_env = os.environ.get("DOWNLOAD_DELAY")  # legacy fallback (seconds per file)
if _rate_env:
    RATE = max(0.01, float(_rate_env))
elif _delay_env:
    RATE = 1.0 / max(0.01, float(_delay_env))
else:
    RATE = 1.0
DELAY = 1.0 / RATE  # seconds between web requests
RETRIES = 3
MAX_CONSECUTIVE_FAILS = 20

TIER_ORDER = ["original", "xl", "l", "m", "s"]
TIER_PIXELS = {"original": "full resolution", "xl": "720x1080 (site default)",
               "l": "480x720", "m": "133x200", "s": "66x100"}
TIER_CHAIN_DOWN = {"xl": ["xl", "l", "m", "s", "original"],
                   "l": ["l", "m", "s", "original"],
                   "m": ["m", "s", "original"],
                   "s": ["s", "original"],
                   "original": ["original"]}
EXT_BY_TYPE = {"image/jpeg": "jpg", "audio/mpeg": "mp3"}

# ---------------------------------------------------------------- tiny console


def out(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        # legacy console codepage cannot render some glyphs - degrade, never crash
        try:
            print(str(msg).encode("ascii", "replace").decode(), flush=True)
        except OSError:
            pass
    except OSError:
        pass  # stdout closed (e.g. piped to a command that exited) - never crash


def ask(prompt):
    return input(prompt).strip()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def sanitize(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name or "tease"


# ---------------------------------------------------------------- http helpers


def http(url, headers=None, timeout=30):
    req = Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urlopen(req, timeout=timeout)


def fetch_bytes(url):
    """GET full body (used for probes). Returns (True, data) or (False, note)."""
    try:
        with http(url) as r:
            data = r.read()
        time.sleep(DELAY)
        return True, data
    except HTTPError as e:
        time.sleep(DELAY)
        return False, "http %d" % e.code
    except (URLError, OSError) as e:
        time.sleep(DELAY)
        return False, str(e)[:80]


def download_to(url, target, expected_size=None):
    """Download url -> target (.part then atomic rename). (ok, bytes, note)."""
    part = target + ".part"
    d = os.path.dirname(target)
    if d:
        os.makedirs(d, exist_ok=True)
    note = ""
    for attempt in range(1, RETRIES + 1):
        try:
            with http(url) as r:
                data = r.read()
            with open(part, "wb") as f:
                f.write(data)
            if expected_size is not None and len(data) != expected_size:
                os.remove(part)
                note = "size mismatch (%d vs %d expected)" % (len(data), expected_size)
                time.sleep(DELAY)
                continue
            os.replace(part, target)
            time.sleep(DELAY)
            return True, len(data), ""
        except HTTPError as e:
            if os.path.exists(part):
                os.remove(part)
            if e.code in (403, 404):
                time.sleep(DELAY)
                return False, 0, "http %d" % e.code
            note = "http %d" % e.code
            time.sleep(DELAY)
        except (URLError, OSError) as e:
            if os.path.exists(part):
                os.remove(part)
            note = str(e)[:80]
            time.sleep(DELAY)
    return False, 0, note


# ---------------------------------------------------------------- file checks


def jpeg_ok(path):
    try:
        if os.path.getsize(path) < 1024:
            return False
        with open(path, "rb") as f:
            head = f.read(2)
            f.seek(-2, os.SEEK_END)
            tail = f.read(2)
        return head == b"\xff\xd8" and tail == b"\xff\xd9"
    except OSError:
        return False


def audio_ok(path):
    try:
        if os.path.getsize(path) < 1000:
            return False
        with open(path, "rb") as f:
            head = f.read(4)
        return head[:3] == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    except OSError:
        return False


def verify_file(path, item, quality):
    """True if the existing file is acceptable for this item+quality."""
    if not os.path.isfile(path):
        return False
    if item["ext"] == "jpg":
        if quality == "original" and item.get("size"):
            return os.path.getsize(path) == item["size"]
        return jpeg_ok(path)
    return audio_ok(path)


# ---------------------------------------------------------------- script scan


def scan_script(script):
    """Returns (used, all_items, requested_sizes). item: hash -> dict."""
    galleries = script.get("galleries", {})
    files = script.get("files", {})

    def add_img(dst, img):
        h = img.get("hash")
        if not h or h in dst:
            return
        dst[h] = {"size": int(img.get("size") or 0), "ext": "jpg",
                  "type": "image/jpeg", "w": img.get("width"), "h": img.get("height")}

    def add_file(dst, name, meta):
        h = meta.get("hash")
        if not h or h in dst:
            return
        t = (meta.get("type") or "").lower()
        ext = EXT_BY_TYPE.get(t)
        if not ext:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
        dst[h] = {"size": int(meta.get("size") or 0), "ext": ext, "type": t or "?", "w": None, "h": None}

    # every locator string anywhere in the pages
    locators = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and (node.startswith("gallery:") or node.startswith("file:")):
            locators.add(node)

    walk(script.get("pages", {}))

    used = {}
    for loc in sorted(locators):
        if loc.startswith("gallery:"):
            uuid, _, img = loc[len("gallery:"):].partition("/")
            gal = galleries.get(uuid)
            if not gal:
                continue
            for im in gal.get("images", []):
                if img in ("*", "") or str(im.get("id")) == img:
                    add_img(used, im)
        elif loc.startswith("file:"):
            pat = loc[len("file:"):]
            for name, meta in files.items():
                if fnmatch.fnmatch(name, pat):
                    add_file(used, name, meta)

    all_items = {}
    for gal in galleries.values():
        for im in gal.get("images", []):
            add_img(all_items, im)
    for name, meta in files.items():
        add_file(all_items, name, meta)

    # image sizes the script actually requests (informational)
    sizes_req = {}

    def walk_cmds(node):
        if isinstance(node, dict):
            if "image" in node and isinstance(node["image"], dict):
                s = node["image"].get("size") or "xl"
                sizes_req[s] = sizes_req.get(s, 0) + 1
            for v in node.values():
                walk_cmds(v)
        elif isinstance(node, list):
            for v in node:
                walk_cmds(v)

    walk_cmds(script.get("pages", {}))
    return used, all_items, sizes_req


def build_targets(items, quality, timg_dir):
    """item list -> [(url, target_path, item, is_tier)]"""
    targets = []
    for h, it in sorted(items.items()):
        tier = quality if (it["ext"] == "jpg" and quality != "original") else None
        rel = os.path.join("tb_%s" % tier, "%s.%s" % (h, it["ext"])) if tier \
            else "%s.%s" % (h, it["ext"])
        url = MEDIA + (("tb_%s/" % tier) if tier else "") + "%s.%s" % (h, it["ext"])
        targets.append((url, os.path.join(timg_dir, rel), it, tier is not None))
    return targets


# ---------------------------------------------------------------- wizard text


_OPENED_URLS = set()


def open_in_browser(url):
    """Open a URL once per run in the default browser (best effort).
    Set DOWNLOADER_NO_BROWSER=1 to suppress (used by tests)."""
    if url in _OPENED_URLS:
        return
    _OPENED_URLS.add(url)
    if os.environ.get("DOWNLOADER_NO_BROWSER"):
        out("  -> (auto-open disabled: DOWNLOADER_NO_BROWSER)")
        return
    try:
        out("  -> opening in your browser: %s" % url)
        webbrowser.open(url)
    except Exception as e:
        out("  -> (could not open the browser automatically: %s)" % e)


def find_existing(tease_id):
    if not os.path.isdir(TEASES):
        return None
    for name in sorted(os.listdir(TEASES)):
        folder = os.path.join(TEASES, name)
        meta_p = os.path.join(folder, "tease-meta.json")
        if os.path.isdir(folder) and os.path.isfile(meta_p):
            try:
                with open(meta_p, encoding="utf-8") as f:
                    if str(json.load(f).get("id")) == str(tease_id):
                        return folder
            except Exception:
                pass
    return None


def list_existing():
    out("")
    if not os.path.isdir(TEASES):
        out("  (no teases folder yet)")
        return
    found = False
    for name in sorted(os.listdir(TEASES)):
        folder = os.path.join(TEASES, name)
        meta_p = os.path.join(folder, "tease-meta.json")
        if os.path.isdir(folder) and os.path.isfile(meta_p):
            try:
                with open(meta_p, encoding="utf-8") as f:
                    m = json.load(f)
                out("  - %s   (id %s)" % (os.path.basename(folder), m.get("id")))
                found = True
            except Exception:
                pass
    if not found:
        out("  (no teases found)")


def parse_info(path):
    """Read quality+scope recorded in a tease's info.txt. (q, s) or (None, None)."""
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"quality: (\w+), scope: ([\w-]+)", f.read())
        if m:
            return m.group(1), m.group(2)
    except Exception:
        pass
    return None, None


def load_unavailable(folder):
    """File names the media host refuses (403/404 on every rendition) - listed in
    unavailable.txt so repair runs skip them until the user deletes lines."""
    names = set()
    try:
        with open(os.path.join(folder, "unavailable.txt"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.add(line)
    except OSError:
        pass
    return names


def append_unavailable(folder, names):
    if not names:
        return
    p = os.path.join(folder, "unavailable.txt")
    header_needed = not os.path.isfile(p)
    with open(p, "a", encoding="utf-8") as f:
        if header_needed:
            f.write("# Files the media host refuses (403/404 on every rendition) - likely\n"
                    "# removed from the site. Delete a line (or this file) to retry them.\n")
        for n in sorted(names):
            f.write(n + "\n")


def clean_line(v):
    """Meta values are single-line strings: collapse any whitespace run (pasted
    newlines, tabs) into one space and trim the ends. JSON escaping itself is
    handled by json.dump on EVERY meta write - quotes, backslashes and unicode
    can never break the file."""
    return re.sub(r"\s+", " ", v).strip()


def write_info(path, tease_id, title, author, quality, scope, phase, counts, fallbacks=0, failures=0):
    with open(path, "w", encoding="utf-8") as f:
        f.write("Tease %s - %s\n" % (tease_id, title))
        f.write("Author: %s\n\n" % (author or "?"))
        f.write("Original page:  %s\n" % PAGE_URL.format(id=tease_id))
        f.write("Script source:  %s\n\n" % SCRIPT_URL.format(id=tease_id))
        f.write("Downloaded with: offline/downloader (quality: %s, scope: %s)\n" % (quality, scope))
        f.write("Date: %s\n" % time.strftime("%Y-%m-%d %H:%M"))
        f.write("Status: %s\n" % phase)
        if counts:
            f.write("Files: %s\n" % counts)
        if fallbacks:
            f.write("Note: %d file(s) used a lower quality rendition (not available at chosen tier)\n" % fallbacks)
        if failures:
            f.write("Note: %d file(s) FAILED - see failed.txt, rerun to retry\n" % failures)
        f.write("\ntease.json      = the tease script\n")
        f.write("tease-meta.json = id / title / author / tags / description (landing page + player UI)\n")
        f.write("timg\\           = all media of this tease\n")


def run_pipeline(folder, script, tease_id, title, author, quality, scope,
                 prev_quality=None, interactive=False):
    """Layout + preflight verify + download + post-verify + info.txt.
    Used by the interactive wizard AND by repair-all (R). Returns a stats dict."""
    timg = os.path.join(folder, "timg")
    os.makedirs(timg, exist_ok=True)
    used, all_items, _sizes = scan_script(script)
    items = used if scope == "used-only" else all_items
    targets = build_targets(items, quality, timg)

    unavail = load_unavailable(folder)
    to_fetch, present, suspicious, known_gone = [], 0, 0, 0
    for url, path, it, is_tier in targets:
        if os.path.isfile(path):
            if verify_file(path, it, quality):
                present += 1
            else:
                suspicious += 1
                to_fetch.append((url, path, it, is_tier))
        elif os.path.basename(path) in unavail:
            known_gone += 1
        else:
            to_fetch.append((url, path, it, is_tier))

    if interactive:
        out("")
        out("=== Step 6/8: layout + pre-flight verify ===")
        out("  folder: %s" % folder)
    out("  %d present-ok | %d missing | %d suspicious -> will fetch %d%s" %
        (present, len(to_fetch) - suspicious, suspicious, len(to_fetch),
         ("  [%d known-unavailable]" % known_gone) if known_gone else ""))

    if interactive and prev_quality and prev_quality != quality and os.path.isdir(timg):
        ans = ask('Existing files use quality "%s". New choice: "%s".  '
                  '(K)eep old files too / (R)emove files that do not match the new choice [k]: '
                  % (prev_quality, quality))
        if ans.lower() == "r":
            keep = {p for _, p, _, _ in targets}
            removed = 0
            for dirpath, _, files in os.walk(timg):
                for fn in files:
                    p = os.path.join(dirpath, fn)
                    if p not in keep:
                        try:
                            os.remove(p)
                            removed += 1
                        except OSError:
                            pass
            out("  -> removed %d file(s) that did not match the new choice" % removed)

    info_p = os.path.join(folder, "info.txt")
    write_info(info_p, tease_id, title, author, quality, scope, "IN PROGRESS",
               "%d/%d files" % (present, len(targets)))

    if interactive:
        out("")
        out("=== Step 7/8: downloading ===")
    if not to_fetch:
        out("  nothing to download - everything is already present.")
    done = 0
    fails = []
    newly_gone = set()
    fallbacks = 0
    consecutive = 0
    total = len(to_fetch)
    t0 = time.time()
    for i, (url, path, it, is_tier) in enumerate(to_fetch, 1):
        if consecutive >= MAX_CONSECUTIVE_FAILS:
            out("")
            out("  !! %d consecutive failures - stopping (possible soft-block OR a batch of" % consecutive)
            out("  !! files that no longer exist on the site). Check your VPN / wait a bit,")
            out("  !! then rerun - progress is saved and dead files get marked in unavailable.txt.")
            break
        h = os.path.basename(path).rsplit(".", 1)[0]
        first = re.search(r"tb_(\w+)/", url).group(1) if is_tier else "original"
        chain = TIER_CHAIN_DOWN[first]
        ok = False
        note = ""
        used_tier = None
        attempt_notes = []
        for tier in chain:
            if tier == "original":
                u = MEDIA + "%s.%s" % (h, it["ext"])
                expected = it["size"] if (it["ext"] == "jpg" and it["size"]) else None
            else:
                u = MEDIA + "tb_%s/%s.%s" % (tier, h, it["ext"])
                expected = None
            ok, nbytes, note = download_to(u, path, expected)
            attempt_notes.append(note)
            if ok and it["ext"] == "jpg" and not jpeg_ok(path):
                ok, note = False, "invalid jpeg"
                attempt_notes[-1] = note
                try:
                    os.remove(path)
                except OSError:
                    pass
            if ok:
                used_tier = tier
                break
        if ok:
            done += 1
            consecutive = 0
            if is_tier and used_tier != first:
                fallbacks += 1
        else:
            consecutive += 1
            base = os.path.basename(path)
            if attempt_notes and all(x in ("http 403", "http 404") for x in attempt_notes):
                newly_gone.add(base)   # media host refuses this file on every rendition
            else:
                fails.append((base, note))
        elapsed = max(time.time() - t0, 0.001)
        eta = (total - i) * (elapsed / i)
        try:
            sys.stdout.write("\r  [%d/%d]  elapsed %4ds  ETA %4ds  fails: %-3d " %
                             (i, total, int(elapsed), int(eta), len(fails)))
            sys.stdout.flush()
        except OSError:
            pass  # stdout closed - keep downloading
    if total:
        out("")
    if newly_gone:
        append_unavailable(folder, newly_gone)
    out("  downloaded: %d | already present: %d | failed: %d | fallback-quality files: %d%s" %
        (done, present, len(fails), fallbacks,
         ("  [+%d marked unavailable]" % len(newly_gone)) if newly_gone else ""))

    if interactive:
        out("")
        out("=== Step 8/8: post-verify + finalize ===")
    gone_set = unavail | newly_gone
    ok_count, bad_count = 0, 0
    for url, path, it, is_tier in targets:
        if verify_file(path, it, quality):
            ok_count += 1
        elif os.path.basename(path) in gone_set and not os.path.isfile(path):
            continue  # known-unavailable (deleted on the site) - not a failure
        else:
            bad_count += 1
    out("  verified: %d ok | %d missing/bad | %d known-unavailable" %
        (ok_count, bad_count, len(gone_set)))
    failed_p = os.path.join(folder, "failed.txt")
    if fails:
        with open(failed_p, "w", encoding="utf-8") as f:
            for name, note in fails:
                f.write("%s  %s\n" % (name, note))
        out("  failures written to failed.txt - just rerun to retry them")
    elif os.path.isfile(failed_p):
        os.remove(failed_p)
    total_bytes = sum(os.path.getsize(p) for _, p, _, _ in targets if os.path.isfile(p))
    counts = "%d/%d files ok, %s on disk" % (ok_count, len(targets), human(total_bytes))
    if gone_set:
        counts += ", %d known-unavailable (see unavailable.txt)" % len(gone_set)
    write_info(info_p, tease_id, title, author, quality, scope,
               "COMPLETE" if bad_count == 0 else "INCOMPLETE",
               counts, fallbacks, bad_count)
    return {"present": present, "total": len(targets), "downloaded": done,
            "failed": len(fails), "fallbacks": fallbacks, "ok": ok_count,
            "bad": bad_count, "gone": len(gone_set), "bytes": total_bytes}


def repair_all(db=None):
    """R: refresh every tease's meta from the knowledge DB, then verify/download
    its files using the quality+scope it recorded. Never prompts."""
    if db is None:
        db = db_load()
    out("")
    out("=== REPAIR ALL: meta refresh from DB + file check (settings from each info.txt) ===")
    if not os.path.isdir(TEASES):
        out("  (no teases folder)")
        return
    tot = {"teases": 0, "skipped": 0, "downloaded": 0, "failed": 0,
           "bad": 0, "meta_refreshed": 0}
    for name in sorted(os.listdir(TEASES)):
        folder = os.path.join(TEASES, name)
        if not os.path.isdir(folder):
            continue
        meta_p = os.path.join(folder, "tease-meta.json")
        script_p = os.path.join(folder, "tease.json")
        info_p = os.path.join(folder, "info.txt")
        if not (os.path.isfile(meta_p) and os.path.isfile(script_p)):
            out("  skip (no meta/script): %s" % name)
            tot["skipped"] += 1
            continue
        quality, scope = parse_info(info_p)
        if not quality or not scope:
            out("  skip (no quality/scope in info.txt - run an Update once): %s" % name)
            tot["skipped"] += 1
            continue
        try:
            with open(meta_p, encoding="utf-8") as f:
                meta = json.load(f)
            with open(script_p, encoding="utf-8") as f:
                script = json.load(f)
        except Exception as e:
            out("  skip (unreadable): %s (%s)" % (name, e))
            tot["skipped"] += 1
            continue
        tot["teases"] += 1
        out("")
        out("--- %s  (quality=%s, scope=%s)" % (name, quality, scope))
        # DB -> tease meta refresh (real DB values win; placeholders never overwrite)
        entry = (db.get("teases") or {}).get(str(meta.get("id", "")).strip())
        if entry:
            changed = []
            for key in ("title", "author", "tags", "description"):
                v = str(entry.get(key) or "").strip()
                if v and v not in PLACEHOLDERS and str(meta.get(key) or "").strip() != v:
                    meta[key] = v
                    changed.append(key)
            if changed:
                with open(meta_p, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=1)
                out("  meta refreshed from DB: %s" % ", ".join(changed))
                tot["meta_refreshed"] += len(changed)
        new_folder, was = _sync_folder(str(meta.get("id", "")), meta.get("title"), meta.get("author"))
        if new_folder and was:
            out('  folder renamed: "%s" -> "%s"' % (was, os.path.basename(new_folder)))
            folder = new_folder
            meta_p = os.path.join(folder, "tease-meta.json")
            script_p = os.path.join(folder, "tease.json")
            info_p = os.path.join(folder, "info.txt")
        stats = run_pipeline(folder, script, str(meta.get("id", "?")),
                             meta.get("title", ""), meta.get("author", ""),
                             quality, scope, prev_quality=None, interactive=False)
        tot["downloaded"] += stats["downloaded"]
        tot["failed"] += stats["failed"]
        tot["bad"] += stats["bad"]
        if stats["bad"] == 0:
            out("  OK - %d/%d files verified%s" %
                (stats["ok"], stats["total"],
                 (", %d known-unavailable" % stats.get("gone", 0)) if stats.get("gone") else ""))
        else:
            out("  PARTIAL - %d missing/bad (run R again to retry)" % stats["bad"])
    done_line = ("REPAIR DONE. teases: %d | skipped: %d | downloaded: %d | still failed: %d" %
                 (tot["teases"], tot["skipped"], tot["downloaded"], tot["failed"]))
    if tot["meta_refreshed"]:
        done_line += " | meta refreshed from DB: %d field(s)" % tot["meta_refreshed"]
    out("")
    out(done_line)
    if tot["failed"] or tot["bad"]:
        beep()
    if tot["failed"]:
        out("Failures are listed in each tease's failed.txt - just run R again.")


# ---------------------------------------------------------------- orphan extractor


def _strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _page_snippet(page_commands, limit=140):
    """First visible text of a page (for reports)."""
    found = []

    def walk(n):
        if found:
            return
        if isinstance(n, dict):
            for k, v in n.items():
                if k in ("label", "text") and isinstance(v, str) and v.strip():
                    t = _strip_html(v)
                    if t:
                        found.append(t[:limit])
                        return
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(page_commands)
    return found[0] if found else ""


def _page_targets(page_commands):
    """All goto targets inside a page's commands (recursive)."""
    targets = []

    def walk(n):
        if isinstance(n, dict):
            got = n.get("goto")
            if isinstance(got, dict) and isinstance(got.get("target"), str):
                targets.append(got["target"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(page_commands)
    return targets


def _page_locators(page_commands):
    locs = set()

    def walk(n):
        if isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
        elif isinstance(n, str) and (n.startswith("gallery:") or n.startswith("file:")):
            locs.add(n)

    walk(page_commands)
    return locs


def _resolve_locator(loc, galleries, files):
    """locator -> ([media item dicts], dangling?)."""
    items = []
    if loc.startswith("gallery:"):
        uuid, _, img = loc[len("gallery:"):].partition("/")
        gal = galleries.get(uuid)
        if not gal:
            return items, True
        imgs = gal.get("images", [])
        chosen = imgs if img in ("*", "") else [im for im in imgs if str(im.get("id")) == img]
        for im in chosen:
            if not im.get("hash"):
                continue
            items.append({"hash": im["hash"], "ext": "jpg", "type": "image/jpeg",
                          "name": "%s/#%s" % (gal.get("name", uuid[:8]), im.get("id")),
                          "size": int(im.get("size") or 0),
                          "locator": "gallery:%s/%s" % (uuid, im.get("id"))})
        return items, not chosen
    if loc.startswith("file:"):
        pat = loc[len("file:"):]
        names = [n for n in files.keys() if fnmatch.fnmatch(n, pat)]
        for n in names:
            meta = files[n]
            if not meta.get("hash"):
                continue
            t = (meta.get("type") or "").lower()
            ext = EXT_BY_TYPE.get(t) or (n.rsplit(".", 1)[-1].lower() if "." in n else "bin")
            items.append({"hash": meta["hash"], "ext": ext, "type": t or "?",
                          "name": n, "size": int(meta.get("size") or 0),
                          "locator": "file:%s" % n})
        return items, not names
    return items, True


def analyze_orphans(script):
    """Static reachability (literal + glob edges only; dynamic $expr targets are
    unresolvable and treated as orphaned per project policy) + media buckets."""
    pages = script.get("pages", {})
    galleries = script.get("galleries", {})
    files = script.get("files", {})
    names = list(pages.keys())
    name_set = set(names)

    edges = {}
    dynamic, dangling_targets = [], []
    for p, cmds in pages.items():
        edges[p] = set()
        for t in _page_targets(cmds):
            if t.startswith("$"):
                dynamic.append((p, t))
                continue
            neg = t.startswith("!")
            pat = t[1:] if neg else t
            if any(ch in pat for ch in "*?["):
                matched = {n for n in names if fnmatch.fnmatch(n, pat)}
                if neg:
                    matched = name_set - matched
                if not matched:
                    dangling_targets.append((p, t))
                edges[p] |= matched
            elif t in name_set:
                edges[p].add(t)
            else:
                dangling_targets.append((p, t))

    reachable = set()
    if "start" in name_set:
        stack = ["start"]
        while stack:
            cur = stack.pop()
            if cur in reachable:
                continue
            reachable.add(cur)
            stack.extend(edges.get(cur, ()) - reachable)
    orphan_pages = [n for n in names if n not in reachable]

    # media per page
    page_media = {}
    dangling_locators = []
    for p, cmds in pages.items():
        items = {}
        for loc in sorted(_page_locators(cmds)):
            got, bad = _resolve_locator(loc, galleries, files)
            if bad:
                dangling_locators.append((p, loc))
            for it in got:
                items.setdefault(it["hash"], it)
        page_media[p] = items

    all_items = {}
    for uuid, gal in galleries.items():
        for im in gal.get("images", []):
            h = im.get("hash")
            if h and h not in all_items:
                all_items[h] = {"hash": h, "ext": "jpg", "type": "image/jpeg",
                                "name": "%s/#%s" % (gal.get("name", uuid[:8]), im.get("id")),
                                "size": int(im.get("size") or 0),
                                "locator": "gallery:%s/%s" % (uuid, im.get("id"))}
    for n, meta in files.items():
        h = meta.get("hash")
        if h and h not in all_items:
            t = (meta.get("type") or "").lower()
            ext = EXT_BY_TYPE.get(t) or (n.rsplit(".", 1)[-1].lower() if "." in n else "bin")
            all_items[h] = {"hash": h, "ext": ext, "type": t or "?", "name": n,
                            "size": int(meta.get("size") or 0), "locator": "file:%s" % n}

    used_reach, used_orph = {}, {}
    for p, items in page_media.items():
        tgt = used_reach if p in reachable else used_orph
        for h, it in items.items():
            tgt.setdefault(h, it)

    media = []
    for h, it in all_items.items():
        if h in used_reach or h in used_orph:
            continue
        it["bucket"] = "never referenced"
        media.append(it)
    for h, it in used_orph.items():
        it["bucket"] = ("shared (also shown by reachable pages)" if h in used_reach
                        else "referenced only by orphan pages")
        media.append(it)

    return {
        "pages_total": names, "reachable": sorted(reachable),
        "orphan_pages": orphan_pages, "dynamic": dynamic,
        "dangling_targets": dangling_targets, "dangling_locators": dangling_locators,
        "media": media, "used_reach": used_reach, "all_items": all_items,
    }


def write_orphan_report(path, source_name, script, rep, copied, missing,
                        copied_n=0, missing_n=0):
    pages = script.get("pages", {})
    def mb(b):
        return human(b)
    with open(path, "w", encoding="utf-8") as f:
        f.write("ORPHANED leftovers extraction\n")
        f.write("Source: %s\nDate: %s\n\n" % (source_name, time.strftime("%Y-%m-%d %H:%M")))
        f.write("Pages: %d total | %d reachable | %d ORPHANED" %
                (len(rep["pages_total"]), len(rep["reachable"]), len(rep["orphan_pages"])))
        if rep["dynamic"]:
            f.write(" (note: %d dynamic '$' jumps exist and cannot be resolved statically -"
                    " pages reachable only via those are treated as orphaned by policy)" %
                    len(rep["dynamic"]))
        f.write("\n\n== ORPHANED PAGES ==\n")
        for p in rep["orphan_pages"]:
            cmds = pages.get(p, [])
            f.write("- %s   (%d commands)\n    %s\n" %
                    (p, len(cmds), _page_snippet(cmds) or "(no text)"))
        f.write("\n== LEFTOVER MEDIA (%d items, %s) ==\n" %
                (len(rep["media"]), mb(sum(i["size"] for i in rep["media"]))))
        for it in sorted(rep["media"], key=lambda i: (i["bucket"], i["name"])):
            f.write("- [%s] %s   %s   %s\n" %
                    (it["bucket"], it["name"], mb(it["size"]), it["hash"][:12]))
        if rep.get("normal"):
            f.write("\nNormal-media slideshow section: %d item(s) (the tease's regular\n"
                    "media in script order; not listed below)\n" % len(rep["normal"]))
        f.write("\nCopied: %d leftover (+%d normal) | not found on disk: %d (+%d normal)\n" %
                (copied, copied_n, len(missing), len(missing_n)))
        for it in missing:
            f.write("  MISSING: %s (%s)\n" % (it["name"], it["hash"][:12]))
        if rep["dangling_targets"]:
            f.write("\n== DANGLING GOTO TARGETS (point at pages that do not exist) ==\n")
            for p, t in rep["dangling_targets"]:
                f.write("- %s -> %s\n" % (p, t))
        if rep["dangling_locators"]:
            f.write("\n== DANGLING LOCATORS (point at media that does not exist) ==\n")
            for p, loc in rep["dangling_locators"]:
                f.write("- %s -> %s\n" % (p, loc))


def _copy_media(items, src_timg, dest):
    """Copy media items (first existing rendition) into dest/timg. (copied, missing)."""
    copied, missing = 0, []
    for it in items:
        src = None
        rels = ["%s.%s" % (it["hash"], it["ext"])] + \
               [os.path.join("tb_%s" % t, "%s.%s" % (it["hash"], it["ext"]))
                for t in ("xl", "l", "m", "s")]
        for rel in rels:
            p = os.path.join(src_timg, rel)
            if os.path.isfile(p):
                src = (p, rel)
                break
        if not src:
            missing.append(it)
            continue
        tp = os.path.join(dest, "timg", src[1])
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        shutil.copy2(src[0], tp)
        copied += 1
    return copied, missing


def orphan_extract():
    """O mode: pure-local extraction of everything a tease never reaches into a new
    'ORPHANED' tease folder (orphan pages + orphaned media + a normal-media slideshow)
    that plays in the player (its new 'start' page is an index; Nav can filter ORPH-
    pages). Slideshow pages advance with click/SPACE (Auto drives them hands-free)."""
    out("")
    out("=== ORPHAN EXTRACTOR (pure local - reads your downloaded teases) ===")
    if not os.path.isdir(TEASES):
        out("  (no teases folder)")
        return
    folders = []
    for name in sorted(os.listdir(TEASES)):
        fp = os.path.join(TEASES, name)
        if os.path.isdir(fp) and os.path.isfile(os.path.join(fp, "tease.json")):
            folders.append((name, fp))
    if not folders:
        out("  (no teases with a script found)")
        return
    for i, (name, _fp) in enumerate(folders, 1):
        out("  %2d) %s" % (i, name))
    chosen = None
    while chosen is None:
        s = ask("Pick a tease (number, or its id): ")
        if s.isdigit() and 1 <= int(s) <= len(folders):
            chosen = folders[int(s) - 1]
            break
        if s.isdigit():
            for name, fp in folders:
                try:
                    with open(os.path.join(fp, "tease-meta.json"), encoding="utf-8") as f:
                        if str(json.load(f).get("id")) == s:
                            chosen = (name, fp)
                            break
                except Exception:
                    pass
            if chosen:
                break
        out("  -> enter one of the listed numbers (or a tease id)")
    name, folder = chosen
    try:
        with open(os.path.join(folder, "tease.json"), encoding="utf-8") as f:
            script = json.load(f)
    except Exception as e:
        out("  cannot read tease.json: %s" % e)
        return
    meta = {}
    try:
        with open(os.path.join(folder, "tease-meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        pass

    rep = analyze_orphans(script)
    out("")
    out("  pages: %d total | %d reachable | %d ORPHANED" %
        (len(rep["pages_total"]), len(rep["reachable"]), len(rep["orphan_pages"])))
    if rep["dynamic"]:
        out("  dynamic '$' jumps: %d (unresolvable -> pages depending on them are assumed orphaned)"
            % len(rep["dynamic"]))
    out("  leftover media: %d items (%s)" %
        (len(rep["media"]), human(sum(i["size"] for i in rep["media"]))))
    if rep["dangling_targets"] or rep["dangling_locators"]:
        out("  dangling refs: %d goto target(s), %d locator(s) (details in leftovers.txt)" %
            (len(rep["dangling_targets"]), len(rep["dangling_locators"])))

    # normal media = referenced by reachable pages (skip anything already in the
    # leftover section); kept in script order for the second slideshow chain
    included = {it["hash"] for it in rep["media"]}
    normal = [it for h, it in rep["all_items"].items()
              if h in rep["used_reach"] and h not in included]
    for it in normal:
        it["bucket"] = "normal media"
    rep["normal"] = normal
    if normal:
        out("  normal media (2nd slideshow section): %d items (%s)" %
            (len(normal), human(sum(i["size"] for i in normal))))

    if not rep["orphan_pages"] and not rep["media"]:
        out("")
        out("Nothing orphaned in this tease - nothing was created. :)")
        return

    dest_name = sanitize("%s %s ORPHANED" % (meta.get("id", "?"), meta.get("title", name)))
    dest = os.path.join(TEASES, dest_name)
    if os.path.exists(dest):
        if ask('"%s" already exists. (O)verwrite / Enter to abort: ' % dest_name).lower() != "o":
            return
        shutil.rmtree(dest)
    os.makedirs(os.path.join(dest, "timg"), exist_ok=True)

    # ---- copy media (independent folder; source untouched) ----
    src_timg = os.path.join(folder, "timg")
    copied, missing = _copy_media(rep["media"], src_timg, dest)
    copied_n, missing_n = _copy_media(normal, src_timg, dest)
    out("  media copied: %d leftover + %d normal | not found on disk: %d" %
        (copied, copied_n, len(missing) + len(missing_n)))

    # ---- build the new script ----
    new_pages = {}
    title = meta.get("title", name)
    land = [{"say": {"label": "<p><b>ORPHANED leftovers</b> of &quot;%s&quot;</p>"
                               "<p>Orphaned pages: %d &middot; Leftover media: %d"
                               " &middot; Normal media: %d</p>"
                               "<p>Slideshows advance with click/SPACE - turn on"
                               " [Auto] to watch them hands-free. Nav (filter&quot;ORPH-&quot;)"
                               " roams anywhere.</p>" %
                               (title, len(rep["orphan_pages"]), len(rep["media"]),
                                len(normal)),
                     "mode": "pause"}}]
    land_opts = []
    if rep["orphan_pages"]:
        land_opts.append({"label": "Browse orphaned pages (%d)" % len(rep["orphan_pages"]),
                          "commands": [{"goto": {"target": rep["orphan_pages"][0]}}],
                          "color": "#e1bee7"})
    if rep["media"]:
        land_opts.append({"label": "\U0001F39E Slideshow: leftovers (%d)" % len(rep["media"]),
                          "commands": [{"goto": {"target": "ORPH-MEDIA-0001"}}],
                          "color": "#9fa8da"})
    if normal:
        land_opts.append({"label": "\U0001F39E Slideshow: normal media (%d)" % len(normal),
                          "commands": [{"goto": {"target": "ORPH-NORM-0001"}}],
                          "color": "#80cbc4"})
    if land_opts:
        land.append({"choice": {"options": land_opts}})
    new_pages["start"] = land
    for p in rep["orphan_pages"]:
        new_pages[p] = script["pages"][p]

    viewer = sorted(rep["media"], key=lambda i: (i["bucket"], i["name"]))

    def slideshow(prefix, items, next_after_last):
        for i, it in enumerate(items, 1):
            pid = "%s-%04d" % (prefix, i)
            body = []
            if it["type"].startswith("audio"):
                body.append({"audio.play": {"locator": it["locator"]}})
            else:
                body.append({"image": {"locator": it["locator"]}})
            body.append({"say": {"label": "<p><b>%s</b></p><p>%s &middot; %s &middot; %s</p>" %
                                         (it["name"], it.get("bucket", ""), it["type"],
                                          human(it["size"])),
                                 "mode": "pause"}})
            nxt = ("%s-%04d" % (prefix, i + 1)) if i < len(items) else next_after_last
            body.append({"goto": {"target": nxt}})
            new_pages[pid] = body

    slideshow("ORPH-MEDIA", viewer, "ORPH-NORM-0001" if normal else "start")
    slideshow("ORPH-NORM", normal, "start")

    dest_script = {
        "pages": new_pages,
        "galleries": script.get("galleries", {}),
        "files": script.get("files", {}),
        "modules": script.get("modules", {}),
        "init": script.get("init", ""),
    }
    with open(os.path.join(dest, "tease.json"), "w", encoding="utf-8") as f:
        json.dump(dest_script, f)
    # ORPHANED copies carry the SAME meta as their base tease (author, tags,
    # description, any future keys) - only the title gets the "(ORPHANED)" suffix.
    orphan_meta = dict(meta)
    orphan_meta["title"] = "%s (ORPHANED)" % title
    with open(os.path.join(dest, "tease-meta.json"), "w", encoding="utf-8") as f:
        json.dump(orphan_meta, f, indent=1)
    write_info(os.path.join(dest, "info.txt"), str(meta.get("id", "?")),
               "%s (ORPHANED)" % title, meta.get("author", ""),
               "n/a (copied from source)", "orphaned-extract", "COMPLETE",
               "%d orphan page(s), %d leftover media, %d normal media (%d+%d copied)" %
               (len(rep["orphan_pages"]), len(rep["media"]), len(normal),
                copied, copied_n))
    write_orphan_report(os.path.join(dest, "leftovers.txt"), name, script, rep,
                        copied, missing, copied_n, missing_n)
    out("")
    out('DONE. Created: teases\\%s' % dest_name)
    out("Start the server, pick it on the landing page - it opens on a new index page.")
    out('Slideshows advance with click/SPACE - switch on [Auto] to watch them hands-free;')
    out('the Nav panel (filter "ORPH-") roams anywhere.')


# ---------------------------------------------------------------- clipboard

_CLIP_API = None


def _clip_api():
    """Lazy, 64-bit-safe bindings for the Windows clipboard API. Win32 handle
    functions return pointers - without explicit restypes ctypes would truncate
    them to 32 bits and crash. Returns (user32, kernel32) or None."""
    global _CLIP_API
    if _CLIP_API is None and ctypes is not None:
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            u.OpenClipboard.argtypes = [ctypes.c_void_p]
            u.OpenClipboard.restype = ctypes.c_int
            u.CloseClipboard.restype = ctypes.c_int
            u.GetClipboardData.argtypes = [ctypes.c_uint]
            u.GetClipboardData.restype = ctypes.c_void_p
            u.GetClipboardSequenceNumber.restype = ctypes.c_uint
            u.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
            u.RegisterClipboardFormatW.restype = ctypes.c_uint
            k.GlobalLock.argtypes = [ctypes.c_void_p]
            k.GlobalLock.restype = ctypes.c_void_p
            k.GlobalUnlock.argtypes = [ctypes.c_void_p]
            k.GlobalUnlock.restype = ctypes.c_int
            _CLIP_API = (u, k)
        except Exception:
            _CLIP_API = False
    return _CLIP_API if _CLIP_API else None


def clipboard_text():
    """Clipboard text (CF_UNICODETEXT) or None (empty / not text / no access)."""
    api = _clip_api()
    if not api:
        return None
    u, k = api
    try:
        if not u.OpenClipboard(None):
            return None
        try:
            h = u.GetClipboardData(13)   # CF_UNICODETEXT
            if not h:
                return None
            p = k.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()
    except Exception:
        return None


def clipboard_html():
    """The clipboard's HTML flavor: (fragment_html, source_url) or (None, None).
    Chrome/Edge attach this to every page copy - SourceURL carries the page's
    own address (which gives us ids and page numbers)."""
    api = _clip_api()
    if not api:
        return None, None
    u, k = api
    try:
        fmt = u.RegisterClipboardFormatW("HTML Format")
        if not fmt:
            return None, None
        if not u.OpenClipboard(None):
            return None, None
        try:
            h = u.GetClipboardData(fmt)
            if not h:
                return None, None
            p = k.GlobalLock(h)
            if not p:
                return None, None
            try:
                raw = ctypes.string_at(p)
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()
    except Exception:
        return None, None
    head = raw[:4000].decode("utf-8", "replace")
    src = None
    m = re.search(r"SourceURL:(\S+)", head)
    if m:
        src = m.group(1)
    frag = None
    m1 = re.search(r"StartFragment:(\d+)", head)
    m2 = re.search(r"EndFragment:(\d+)", head)
    if m1 and m2:
        try:
            frag = raw[int(m1.group(1)):int(m2.group(1))].decode("utf-8", "replace")
        except Exception:
            frag = None
    if frag is None:
        frag = raw.decode("utf-8", "replace")
    return frag, src


def _clipboard_seq():
    """Windows clipboard change counter (0 when unavailable)."""
    api = _clip_api()
    if not api:
        return 0
    try:
        return int(api[0].GetClipboardSequenceNumber())
    except Exception:
        return 0


def read_payload():
    """Whatever is on the clipboard right now: (text, page_html, source_url)."""
    html_text, src = clipboard_html()
    return clipboard_text(), html_text, src


# ------------------------------------------------------------------- sounds


def beep():
    """Windows warning sound for unexpected / failed steps (silent elsewhere)."""
    if winsound is None:
        return
    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


# ------------------------------------------------------------- knowledge DB


def db_load():
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            db = json.load(f)
        if isinstance(db, dict) and isinstance(db.get("teases"), dict):
            return db
    except Exception:
        pass
    return {"version": 1, "teases": {}}


def db_save(db):
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=1, ensure_ascii=False)
    except OSError as e:
        out("  !! could not write the knowledge DB: %s" % e)


def db_entry(db, tid):
    """Get or create the entry for a tease id (unknowns get placeholders)."""
    tid = str(tid)
    e = db["teases"].get(tid)
    if e is None:
        e = dict(PLACEHOLDER)
        db["teases"][tid] = e
    return e


def db_set_real(e, field, value):
    """Set a field from a REAL source. Empty cells become placeholders so every
    key always exists; real values always overwrite."""
    v = str(value or "").strip()
    if not v or v in PLACEHOLDERS:
        v = PLACEHOLDER.get(field, "Unknown")
    e[field] = v


def db_fill(e, field, value):
    """Gap-fill from a page copy: only when the current value is unknown."""
    cur = str(e.get(field) or "").strip()
    v = str(value or "").strip()
    if v and v not in PLACEHOLDERS and (not cur or cur in PLACEHOLDERS):
        e[field] = v


def _has_keys(e):
    """True when title + author are known for real (folders only get these)."""
    if not e:
        return False
    t = str(e.get("title") or "").strip()
    a = str(e.get("author") or "").strip()
    return bool(t) and t not in PLACEHOLDERS and bool(a) and a not in PLACEHOLDERS


def db_stub(db, tid, assumed=None):
    e = db_entry(db, tid)
    e["wanted"] = True
    if assumed:
        e["assumed"] = assumed
    return e


def db_want_ids(db):
    """Wanted ids with a known title+author and still no data (no script, no
    simple pages). Stubs without keys are rejected at creation time."""
    ids = []
    for tid, e in db["teases"].items():
        if not e.get("wanted"):
            continue
        if e.get("has_script") or e.get("pages"):
            continue
        if not _has_keys(e):
            continue
        ids.append(tid)
    return ids


def _fmt_numbers(nums):
    """[1,2,3,7,8] -> '1-3, 7-8'"""
    nums = sorted(nums)
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if j == i else "%d-%d" % (nums[i], nums[j]))
        i = j + 1
    return ", ".join(parts)


def _label(db, tid):
    e = db_entry(db, tid)
    return '%s "%s"' % (tid, e.get("title") or "Unknown")


# ------------------------------------------------------------------- import


def _rebuild_simple_pages(folder, meta, e):
    """Recover page records from a converted simple-tease script, so future
    page copies MERGE instead of wiping the tease."""
    try:
        with open(os.path.join(folder, "tease.json"), encoding="utf-8") as f:
            script = json.load(f)
        quality, _scope = parse_info(os.path.join(folder, "info.txt"))
        files = script.get("files") or {}
        pages = {}
        for pid, cmds in (script.get("pages") or {}).items():
            m = re.fullmatch(r"P(\d+)", str(pid))
            if not m:
                continue
            num = int(m.group(1))
            imgs = []
            text = ""
            end = False
            for c in cmds or []:
                if not isinstance(c, dict):
                    continue
                if "image" in c:
                    loc = (c.get("image") or {}).get("locator") or ""
                    if loc.startswith("file:"):
                        name = loc[5:]
                        h = (files.get(name) or {}).get("hash")
                        ext = name.rsplit(".", 1)[-1] if "." in name else "jpg"
                        if h:
                            imgs.append([name, h, ext,
                                         quality if quality in ("s", "m", "l", "xl") else None])
                elif "say" in c:
                    text = (c.get("say") or {}).get("label") or ""
                elif "end" in c:
                    end = True
            pages[str(num)] = {"images": imgs, "text": text, "end": end}
        if pages and not e.get("pages"):
            e["pages"] = pages
    except Exception:
        pass


def import_teases(db):
    """teases\\ -> DB (on launch). Folders only FILL GAPS: real DB values (e.g.
    fresher listing data) always survive; placeholders are never copied over
    real values; simple conversions get their pages rebuilt. ORPHANED copies
    are left alone (utility folders, not teases)."""
    n = 0
    seen = {}
    if not os.path.isdir(TEASES):
        return n
    for name in sorted(os.listdir(TEASES)):
        folder = os.path.join(TEASES, name)
        meta_p = os.path.join(folder, "tease-meta.json")
        if not os.path.isdir(folder) or not os.path.isfile(meta_p):
            continue
        try:
            with open(meta_p, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        tid = str(meta.get("id") or "").strip()
        if not tid:
            continue
        if str(meta.get("title") or "").strip().endswith("(ORPHANED)"):
            continue                      # utility folder - never a tease entry
        if tid in seen:
            beep()
            out('  warning: duplicate tease folders for id %s: "%s" and "%s"' % (tid, seen[tid], name))
            out("           (delete the stale one - folder names must match title changes)")
            continue
        seen[tid] = name
        e = db_entry(db, tid)
        for key in ("title", "author", "tags", "description"):
            v = str(meta.get(key) or "").strip()
            cur = str(e.get(key) or "").strip()
            if v and v not in PLACEHOLDERS and (not cur or cur in PLACEHOLDERS):
                e[key] = v      # folders fill gaps; real DB values win
        if os.path.isfile(os.path.join(folder, "tease.json")):
            e["has_script"] = True
        if meta.get("source") == "simple-tease":
            e["type"] = "static"
            _rebuild_simple_pages(folder, meta, e)
        else:
            e["type"] = "player"
        n += 1
    return n


def _find_folder_by_id(tid):
    """The tease folder for this id (ORPHANED copies excluded), or None."""
    if not os.path.isdir(TEASES):
        return None
    for name in sorted(os.listdir(TEASES)):
        folder = os.path.join(TEASES, name)
        meta_p = os.path.join(folder, "tease-meta.json")
        if not os.path.isdir(folder) or not os.path.isfile(meta_p):
            continue
        try:
            with open(meta_p, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        if str(meta.get("id")) != str(tid):
            continue
        if str(meta.get("title") or "").strip().endswith("(ORPHANED)"):
            continue
        return folder
    return None


def _sync_folder(tid, title, author):
    """The folder for this id, created as 'id title' - and RENAMED when the
    title changed (no stale 'Unknown' folders, no duplicates). Never creates
    anything before title+author are known. Returns (folder, renamed_from)."""
    t = str(title or "").strip()
    a = str(author or "").strip()
    if (not t) or t in PLACEHOLDERS or (not a) or a in PLACEHOLDERS:
        return None, None
    desired = os.path.join(TEASES, sanitize("%s %s" % (tid, title)))
    cur = _find_folder_by_id(tid)
    if cur is None:
        os.makedirs(desired, exist_ok=True)
        return desired, None
    if os.path.normcase(os.path.abspath(cur)) == os.path.normcase(os.path.abspath(desired)):
        return cur, None
    if os.path.exists(desired):
        return cur, None                  # that name is taken - keep as is
    try:
        os.rename(cur, desired)
        return desired, os.path.basename(cur)
    except OSError:
        return cur, None


def _meta_for(db, tid):
    e = db_entry(db, tid)
    meta = {"id": int(tid),
            "title": str(e.get("title") or PLACEHOLDER["title"]),
            "author": str(e.get("author") or PLACEHOLDER["author"])}
    for key in ("tags", "description"):
        v = str(e.get(key) or "").strip()
        if v:
            meta[key] = v
    return meta


def write_tease_from_script(db, tid, script):
    """A banked EOS script -> tease folder (script + meta + info; media via R).
    Only ever writes when title+author are known; renames stale folders.
    Returns (folder, None) or (None, reason)."""
    meta = _meta_for(db, tid)
    folder, was = _sync_folder(tid, meta["title"], meta["author"])
    if folder is None:
        return None, "no title/author known yet"
    if was:
        out('    (folder renamed: "%s" -> "%s")' % (was, os.path.basename(folder)))
    with open(os.path.join(folder, "tease.json"), "w", encoding="utf-8") as f:
        json.dump(script, f)
    with open(os.path.join(folder, "tease-meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    quality, scope = load_q_defaults()
    write_info(os.path.join(folder, "info.txt"), tid, meta["title"], meta["author"],
               quality, scope, "SCRIPT BANKED - run R to fetch the media", None)
    e = db_entry(db, tid)
    e["has_script"] = True
    e["type"] = "player"
    return folder, None


def convert_simple(db, tid):
    """(Re)build the converted tease from ALL page records in the DB.
    Simple conversions are PINNED to what the pages themselves use (tier +
    used-only) - nothing else is ever assumed to exist.
    Returns (result, None) or (None, reason)."""
    e = db_entry(db, tid)
    pages = e.get("pages") or {}
    if not pages:
        return None, "no pages yet"
    nums = sorted(int(n) for n in pages)
    end_nums = [n for n in nums if pages[str(n)].get("end")]
    end_num = max(end_nums) if end_nums else None      # a re-copied higher END wins
    missing = [n for n in range(1, (end_num or nums[-1]) + 1)
               if str(n) not in pages and n != end_num]

    if not _has_keys(e):
        # pages live in the DB; a folder only ever appears with a real name
        return {"folder": None, "pending": True, "pages": nums, "end": end_num,
                "missing": missing, "images": 0}, None

    files = {}
    script_pages = {}
    ordered = []
    for n in nums:
        rec = pages[str(n)]
        pid = "P%04d" % n
        cmds = []
        for img in rec.get("images") or []:
            name, h, ext = img[0], img[1], img[2]
            tier = img[3] if len(img) > 3 else None
            files[name] = {"hash": h, "type": parsers.mime_for_ext(ext)}
            cmd = {"locator": "file:%s" % name}
            if tier:
                cmd["size"] = tier
            cmds.append({"image": cmd})
        cmds.append({"say": {"label": rec.get("text") or "&nbsp;", "mode": "pause"}})
        cmds.append({"end": {}})
        ordered.append((pid, n, cmds))
    for i, (pid, n, cmds) in enumerate(ordered):
        terminal = (end_num is not None and n == end_num)
        if not terminal and i + 1 < len(ordered):
            cmds[-1] = {"goto": {"target": ordered[i + 1][0]}}
        script_pages[pid] = cmds
    script_pages["start"] = [{"goto": {"target": ordered[0][0]}}]
    script = {"pages": script_pages, "files": files}

    tiers = [img[3] for n in nums for img in (pages[str(n)].get("images") or [])
             if len(img) > 3 and img[3]]
    quality = max(set(tiers), key=tiers.count) if tiers else "original"

    meta = _meta_for(db, tid)
    meta["source"] = "simple-tease"
    e["type"] = "static"
    folder, was = _sync_folder(tid, meta["title"], meta["author"])
    if was:
        out('    (folder renamed: "%s" -> "%s")' % (was, os.path.basename(folder)))
    with open(os.path.join(folder, "tease.json"), "w", encoding="utf-8") as f:
        json.dump(script, f)
    with open(os.path.join(folder, "tease-meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    write_info(os.path.join(folder, "info.txt"), tid, meta["title"], meta["author"],
               quality, "used-only",
               "CONVERTED SIMPLE TEASE (%d page(s) saved%s) - run R to fetch the images" % (
                   len(nums), ", END=%d" % end_num if end_num else ", NO END PAGE"),
               "%d page(s); missing: %s" % (len(nums),
                                            _fmt_numbers(missing) if missing else "none"))
    return {"folder": folder, "pending": False, "pages": nums, "end": end_num,
            "missing": missing, "images": len(files)}, None


# ------------------------------------------------------------------ handlers


def handle_listing(db, entries):
    new = upd = 0
    for it in entries:
        tid = str(it["id"])
        existed = tid in db["teases"]
        e = db_entry(db, tid)
        before = (e.get("title"), e.get("author"), e.get("tags"), e.get("description"),
                  e.get("type"))
        db_set_real(e, "title", it.get("title"))
        db_set_real(e, "author", it.get("author"))
        db_set_real(e, "tags", " ".join(it.get("tags") or []))
        db_set_real(e, "description", it.get("desc"))
        if it.get("type"):
            e["type"] = it["type"]          # listings are authoritative (Flash/EOS picto = player)
        after = (e.get("title"), e.get("author"), e.get("tags"), e.get("description"),
                 e.get("type"))
        if not existed:
            new += 1
        elif before != after:
            upd += 1
    players = sum(1 for it in entries if it.get("type") == "player")
    out("  listing: %d meta pair(s) read (%d new, %d refreshed; %d player / %d static)"
        % (len(entries), new, upd, players, len(entries) - players))


def handle_simple(db, rec):
    tid = str(rec["id"])
    e = db_entry(db, tid)
    db_fill(e, "title", rec.get("title"))
    db_fill(e, "author", rec.get("author"))
    e["type"] = "static"
    e.setdefault("pages", {})[str(rec["page"])] = {
        "images": rec["images"], "text": rec["text"], "end": bool(rec["end"])}
    res, err = convert_simple(db, tid)
    if err:
        out("  %s: page stored, but conversion failed (%s)" % (_label(db, tid), err))
        beep()
        return
    if res.get("pending"):
        out('  %s page %d stored - the folder appears once title+author are known' %
            (_label(db, tid), rec["page"]))
        out("    (copy a listing page that contains this tease, then copy any of its pages again)")
        return
    extra = []
    if res["end"] is None:
        extra.append("no END yet")
    if res["missing"]:
        extra.append("missing " + _fmt_numbers(res["missing"]))
    out('  %s page %d saved \u2192 tease updated: pages %s%s%s' % (
        _label(db, tid), rec["page"], _fmt_numbers(res["pages"]),
        (" + END(%d)" % res["end"]) if res["end"] else "",
        ("  \u2014 %s" % "; ".join(extra)) if extra else ""))


def handle_player(db, tid, page_title="", page_author=""):
    e = db_entry(db, tid)
    db_fill(e, "title", page_title)
    db_fill(e, "author", page_author)
    if e.get("has_script") or e.get("pages"):
        out('  player page: %s  (already have its data - nothing queued)' % _label(db, tid))
        return
    if not _has_keys(e):
        beep()
        out("  player page for %s: could not read title/author from it - skipped" % tid)
        out("    (keep that page - it is a parser patch sample!)")
        return
    e["type"] = "player"
    db_stub(db, tid, assumed="player")
    out('  player page \u2192 stub: %s  (script pending - S mode banks it)' % _label(db, tid))


def handle_link(db, tid):
    e = db_entry(db, tid)
    if e.get("has_script") or e.get("pages"):
        out('  link: %s  (already have its data - nothing queued)' % _label(db, tid))
        return
    if not _has_keys(e):
        beep()
        out("  link: %s  (no title/author known yet - copy a page that lists this tease" % tid)
        out("    first, then copy the link again - skipped)")
        return
    db_stub(db, tid, assumed="player")
    out('  link \u2192 stub: %s  (queued - S mode banks it later)' % _label(db, tid))


def looks_like_script_json(txt):
    """A valid EOS script copy: JSON with a non-empty 'pages' dict."""
    if not txt:
        return False
    t = txt.lstrip("\ufeff\r\n\t ")
    if not t.startswith("{"):
        return False
    try:
        data = json.loads(t)
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("pages"), dict) and bool(data["pages"])


def process_payload(db, text, page_html, source_url):
    """Handle one clipboard payload by its kind. Returns a small dict describing
    what was handled ({'kind', 'id'}) - or None for unrelated junk (silent)."""
    if page_html:
        what = parsers.classify_page(page_html, source_url)
        if what is None:
            if source_url and "milovana.com" in source_url:
                beep()
                out("  that Milovana page did not parse (site may have failed to load) - ignored.")
                return {"kind": "bad-page"}
            return None
        if what["kind"] == "listing":
            handle_listing(db, what["entries"])
            db_save(db)
            return {"kind": "listing", "count": len(what["entries"])}
        if what["kind"] == "simple":
            handle_simple(db, what["page"])
            db_save(db)
            return {"kind": "simple", "id": str(what["page"]["id"])}
        if what["kind"] == "player":
            handle_player(db, what["id"], what.get("title", ""), what.get("author", ""))
            db_save(db)
            return {"kind": "player", "id": str(what["id"])}
    if text:
        tid = parsers.tease_id_from_link(text)
        if tid:
            handle_link(db, tid)
            db_save(db)
            return {"kind": "link", "id": tid}
    return None


# ---------------------------------------------------------------- S mode


def _s_left(db, ids):
    n = 0
    for t in ids:
        e = db.get("teases", {}).get(t) or {}
        if e.get("wanted") and not e.get("has_script") and not e.get("pages"):
            n += 1
    return n


def mode_grab_scripts(db):
    """S: collect the missing data, driven by each stub's TYPE:
    - player  -> open the geteosscript link and wait for that JSON copy
    - static  -> open the tease's first page (a bookmark; copy pages whenever)
    - unknown -> open the tease page first to LEARN the type (copy it once):
                 static -> its first page is stored; player -> the JSON opens next.
    Enter = skip the current id; any copied page/listing is absorbed mid-wait."""
    ids = db_want_ids(db)
    if not ids:
        out("  S: nothing to grab - no wanted stubs without data (with title+author known).")
        return
    out("")
    out("  S MODE - %d wanted stub(s), one at a time (Enter = skip the current one):" % len(ids))
    out("    player teases -> geteosscript link opens; copy that JSON (Ctrl+A, Ctrl+C)")
    out("    unknown       -> the tease page opens first; copy it so I can tell what it is")
    out("    static        -> its first page just opens (copy its pages whenever)")
    _OPENED_URLS.clear()
    banked = skipped = 0
    idx = 0
    while idx < len(ids):
        tid = ids[idx]
        e = db.get("teases", {}).get(tid) or {}
        if not e.get("wanted") or e.get("has_script") or e.get("pages") or not _has_keys(e):
            idx += 1
            continue
        ttype = e.get("type")
        if ttype == "static":
            out("")
            out('  %s is a static tease - opening its first page: showtease.php?id=%s' % (_label(db, tid), tid))
            open_in_browser(PAGE_URL.format(id=tid))
            out("    copy its pages whenever you like - each one merges into the tease.")
            idx += 1
            continue
        phase = "json" if ttype == "player" else "learn"
        if phase == "json":
            out("")
            out('  [%d left] opening geteosscript.php?id=%s  %s' % (_s_left(db, ids), tid, _label(db, tid)))
            open_in_browser(SCRIPT_URL.format(id=tid))
            out('    waiting for the script - Ctrl+A, Ctrl+C in the tab that just opened. (Enter = skip)')
        else:
            out("")
            out('  [%d left] %s - no type known yet; opening its tease page: showtease.php?id=%s' %
                (_s_left(db, ids), _label(db, tid), tid))
            open_in_browser(PAGE_URL.format(id=tid))
            out('    copy that page (Ctrl+A, Ctrl+C) so I can tell static from player. (Enter = skip)')
        outcome = None
        buf = ""
        if msvcrt is not None:
            while msvcrt.kbhit():
                msvcrt.getwch()
        seq = _clipboard_seq()
        use_seq = seq != 0
        last = None if use_seq else clipboard_text()
        while outcome is None:
            time.sleep(0.25)
            if msvcrt is not None:
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\r", "\n"):
                        word = buf.strip().lower()
                        buf = ""
                        if word:
                            print()
                        if not word:
                            outcome = "skip"
                            break
                        if word in ("o", "l", "r", "s"):
                            outcome = word
                            break
                        out('  (unknown input "%s" - modes: O, L, R, S; Enter = skip)' % word)
                        beep()
                        continue
                    if ch in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        continue
                    if ch == "\b":
                        if buf:
                            buf = buf[:-1]
                            print("\b \b", end="", flush=True)
                        continue
                    if ch == "\x03":
                        raise KeyboardInterrupt
                    if ch.isprintable():
                        buf += ch
                        print(ch, end="", flush=True)
                if outcome is not None:
                    break
            changed = False
            if use_seq:
                s2 = _clipboard_seq()
                if s2 != seq:
                    seq = s2
                    changed = True
            else:
                t = clipboard_text()
                if t is not None and t != last:
                    last = t
                    changed = True
            if not changed:
                continue
            text, html_text, src = read_payload()
            if phase == "json" and looks_like_script_json(text):
                try:
                    script = json.loads(text.lstrip("\ufeff"))
                except Exception:
                    script = None
                if script is None:
                    out("  could not read that JSON - still waiting. (Enter = skip)")
                    beep()
                    continue
                src_id = parsers.tease_id_from_url(src) if src else None
                bind = str(src_id) if src_id else tid
                folder, werr = write_tease_from_script(db, bind, script)
                if folder is None:
                    beep()
                    out("  cannot bank %s: %s - skipped (copy a page for it, then run S again)" % (bind, werr))
                    skipped += 1
                    outcome = "next"
                    continue
                db_save(db)
                banked += 1
                out('  banked: %s "%s" - script written (%d pages / %d files)' % (
                    bind, (db["teases"].get(bind, {}) or {}).get("title") or "Unknown",
                    len(script.get("pages") or {}), len(script.get("files") or {})))
                if bind != tid:
                    beep()
                    out('  note: that script came from id %s - banked there; still waiting for %s.' % (bind, tid))
                    continue
                outcome = "next"
                continue
            info = process_payload(db, text, html_text, src)
            if info and info.get("kind") == "simple" and info.get("id") == tid:
                out("    that was page 1 of a static tease - stored; more pages merge as you copy them.")
                outcome = "next"
            elif info and info.get("kind") == "player" and info.get("id") == tid and phase == "learn":
                out("    it is a player tease! opening its geteosscript link now...")
                open_in_browser(SCRIPT_URL.format(id=tid))
                out('    waiting for the script - Ctrl+A, Ctrl+C in the tab that opened. (Enter = skip)')
                phase = "json"
            elif info:
                out('    (absorbed - still on %s "%s"; Enter = skip)' % (tid, e.get("title") or "Unknown"))
            else:
                out('    that was not usable content - still on %s "%s". (Enter = skip)' % (tid, e.get("title") or "Unknown"))
                beep()
        if outcome == "skip":
            out('  skipped: %s "%s"' % (tid, e.get("title") or "Unknown"))
            beep()
            skipped += 1
            idx += 1
        elif outcome in ("o", "l", "r", "s"):
            out("  leaving S mode...")
            run_mode(db, outcome)
            return
        elif outcome == "next":
            idx += 1
    out("")
    out("  S done: %d banked, %d skipped." % (banked, skipped))


def run_mode(db, word):
    if word == "o":
        orphan_extract()
    elif word == "l":
        list_existing()
    elif word == "r":
        repair_all(db)
    elif word == "s":
        mode_grab_scripts(db)
    out("")
    out("  back to the main loop - copy pages, or type a mode letter.")


def main_loop(db):
    """The always-on watchdog: console mode letters + clipboard payloads."""
    out("")
    out("=" * 66)
    out("  READY - copy pages straight from the site:")
    out("    listing page  -> meta pairs into the DB")
    out("    simple page   -> that tease is created/extended (per page)")
    out("    player page   -> stub (script via S mode)")
    out("    tease link    -> stub (script via S mode)")
    out("  Modes (type + Enter):  O orphan-extract | L list | R repair-all | S grab-scripts")
    out("  Exit: close the window or Ctrl+C.")
    out("=" * 66)
    seq = _clipboard_seq()
    use_seq = seq != 0
    last_text = None if use_seq else clipboard_text()
    buf = ""
    if msvcrt is not None:
        while msvcrt.kbhit():
            msvcrt.getwch()
    while True:
        time.sleep(0.25)
        if msvcrt is not None:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    word = buf.strip().lower()
                    buf = ""
                    if word:
                        print()
                        if word in ("o", "l", "r", "s"):
                            run_mode(db, word)
                        else:
                            out('  (unknown input "%s" - modes: O, L, R, S)' % word)
                            beep()
                    continue
                if ch in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                if ch == "\b":
                    if buf:
                        buf = buf[:-1]
                        print("\b \b", end="", flush=True)
                    continue
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch.isprintable():
                    buf += ch
                    print(ch, end="", flush=True)
        changed = False
        if use_seq:
            s2 = _clipboard_seq()
            if s2 != seq:
                seq = s2
                changed = True
        else:
            t = clipboard_text()
            if t is not None and t != last_text:
                last_text = t
                changed = True
        if changed:
            text, html_text, src = read_payload()
            if process_payload(db, text, html_text, src):
                continue
            if text and looks_like_script_json(text):
                out("  note: scripts are banked via S mode (type S) - this JSON was ignored.")


# -------------------------------------------------------- session defaults


def load_q_defaults():
    try:
        with open(Q_DEFAULTS, encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("quality") or "xl", d.get("scope") or "all")
    except Exception:
        return ("xl", "all")


def save_q_defaults(quality, scope):
    try:
        with open(Q_DEFAULTS, "w", encoding="utf-8") as f:
            json.dump({"quality": quality, "scope": scope}, f, indent=1)
    except OSError:
        pass


def ask_quality_scope_batch():
    """ONE-TIME session defaults for NEW EOS teases (Enter = last used).
    Simple-tease conversions are pinned instead: they use exactly the media
    their pages reference (tier + used-only); nothing else is assumed."""
    dq, ds = load_q_defaults()
    q_opts = ["xl", "original", "l", "m", "s"]
    out("")
    out("  session defaults for NEW EOS teases (simple-tease conversions are pinned):")
    for i, q in enumerate(q_opts, 1):
        out("    %d) %-9s %s" % (i, q, TIER_PIXELS.get(q, "")))
    while True:
        a = ask("  quality [%s] (number, Enter = keep): " % dq)
        if not a:
            quality = dq if dq in q_opts else "xl"
            break
        if a.isdigit() and 1 <= int(a) <= len(q_opts):
            quality = q_opts[int(a) - 1]
            break
    out("  scope for new EOS teases:")
    out("    1) all        - every media file in the script")
    out("    2) used-only  - only what the script references")
    while True:
        a = ask("  scope [%s] (1/2, Enter = keep): " % ds)
        if not a:
            scope = ds if ds in ("all", "used-only") else "all"
            break
        if a in ("1", "2"):
            scope = "all" if a == "1" else "used-only"
            break
    save_q_defaults(quality, scope)
    return quality, scope


# ---------------------------------------------------------------- main wizard


def main():
    out("=" * 66)
    out(" Offline tease downloader - clipboard watch dog (all state stays inside offline/)")
    out("=" * 66)
    quality, scope = ask_quality_scope_batch()
    db = db_load()
    n = import_teases(db)
    db_save(db)
    out("")
    out("  knowledge DB: %d tease(s) known (%d tease folder(s) imported from teases\\)."
        % (len(db["teases"]), n))
    out("  session defaults for new EOS teases: quality=%s, scope=%s" % (quality, scope))
    main_loop(db)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        out("")
        out("Closed by user - everything is saved.")
    except Exception as e:
        out("")
        out("Unexpected error: %r" % e)
        import traceback
        traceback.print_exc()
        out("Nothing outside the offline/ folder was touched.")
