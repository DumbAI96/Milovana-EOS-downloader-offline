#!/usr/bin/env python3
# Offline tease downloader (stdlib only - no pip packages).
# Spec: DOWNLOADER-SPEC.md (project root). Console wizard, 8 steps.
#
# Self-containment hard rule: everything this tool touches lives inside the
# offline/ folder - its own venv (downloader/venv), inbox (downloader/incoming),
# .part temp files next to their targets. Nothing in %TEMP%/%APPDATA%/registry.
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

# ---------------------------------------------------------------- paths / config
HERE = os.path.dirname(os.path.abspath(__file__))       # offline/downloader
BASE = os.path.dirname(HERE)                            # offline/
INCOMING = os.path.join(HERE, "incoming")
TEASES = os.path.join(BASE, "teases")

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


def print_script_instructions(tease_id):
    out("")
    out("  Browser step (milovana.com is Cloudflare-protected - the downloader")
    out("  cannot fetch the script itself):")
    out("    Save the page (Ctrl+S) into:  %s" % INCOMING)
    out("    (any file name works; in the browser wait until you can SEE the JSON")
    out("     text before saving - a Cloudflare check page saved as file won't work)")
    out("")
    open_in_browser(SCRIPT_URL.format(id=tease_id))


def find_inbox_script(tease_id):
    """Find a tease script among the files in the inbox. CONTENT decides validity:
    any file name / extension works ('geteosscript.json', 'geteosscript (1).json',
    even 'geteosscript.json.txt'). Preference: 'script-<id>.json' first, then
    NEWEST file first. Returns ((path, data), [other_valid_paths]) or (None, [])."""
    if not os.path.isdir(INCOMING):
        return None, []
    preferred = os.path.join(INCOMING, "script-%s.json" % tease_id)
    others = []
    for name in os.listdir(INCOMING):
        p = os.path.join(INCOMING, name)
        if not os.path.isfile(p) or p == preferred:
            continue
        if name.startswith(".") or name.lower() == "readme.txt":
            continue
        try:
            others.append((os.path.getmtime(p), p))
        except OSError:
            pass
    others.sort(reverse=True)  # newest first
    candidates = ([preferred] if os.path.isfile(preferred) else []) + [p for _m, p in others]
    valid = []
    for p in candidates:
        try:
            with open(p, encoding="utf-8-sig") as f:
                data = json.load(f)
            # A tease script is anything with a non-empty 'pages' dict.
            # 'galleries' is OPTIONAL: nyx/file-based teases have none (they use 'files').
            if isinstance(data.get("pages"), dict) and data["pages"]:
                valid.append((p, data))
        except Exception:
            continue
    if not valid:
        return None, []
    return valid[0], [p for p, _d in valid[1:]]


def inbox_report():
    """Explain what's in the inbox when nothing usable was found."""
    if not os.path.isdir(INCOMING):
        out("  -> the inbox folder does not exist: %s" % INCOMING)
        return
    entries = [n for n in sorted(os.listdir(INCOMING))
               if os.path.isfile(os.path.join(INCOMING, n))
               and not n.startswith(".") and n.lower() != "readme.txt"]
    if not entries:
        out("  -> the inbox is EMPTY - did the browser save somewhere else?")
        out("     (you can also paste the full path of the saved file here,")
        out("      or drag the file onto this console window)")
        return
    out("  -> inbox contents (nothing usable found):")
    for n in entries:
        p = os.path.join(INCOMING, n)
        try:
            size = os.path.getsize(p)
        except OSError:
            size = -1
        try:
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                head = f.read(400)
            if head.lstrip()[:1] == "<":
                verdict = "looks like an HTML page (Cloudflare check?) - in the browser wait until you SEE the JSON text, then save"
            else:
                try:
                    data = json.loads(open(p, encoding="utf-8-sig").read())
                    if isinstance(data.get("pages"), dict) and data["pages"]:
                        verdict = "VALID tease script (?) - please report this!"
                    elif isinstance(data, dict):
                        verdict = "JSON, but no 'pages' - top-level keys: %s" % \
                                  ", ".join(list(data.keys())[:8])
                    else:
                        verdict = "JSON (%s), but not a tease script" % type(data).__name__
                except Exception as e:
                    verdict = "not readable as JSON (%s)" % str(e)[:70]
        except Exception as e:
            verdict = "unreadable (%s)" % e
        out("       %-42s %8s B   %s" % (n[:42], size, verdict))


def note_extras(paths):
    if paths:
        out("  -> (also in inbox, ignored: %s - delete to avoid confusion)" %
            ", ".join(os.path.basename(p) for p in paths))


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


def ask_meta(prev):
    """Title/author/tags/description prompts.
    - defined key: Enter keeps the previous value, '-' clears it (stored as "")
    - missing key: Enter skips -> it stays MISSING (R asks again on a later run)
    Returns (title, author, tags, description); tags/description None = undefined."""
    t_prompt = "Title (as shown on the tease page): "
    if prev.get("title"):
        t_prompt = "Title [%s] (Enter to keep): " % prev["title"]
    title = ask(t_prompt)
    if not title and prev.get("title"):
        title = prev["title"]
    while not title:
        title = ask("Title is required (as shown on the tease page): ")
    a_prompt = "Author (optional, Enter to skip): "
    if prev.get("author"):
        a_prompt = "Author [%s] (Enter to keep, '-' to clear): " % prev["author"]
    author = ask(a_prompt)
    if author == "-":
        author = ""
    elif not author and prev.get("author"):
        author = prev["author"]

    def one(key, prompt_new, prompt_keep):
        if key in prev:
            cur = prev.get(key) or ""
            v = ask(prompt_keep % cur)
        else:
            v = ask(prompt_new)
        if v == "-":
            return ""
        if not v:
            return prev.get(key) if key in prev else None
        return clean_line(v)

    tags = one("tags",
               "Tags (optional, space-separated, Enter to skip): ",
               "Tags [%s] (Enter to keep, '-' to clear): ")
    description = one("description",
                      "Description (optional, single line, Enter to skip): ",
                      "Description [%s] (Enter to keep, '-' to clear): ")
    return title, author, tags, description


def prompt_missing_meta(meta, label):
    """R mode: ask ONLY for meta fields that are MISSING (tags/description).
    Enter skips a field -> it STAYS missing (a later R asks again).
    Returns (filled, asked)."""
    filled = asked = 0
    if "tags" not in meta:
        asked += 1
        v = ask('  Tags for "%s" (space-separated, Enter to skip): ' % label)
        if v:
            meta["tags"] = clean_line(v)
            filled += 1
    if "description" not in meta:
        asked += 1
        v = ask('  Description for "%s" (single line, Enter to skip): ' % label)
        if v:
            meta["description"] = clean_line(v)
            filled += 1
    return filled, asked


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


def repair_all():
    """R mode: repair EVERY tease in teases/ using the quality+scope it recorded
    in its info.txt. Mostly non-interactive: it asks ONLY for missing meta fields
    (tags/description; Enter skips -> asked again next time). Refetches only
    missing/suspicious files."""
    out("")
    out("=== REPAIR ALL: scanning teases/ (settings from each info.txt; missing tags/description are asked) ===")
    if not os.path.isdir(TEASES):
        out("  (no teases folder)")
        return
    tot = {"teases": 0, "skipped": 0, "downloaded": 0, "failed": 0,
           "meta_filled": 0, "meta_open": 0}
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
        filled, asked = prompt_missing_meta(meta, meta.get("title") or name)
        if asked:
            tot["meta_filled"] += filled
            tot["meta_open"] += asked - filled
            if filled:
                with open(meta_p, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=1)
                out("  meta: saved %d field(s)" % filled)
        stats = run_pipeline(folder, script, str(meta.get("id", "?")),
                             meta.get("title", ""), meta.get("author", ""),
                             quality, scope, prev_quality=None, interactive=False)
        tot["downloaded"] += stats["downloaded"]
        tot["failed"] += stats["failed"]
        if stats["bad"] == 0:
            out("  OK - %d/%d files verified%s" %
                (stats["ok"], stats["total"],
                 (", %d known-unavailable" % stats.get("gone", 0)) if stats.get("gone") else ""))
        else:
            out("  PARTIAL - %d missing/bad (run R again to retry)" % stats["bad"])
    done_line = ("REPAIR DONE. teases: %d | skipped: %d | downloaded: %d | still failed: %d" %
                 (tot["teases"], tot["skipped"], tot["downloaded"], tot["failed"]))
    if tot["meta_filled"] or tot["meta_open"]:
        done_line += " | meta: %d filled, %d left open (asked again next run)" % (
            tot["meta_filled"], tot["meta_open"])
    out("")
    out(done_line)
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


# ---------------------------------------------------------------- main wizard


def main():
    out("=" * 66)
    out(" Offline tease downloader  (everything stays inside the offline/ folder)")
    out("=" * 66)

    # ---------- step 1: tease id ----------
    out("")
    out("=== Step 1/8: tease id ===")
    while True:
        s = ask('Tease id (digits), "L" to list, "R" to REPAIR ALL (fills missing tags/desc), "O" to extract ORPHANED leftovers: ')
        if s.lower() == "l":
            list_existing()
            continue
        if s.lower() == "r":
            repair_all()
            return
        if s.lower() == "o":
            orphan_extract()
            return
        if s.isdigit():
            tease_id = s
            break
        out("  -> digits, L, R or O")
    existing = find_existing(tease_id)
    prev_quality = None
    if existing:
        out("")
        out('Existing tease found: "%s"   [UPDATE mode]' % os.path.basename(existing))
        q_prev, s_prev = parse_info(os.path.join(existing, "info.txt"))
        if q_prev:
            prev_quality = q_prev
            out("  (info.txt says: quality=%s, scope=%s - you will re-choose)" %
                (q_prev, s_prev))
        if ask('Press Enter to continue (or "x" to abort): ').lower() == "x":
            return

    # ---------- step 2: script + meta ----------
    out("")
    out("=== Step 2/8: script + meta ===")
    prev_meta = {}
    if existing:
        meta_p = os.path.join(existing, "tease-meta.json")
        if os.path.isfile(meta_p):
            try:
                with open(meta_p, encoding="utf-8") as f:
                    prev_meta = json.load(f)
            except Exception:
                pass

    # NEW teases: title/author/tags/description FIRST (paste them from the tease page
    # you already have open; only the title is required - Enter skips the rest), THEN
    # the script - the browser opens only for the JSON (one switch total).
    title = author = tags = description = None
    if not existing:
        out("  (paste title + author + optional tags/description from the tease page)")
        title, author, tags, description = ask_meta(prev_meta)

    script = None
    reuse = False
    if existing and os.path.isfile(os.path.join(existing, "tease.json")):
        ans = ask("Script: (Enter) reuse the existing tease.json / (N) fetch a new one from the browser: ")
        if ans.lower() != "n":
            reuse = True
            with open(os.path.join(existing, "tease.json"), encoding="utf-8") as f:
                script = json.load(f)
            out("  -> reusing existing script")

    script_src = None
    if script is None:
        hit, extra = find_inbox_script(tease_id)
        if hit:
            script_src, script = hit[0], hit[1]
            out("  -> script found in inbox: %s" % os.path.basename(script_src))
            note_extras(extra)
        while script is None:
            print_script_instructions(tease_id)
            ans = ask("Press Enter when the file is in the inbox (or paste a full file path): ")
            if ans:
                try:
                    with open(ans.strip('"'), encoding="utf-8-sig") as f:
                        data = json.load(f)
                    if isinstance(data.get("pages"), dict) and isinstance(data.get("galleries"), dict):
                        script, script_src = data, ans.strip('"')
                        break
                    out("  -> that file is not a tease script (needs 'pages' + 'galleries')")
                    continue
                except Exception as e:
                    out("  -> cannot read that file: %s" % e)
                    continue
            hit, extra = find_inbox_script(tease_id)
            if hit:
                script_src, script = hit[0], hit[1]
                out("  -> found: %s" % os.path.basename(script_src))
                note_extras(extra)
                break
            out("  -> nothing usable found in the inbox yet.")
            inbox_report()

    # UPDATE teases: meta prompts stay after the script part (previous values kept).
    if existing:
        title, author, tags, description = ask_meta(prev_meta)

    # ---------- step 3: reference scan ----------
    out("")
    out("=== Step 3/8: reference scan ===")
    used, all_items, sizes_req = scan_script(script)
    sum_used = sum(i["size"] for i in used.values())
    sum_all = sum(i["size"] for i in all_items.values())
    out("  pages: %d | galleries: %d | files entries: %d" %
        (len(script.get("pages", {})), len(script.get("galleries", {})), len(script.get("files", {}))))
    out("  used-only : %d files  (~%s original)" % (len(used), human(sum_used)))
    out("  all       : %d files  (~%s original)" % (len(all_items), human(sum_all)))
    if sizes_req:
        out("  image sizes this tease requests: %s" %
            ", ".join("%s x%d" % (k, v) for k, v in sorted(sizes_req.items())))

    # ---------- step 4: availability probe ----------
    out("")
    out("=== Step 4/8: availability probe (2-3 samples, polite pacing) ===")
    pool = [h for h, it in (used or all_items).items() if it["ext"] == "jpg"]
    samples = random.sample(pool, min(3, len(pool))) if pool else []
    availability = {}
    ratios = {}
    if samples:
        out("  samples: %s" % ", ".join(h[:10] + "..." for h in samples))
        out("  rendition   " + "  ".join("sample%d" % (i + 1) for i in range(len(samples))))
        for tier in ["xl", "l", "m", "s"]:
            statuses, ratio_list = [], []
            for h in samples:
                ok, data = fetch_bytes(MEDIA + "tb_%s/%s.jpg" % (tier, h))
                statuses.append("200" if ok else "404")
                if ok and all_items.get(h, {}).get("size"):
                    ratio_list.append(len(data) / float(all_items[h]["size"]))
            availability[tier] = {"ok_all": all(s == "200" for s in statuses),
                                  "ok_any": any(s == "200" for s in statuses)}
            if ratio_list:
                ratios[tier] = sum(ratio_list) / len(ratio_list)
            out("  %-10s  %s" % (tier, "   ".join(statuses)))
        out("  (original is always available - recorded sizes come from the script)")
    else:
        out("  (no jpeg images referenced - nothing to probe)")

    # ---------- step 5: quality + scope menu ----------
    out("")
    out("=== Step 5/8: quality + scope (explicit choice required) ===")
    options = [q for q in TIER_ORDER if q == "original" or availability.get(q, {}).get("ok_any")]
    for i, q in enumerate(options, 1):
        est_used = sum_used if q == "original" else sum_used * ratios.get(q, 0)
        est_all = sum_all if q == "original" else sum_all * ratios.get(q, 0)
        note = ""
        if q != "original":
            a = availability.get(q, {})
            if not a.get("ok_all"):
                note = "  [PARTIAL - fallback used where missing]"
        out("  %d) %-9s %-22s est. used-only ~%s | all ~%s%s" %
            (i, q, TIER_PIXELS[q], human(est_used), human(est_all), note))
    while True:
        q = ask("Enter quality number: ")
        if q.isdigit() and 1 <= int(q) <= len(options):
            quality = options[int(q) - 1]
            break
        out("  -> pick a number from the list")
    out("")
    out("  scope options:")
    out("   1) used-only  - %d files (only what the script references)" % len(used))
    out("   2) all        - %d files (includes %d never-referenced)" %
        (len(all_items), len(all_items) - len(used)))
    while True:
        s = ask("Enter scope number: ")
        if s in ("1", "2"):
            scope = "used-only" if s == "1" else "all"
            break
        out("  -> 1 or 2")
    items = used if scope == "used-only" else all_items

    # ---------- steps 6-8: layout, verify, download, finalize (shared pipeline) ----------
    folder = existing or os.path.join(TEASES, sanitize("%s %s" % (tease_id, title)))
    os.makedirs(os.path.join(folder, "timg"), exist_ok=True)
    if not reuse and script_src:
        shutil.move(script_src, os.path.join(folder, "tease.json"))
    elif not os.path.isfile(os.path.join(folder, "tease.json")):
        with open(os.path.join(folder, "tease.json"), "w", encoding="utf-8") as f:
            json.dump(script, f)
    meta_out = dict(prev_meta) if existing else {}   # updates keep unknown keys
    meta_out["id"] = int(tease_id)
    meta_out["title"] = title
    meta_out["author"] = author
    if tags is not None:            # None = user skipped -> key stays missing
        meta_out["tags"] = tags
    if description is not None:
        meta_out["description"] = description
    with open(os.path.join(folder, "tease-meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta_out, f, indent=1)

    stats = run_pipeline(folder, script, tease_id, title, author, quality, scope,
                         prev_quality=prev_quality, interactive=True)
    out("")
    if stats["bad"] == 0:
        out("DONE. %d/%d files verified (%s).%s" %
            (stats["ok"], stats["total"], human(stats["bytes"]),
             (" (%d known-unavailable - see unavailable.txt)" % stats["gone"])
             if stats.get("gone") else ""))
        out('Start "start-server.bat" -> the landing page lists the tease -> play.')
    else:
        out("PARTIAL: %d file(s) missing/bad - rerun this tool to retry." % stats["bad"])
    out("(Rerun any time to update or repair this tease.)")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        out("")
        out("Aborted by user - finished files are kept; rerun to resume.")
    except Exception as e:
        out("")
        out("Unexpected error: %r" % e)
        out("Nothing outside the offline/ folder was touched.")
