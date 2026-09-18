# Offline Eos teases

A fully offline **player + downloader** for Milovana "Eos" webteases. Everything lives in one
self-contained folder and uses only relative paths — copy or move it anywhere and it still runs:

- **Player** — recreates the official viewer locally: the real engine, every branch, all media, 100% offline.
- **Extras** — a quality-of-life layer on top of the player (auto-advance, timer controls + speed
  multiplier, navigation + checkpoints, back button).
- **Downloader** — a polite, resumable console wizard that turns a tease id into a ready-to-play folder.
- **Landing page** — a sortable / filterable table of your whole library.

> **A fresh clone of this repository does not include the official player files** (they are
> third-party and not redistributable). Run the one-time
> [How to DIY](#how-to-diy-assembling-the-player-files) setup at the bottom before the first start.

## Disclaimer, ownership & license

- **Not affiliated** with Milovana.com or eosscript.com. All trademarks, names and content belong to
  their respective owners.
- **This repository contains only original code** written for this project (server, downloader,
  player shell + shim, extras layer, docs). It intentionally contains **no files fetched from other
  sites** — no app bundles, css, fonts, images, no tease content, no media. You fetch those yourself
  (see [How to DIY](#how-to-diy-assembling-the-player-files)).
- **Downloaded teases belong to their authors** (and image/audio rights holders): use them for
  personal offline play/backup; do not redistribute them.
- The downloader is **polite by design** (paced requests, browser-assist for challenge-protected
  pages, no protection bypassing). Mind the source sites' terms — you are responsible for how you
  use this tool.
- **License:** original code in this repo is released under the MIT License (see `LICENSE`).
  Third-party files you fetch are **not** covered by it.
- **No warranty** — provided as-is, use at your own risk.

## System requirements

- **Windows 10 or newer** — the launchers are `.bat` files; the Python components themselves are
  cross-platform (write your own launcher if you need another OS).
- **Python 3.12** (or newer) available as `python` on PATH. The launchers create a local venv on
  first run — nothing is installed system-wide, no pip packages needed (stdlib only).
- Any current browser (Chrome / Edge / Firefox — the player uses ES modules).
- Everything binds to **127.0.0.1 only**: nothing is exposed to the network.
- Internet is needed only for downloading teases (and the one-time DIY setup). Playing is fully offline.

## Getting started

> **First time?** A fresh clone starts stripped — complete the one-time
> [How to DIY](#how-to-diy-assembling-the-player-files) setup at the bottom of this file first.
> If your copy already has the player files, skip straight to the launcher.

Double-click **`start-server.bat`**:

- creates/uses the local venv in `server\venv` (nothing installed system-wide)
- starts the local server and opens <http://localhost:8123/> (auto-retries the port if it's busy)
- the landing page opens: a sortable / filterable **table** of every tease in `teases\` — click a row
  to play
  - click a column header to sort (id numeric, empty values last)
  - each column has its own filter box under the header
  - the top bar matches **any word in any column**
  - the top bar + table header + filter row stay pinned while you scroll (freeze-panes style)

## Features

### Player

- One shared copy for **all** teases (`player/`), patched globally — see `player/PATCHES.txt`.
- Plays any downloaded tease 100% offline: full script, every branch, all media.
- The local server serves media with a **fallback chain** (requested quality → original → xl → l → m
  → s) — any downloaded quality plays with any tease.
- Progress and script variables are saved to **files** under `state/` (no browser storage involved).
- Fully portable: the whole folder can be copied or moved anywhere; all paths are relative.
- URL parameters: `?tease=<path>` selects the tease · `&start=<pageId>` jumps straight to a page ·
  `&debug=eos:*` enables engine debug logging (open the F12 console first).

### Offline extras (player top bar — code in `player/extras.js`)

The top bar groups are separated by dim ♠ dividers: `Auto` / `NoDelay` / `Timers` / `Nav+Back`.

- **`[Auto ▶]` / `[Auto ●]`** — toggle: auto-advances text lines ("click to continue" bubbles) every
  N seconds. It simulates a real SPACE key press — it never uses the mouse and never answers prompts,
  picks choices or clicks buttons.
- **`[N] s`** — seconds between advances (0.1 – 120.0, one decimal, remembered; e.g. 0.1 = skip dialog).
- **`[NoDelay]`** — toggle: makes ALL text lines behave like normal click-through ones (no built-in
  delays, including previously timed lines which now show the continue arrow). Compounds with Auto:
  with both on, everything advances by itself at your N seconds. Engine-level: affects every tease;
  off = original behavior.
- **Timers: `[Hidden]` `[All]` `[⏭ Now]` `× [1.0]`**
  - `[Hidden]` — toggle: instantly skip invisible timers (style "hidden")
  - `[All]` — toggle: instantly skip ALL timers, visible countdowns included
  - `[⏭ Now]` — button: skip the timer running right now (one-shot)
  - `× [1.0]` — multiplier (0.1 – 10.0, one decimal, remembered): scales the LENGTH of every timer
    that STARTS after the change — 0.5 = half length (faster), 2 = twice the length (slower),
    1 = normal. The countdown shows the scaled time.
  - Timer controls are live (they even skip a wait already in progress); default off. The skip
    buttons always override the multiplier.
- **`[Nav]`** — panel: jump to any page (current one highlighted) + MANUAL checkpoints: type an
  optional label, hit `[+ Save]` at a moment you care about, click a row later to restore it (script
  variables + progress + page); "×" deletes a checkpoint. Saved in
  `state\<tease folder>\checkpoints.json`.
- **`[← Back]`** — leaves the player and returns to the landing page (the tease list) — the whole tab
  navigates, no browser back needed.

Settings persist in `state\global\settings.json` (files, not the browser).

### Landing page

- Table of the whole library: **id / title / author / tags / description** (from each tease's
  `tease-meta.json`).
- Click a column header to sort (asc ⇄ desc); a filter box under each header filters that column;
  the top bar matches **any word in any column**; `clear` button + live `shown X / Y` counter.
- Top bar + header + filter row stay **pinned** while you scroll; descriptions wrap; tags render as
  chips; `ORPHANED` copies get a badge; click a row to play (the title is a real link — middle-click
  works too).

### Downloader (`downloader/`)

- Console wizard; everything it touches stays inside this folder (own venv + `incoming\` inbox).
- **Browser-assist** for script fetching: Cloudflare-protected pages are opened in *your* browser —
  the tool itself never bypasses challenges.
- The inbox accepts **any** file name/extension — the content decides; unusable files are listed
  with reasons (e.g. a saved Cloudflare page is detected).
- Availability probe + **quality/scope menu** with real size estimates (an explicit choice is
  required every run — no defaults).
- Downloads are **paced, resumable** (`.part` + atomic rename) and **byte-size verified** against the
  script; per-file quality fallback where a rendition is missing; failures are retried on rerun.
- Rerunning with the same id = **update/repair**; `R` at the first prompt = **repair ALL teases**
  (also asks for any missing tags/description as it passes each tease); `O` = orphan extractor
  (below).
- **Metadata per tease**: title/author + optional `tags` + `description` (asked at download time,
  fillable later via `R`; Enter skips — a skipped field is asked again next time). Tags are
  space-separated single words (use hyphens for compounds, e.g. `female-top`).
- Files the site no longer serves are listed per tease in `unavailable.txt` and skipped by repairs —
  delete a line to retry it.

### Orphan extractor (`O` at the first prompt)

- Pure-local analysis (no network): finds pages the script's flow can't reach + media nothing
  references.
- Creates a separate `<id> <title> ORPHANED` tease with an index page and click/SPACE slideshow
  chains (leftover media first, then the normal media); all media is copied so the folder is fully
  independent; the source tease is untouched. Its meta mirrors the source tease (title gets the
  suffix).
- Full report: `leftovers.txt`. Nothing orphaned → nothing is created.

### State & portability

- ALL persisted state lives in files under `state/` (global settings, per-tease progress,
  checkpoints) — nothing in browser storage.
- Each `teases\<folder>\` is pure data: `info.txt` (original link + download settings),
  `tease.json` (script), `tease-meta.json` (id/title/author/tags/description), `timg\` (all media).

## What's in this repository

```text
README.md             this file
LICENSE               MIT (original code only)
.gitignore            keeps venvs / state / teases / fetched files out of git
start-server.bat      launcher: venv + server + browser
start-downloader.bat  launcher: downloader (own venv)
server\
    server.py           tiny static server (stdlib only)
player\
    index.html          our shell page (?tease=..., hosts the player iframe)
    host-local.js       our RPC host (offline stand-in for the site's outer glue)
    extras.js           our feature layer (top-bar controls)
    PATCHES.txt         exact patch instructions for the official player files
downloader\
    downloader.py       the whole tool (stdlib only)
    incoming\
        README.txt        inbox for browser-fetched scripts
```

**Not in the repo (by design):** the official player files (`player.html`, `acorn-safe.min.js`,
`interpreter.min.js`, `eos.load.css`, `eos_throbber.gif`, `player\assets\**` — bundle, chunks, css,
fonts, images). Fetch them with the [How to DIY](#how-to-diy-assembling-the-player-files) steps.
Also excluded: `teases\` and `state\` (your data; created automatically on first use).

## Downloading teases (the wizard)

Double-click **`start-downloader.bat`** and follow the wizard:

- **`O`** at the first prompt — extract ORPHANED leftovers (see Features above).
- **`R`** at the first prompt — REPAIR ALL teases at once: re-verifies everything using each tease's
  recorded quality+scope and refetches missing/broken files; asks for any missing tags/description
  as it passes each tease (Enter skips — asked again next run).
- **New tease** — enter the id → paste title/author + optional tags/description (from the tease page
  you already have open; Enter skips the optional ones) → the script link opens in your browser
  automatically; `Ctrl+S` it into `downloader\incoming\` (ANY file name works — content decides; if
  the browser saved a Cloudflare check page instead, the tool says so) → quality + scope menu (probes
  which tiers exist, real size estimates) → download with pacing/resume/verification into a
  server-ready folder.

Rerun with the same id = update/repair (all options re-asked; on a quality change it asks whether to
remove old files).

Pacing: `DOWNLOAD_RATE` in `start-downloader.bat` (files per second).

Everything the downloader does stays inside this folder.

## Testing shortcuts

- Jump straight to a page: `http://localhost:8123/player/?tease=../teases/<folder>&start=<pageId>`
- Debug logging (open the F12 console first): append `&debug=eos:*`
- `http://localhost:8123/teases/<folder>/` also works (redirects to the player)

---

## How to DIY (assembling the player files)

This is the one-time setup that completes a fresh clone: **download the official player files**
from eosscript.com and **apply the patches** documented in `player/PATCHES.txt`. Nothing else in the
repo needs assembling. (The files are not shipped here because they belong to their owners — every
user fetches them directly from the source, exactly like their browser already does.)

### 1. Fetch the official player files

Open <https://eosscript.com/> — that page is the player "host page". Mirror the site into `player\`
(so the relative paths stay the same as on the site):

- save the host page itself as `player\player.html`
- into `player\` (root): `acorn-safe.min.js`, `interpreter.min.js`, `eos.load.css`, `eos_throbber.gif`
- into `player\assets\`: the app bundle (`index-<hash>.js`), its module chunks
  (`index-0391222d.js` notification, `index-0b44e4ec.js` audio, `index-82488813.js` storage,
  `index-86640c88.js` nyx, `index-dc5b7736.js` pcm2), the css files (`index-47f9131c.css`,
  `index-a9ffa513.css`, `index-ec19f3f1.css`), `navy-<hash>.png`, and all font files
  (`fontsans-*` ×6, `noto-sans-*` ×8).

> File-name hashes may differ from the ones above — the site updates over time. Mirror whatever it
> currently serves (any mirroring tool, or the browser's DevTools → Network tab, or save each file
> manually). Keep the paths: root files in `player\`, `/assets/...` under `player\assets\`.

### 2. Apply the patches

Every patch is a small **exact-string replacement**; `player/PATCHES.txt` lists every anchor and its
replacement, with "appears exactly once" checks. Apply them to *your downloaded copies*:

1. **`player\player.html`** — three edits: `/assets/...` references → relative `assets/...`; insert
   the tiny inline script that reads `?data=` and sets `window.__EOS_MEDIA_BASE__`; load `extras.js`
   as a module after the app bundle.
2. **`player\assets\index-<hash>.js`** (main bundle) — patches a–f from `PATCHES.txt` (media-base
   variable, runtime chunk-path helper, expose the engine for the extras layer, NoDelay gates, timer
   skip flags, timer multiplier wrap).
3. **`player\assets\index-86640c88.js` + `index-0391222d.js`** — the timer multiplier wrap (item 4
   in `PATCHES.txt`) so NyX and notification timers obey it too.
4. **`player\assets\index-47f9131c.css` + `index-ec19f3f1.css`** — `url(/assets/...)` → `url(...)`.

If an anchor is not found, the site shipped a new build — the patch set needs a refresh before it
will work against that version.

### 3. Verify

Your `player\` folder should now match the map below; start `start-server.bat` and open the landing
page. If the player misbehaves, open the F12 console — `[extras]` log lines show whether the feature
layer found the engine.

### 4. Let an AI do it

The instructions above are deliberately deterministic (exact anchors, exact file list). Paste this
section + `player/PATCHES.txt` + the fetched files into any capable AI assistant and ask it to apply
the patches and verify each anchor count — that's all there is to it.

### Expected final structure (everything except `teases\` and `state\`)

```text
offline\
├── README.md
├── LICENSE
├── .gitignore
├── start-server.bat
├── start-downloader.bat
├── server\
│   ├── server.py
│   └── venv\                        (auto-created on first run)
├── player\
│   ├── index.html                   (ours)
│   ├── host-local.js                (ours)
│   ├── extras.js                    (ours)
│   ├── PATCHES.txt                  (ours)
│   ├── player.html                  (fetched, then patched)
│   ├── acorn-safe.min.js            (fetched)
│   ├── interpreter.min.js           (fetched)
│   ├── eos.load.css                 (fetched)
│   ├── eos_throbber.gif             (fetched)
│   └── assets\
│       ├── index-6f90e65e.js        (fetched bundle, then patched)
│       ├── index-0391222d.js        (fetched, then patched)
│       ├── index-0b44e4ec.js        (fetched)
│       ├── index-82488813.js        (fetched)
│       ├── index-86640c88.js        (fetched, then patched)
│       ├── index-dc5b7736.js        (fetched)
│       ├── index-47f9131c.css       (fetched, then patched)
│       ├── index-a9ffa513.css       (fetched)
│       ├── index-ec19f3f1.css       (fetched, then patched)
│       ├── navy-37ade957.png        (fetched)
│       ├── fontsans-*.woff/.woff2   (fetched, ×6)
│       └── noto-sans-*.woff/.woff2  (fetched, ×8)
└── downloader\
    ├── downloader.py
    ├── venv\                        (auto-created on first run)
    └── incoming\
        └── README.txt
```

`teases\` and `state\` are created automatically when you first download / play something.
