# ============================================================================
#  SITE PARSERS for the offline downloader  --  THE ONE PATCH POINT.
#
#  Everything that knows Milovana's page markup lives in this file. When the
#  site changes (or new page kinds get captured / samples arrive), patch ONLY
#  here - the downloader handles payloads generically.
#
#  Recognized clipboard payloads:
#    listing  -> meta pairs for many teases   (<div class="tease"> boxes)
#    simple   -> one page of a classic page-style tease
#                (tease_pic image + text box + Continue button)
#    player   -> a script-based tease page (EOS / flash-converted / NyX -
#                all play in the same player; verified marker: "eosIframe")
#    link     -> a naked copied tease url (plain text: showtease.php?id=N)
#    shell    -> the PLAYER'S OWN page: you clicked inside the tease, so the
#                copy is eosscript's document - title/author live in its top
#                bar (_tease_ / _authorButton_), the Milovana id never does
#    address  -> an EMPTY parent-page copy (Ctrl+A without clicking): a player
#                tease's page is one big iframe, so only the id in the address
#                survives; empty copy + showtease address = interactive tease
#
#  Verified against real samples (2026-09-19): listings carry Flash/EOS
#  picto-tags on interactive teases (TOTM = Tease of the Month award, ignored)
#  -> parse_listing derives each entry's type from them; every interactive kind
#  embeds the same "eosIframe" player frame -> parse_player_page anchors there.
#  Clipboard probes (2026-09-19, live pages): a fresh Ctrl+A copy of a player
#  page = EMPTY fragment + the id in the address; a copy made after clicking
#  = the player shell (top bar keeps title/author, running or not).
# ============================================================================

import re
from html.parser import HTMLParser

# ------------------------------------------------------------------ markers

LISTING_BOX_CLASS = "tease"          # entry container on search/author/tag pages
SIMPLE_IMG_CLASS = "tease_pic"       # the per-page image of a simple tease
SIMPLE_TEXT_CLASS = "text"           # the text box of a simple page
SIMPLE_CONTINUE_ID = "continue"      # the Continue button of a simple page
PLAYER_PAGE_MARKERS = ("eosiframe", "eos.outer.js", "eosscript.com")   # refine w/ samples


def mime_for_ext(ext):
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext.lower(),
                                                          "image/" + ext.lower())


def strip_tags(s):
    """Visible text of a small HTML fragment (entities decoded, tags dropped)."""
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return " ".join(s.split())


def extract_header(html_text):
    """The page's visible title + author - works for ANY Milovana tease page
    (static or player). Primary: the tease heading; fallback: the document
    title (minus the " - Milovana.com" / " - Tease #N" decorations)."""
    title = author = ""
    h = re.search(r'<h1[^>]*id="tease_title"[^>]*>(.*?)</h1>', html_text, re.S)
    if not h:
        h = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S)
    if h:
        inner = h.group(1)
        am = re.search(r'<span[^>]*class="[^"]*tease_author[^"]*"[^>]*>(.*?)</span>',
                       inner, re.S)
        if am:
            author = strip_tags(am.group(1))
            if author.lower().startswith("by "):
                author = author[3:].strip()
        title = strip_tags(re.sub(r'<span[^>]*class="[^"]*tease_author[^"]*"[^>]*>.*?</span>',
                                  " ", inner, flags=re.S))
    if not title:
        t = re.search(r"<title>(.*?)</title>", html_text, re.S)
        if t:
            title = strip_tags(t.group(1))
            title = re.sub(r"\s*-\s*Milovana\.com\s*$", "", title)
            title = re.sub(r"\s*-\s*Tease\s*#\d+\s*$", "", title).strip()
    return title, author


# ------------------------------------------------------------- url / link ids

def tease_id_from_url(url):
    """'...showtease.php?id=30088&p=2' -> '30088' (or None)."""
    if not url:
        return None
    m = re.search(r"showtease\.php\?[^\s]*?\bid=(\d+)", url)
    return m.group(1) if m else None


def tease_page_no_from_url(url):
    """'...&p=23' -> 23 (or None when the address carries no page number)."""
    m = re.search(r"[?&]p=(\d+)", url or "")
    return int(m.group(1)) if m else None


def tease_id_from_link(text):
    """A naked copied tease link (plain text) -> id, else None. Strict: the text
    must BE such a url (optionally with scheme) - not prose containing one."""
    t = (text or "").strip()
    if not re.fullmatch(r"(?:https?://)?(?:www\.)?milovana\.com/webteases/showtease\.php\?[^\s#]*", t):
        return None
    return tease_id_from_url(t)


# ------------------------------------------------------------- listing pages

class _TeaseBoxParser(HTMLParser):
    """Collects the entries of a listing page (search / author / tag pages share
    one markup): every entry is a <div class="tease"> containing the title link,
    the author, the description and the tags. Sidebar boxes ("Tease of the
    Month" / "Random Tease") use different markup -> ignored."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.entries = []
        self._div_depth = 0
        self._entry = None
        self._entry_depth = 0
        self._context = None        # author | desc | tags
        self._ctx_div_depth = 0
        self._in_a = False
        self._a_buf = []
        self._h1 = False

    def _new_entry(self):
        return {"id": None, "title": "", "author": "", "desc": "", "tags": []}

    def _finish_entry(self):
        e = self._entry
        if e and e["id"] and e["title"]:
            e["title"] = " ".join(e["title"].split())
            e["author"] = " ".join(e["author"].split())
            e["desc"] = " ".join(e["desc"].split())
            # listing picto-tags: Flash/EOS -> interactive (player); none -> simple.
            # (TOTM = Tease of the Month award - deliberately ignored.)
            e["type"] = "player" if e.pop("_player", False) else "static"
            self.entries.append(e)
        self._entry = None
        self._context = None
        self._in_a = False
        self._a_buf = []
        self._h1 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div":
            self._div_depth += 1
            if LISTING_BOX_CLASS in classes and self._entry is None:
                self._entry = self._new_entry()
                self._entry_depth = self._div_depth
            elif self._entry is not None and "author" in classes:
                self._context = "author"
                self._ctx_div_depth = self._div_depth
            elif self._entry is not None and "desc" in classes:
                self._context = "desc"
                self._ctx_div_depth = self._div_depth
            elif self._entry is not None and "tags" in classes:
                self._context = "tags"
                self._ctx_div_depth = self._div_depth
        elif tag == "h1" and self._entry is not None:
            self._h1 = True
        elif tag == "a":
            self._in_a = True
            self._a_buf = []
            if self._entry is not None and self._entry["id"] is None:
                m = re.search(r"showtease\.php\?id=(\d+)", a.get("href") or "")
                if m:
                    self._entry["id"] = m.group(1)
        elif tag == "img":
            if self._entry is not None and self._h1:
                classes = (a.get("class") or "").split()
                if "eosticon" in classes or "flashticon" in classes:
                    self._entry["_player"] = True

    def handle_endtag(self, tag):
        if tag == "div":
            if (self._entry is not None and self._context
                    and self._div_depth == self._ctx_div_depth):
                self._context = None
            self._div_depth -= 1
            if self._entry is not None and self._div_depth < self._entry_depth:
                self._finish_entry()
        elif tag == "h1":
            self._h1 = False
        elif tag == "a":
            text = " ".join("".join(self._a_buf).split())
            was_in_a = self._in_a
            self._in_a = False
            self._a_buf = []
            if self._entry is None or not was_in_a or not text:
                return
            if self._h1:
                self._entry["title"] = (self._entry["title"] + " " + text).strip()
            elif self._context == "author" and not self._entry["author"]:
                self._entry["author"] = text
            elif self._context == "tags":
                self._entry["tags"].append(text)
            elif self._context == "desc":
                self._entry["desc"] += " " + text

    def handle_data(self, data):
        if self._entry is None or not data:
            return
        if self._in_a:
            self._a_buf.append(data)
        elif self._context == "desc":
            self._entry["desc"] += data


def parse_listing(html_text):
    """One listing-page copy -> [{'id','title','author','tags':[...],'desc'}]."""
    p = _TeaseBoxParser()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass
    return p.entries


# -------------------------------------------------------------- simple pages

def parse_simple_page(html_text, source_url):
    """Strict parse of ONE copied simple-tease page. Expected elements: title/
    author header, image(s), a text box, and either BOTH next-page links
    (Continue button + image click) or NONE (= the END page). Page numbers come
    from the links only; the copied page's own address cross-checks them.
    Returns (info, None) or (None, reason).

    info = {'id', 'page', 'end', 'title', 'author',
            'images': [[name, hash, ext, tier], ...], 'text'}"""
    tid = tease_id_from_url(source_url)
    if not tid:
        return None, "no tease id in the page address"
    url_page = tease_page_no_from_url(source_url)

    # title + author: the visible header text (never filenames / hidden attrs)
    title, author = extract_header(html_text)
    if not title:
        return None, "no title header"

    # images: the link's own file name IS the on-site media name (hash.ext);
    # the tier (tb_l / tb_xl / ...) is pinned in the record - nothing else is
    # ever assumed to exist for a simple tease.
    images = []
    for img in re.finditer(r"<img[^>]*>", html_text):
        tag = img.group(0)
        if not re.search(r'class="[^"]*%s[^"]*"' % SIMPLE_IMG_CLASS, tag):
            continue
        sm = re.search(r'src="([^"]+)"', tag)
        if not sm:
            return None, "tease image without src"
        fname = sm.group(1).rsplit("/", 1)[-1]
        fm = re.match(r"([0-9a-fA-F]{32,64})\.([A-Za-z0-9]{2,5})$", fname)
        if not fm:
            return None, "unexpected image name: %s" % fname[:40]
        tm = re.search(r"/tb_([smlx]+)/", sm.group(1))
        images.append([fname, fm.group(1), fm.group(2), tm.group(1) if tm else None])
    if not images:
        return None, "no tease image found"
    seen = set()
    images = [i for i in images if not (i[0] in seen or seen.add(i[0]))]

    # text box(es) - kept as RAW HTML (entities, <br>, ... stay untouched)
    texts = [t.strip() for t in re.findall(
        r'<p[^>]*class="[^"]*\b%s\b[^"]*"[^>]*>(.*?)</p>' % SIMPLE_TEXT_CLASS,
        html_text, re.S)]
    if not texts:
        return None, "no text box"
    label = "<br>".join(t for t in texts if t) or "&nbsp;"

    # next-page links: Continue button + image-click link - both or none
    hrefs = []
    cm = re.search(r'<a[^>]*id="%s"[^>]*>' % SIMPLE_CONTINUE_ID, html_text)
    if cm:
        hm = re.search(r'href="([^"]+)"', cm.group(0))
        if hm:
            hrefs.append(hm.group(1))
    im = re.search(r'<a([^>]*)>\s*<img[^>]*class="[^"]*%s' % SIMPLE_IMG_CLASS,
                   html_text, re.S)
    if im:
        hm = re.search(r'href="([^"]+)"', im.group(1))
        if hm:
            hrefs.append(hm.group(1))

    if not hrefs:
        # END page: no link to the next page anywhere
        if url_page is None:
            return None, "END page copy has no page number in its address"
        return {"id": tid, "page": url_page, "end": True, "title": title,
                "author": author, "images": images, "text": label}, None

    ps = []
    for hr in hrefs:
        pm = re.search(r"[?&]p=(\d+)", hr.replace("&amp;", "&"))
        ps.append(int(pm.group(1)) if pm else None)
    if len(hrefs) != 2 or None in ps or ps[0] != ps[1]:
        return None, "next-page links missing or disagreeing"
    page = ps[0] - 1
    if page < 1:
        return None, "implausible next page (p=%d)" % ps[0]
    if url_page is not None:
        if page != url_page:
            return None, "page mismatch: links say %d, address says %d" % (page, url_page)
    elif page != 1:
        return None, "page %d copy has no page number in its address" % page
    return {"id": tid, "page": page, "end": False, "title": title,
            "author": author, "images": images, "text": label}, None


# -------------------------------------------------------------- player pages

def parse_player_page(html_text, source_url):
    """An interactive tease page (EOS / flash-converted / NyX - all three play in
    the same player). Verified markers (real samples, 2026-09-19):
      - the runner page carries the truth on its <body> tag:
        data-tease-id / data-title / data-author (exactly the proper values)
      - the embedded player frame ("eosIframe") proves it is interactive.
    Returns ({'id','title','author'}, None) or (None, reason)."""
    tid = tease_id_from_url(source_url)
    if not tid:
        return None, "no tease id in the page address"
    if "eosIframe" not in html_text and "eosscript.com" not in html_text:
        return None, "no player frame found"

    def data_attr(name):
        m = re.search(r'data-%s="([^"]*)"' % name, html_text)
        return strip_tags(m.group(1)) if m else ""

    did = data_attr("tease-id") or tid
    title = data_attr("title")
    author = data_attr("author")
    if not title or not author:
        t2, a2 = extract_header(html_text)
        title = title or t2
        author = author or a2
    return {"id": str(did), "title": title, "author": author}, None


def _is_empty_copy(html_text):
    """True when the fragment is just Chrome's trailing marker - i.e. Ctrl+A
    copied nothing. Normal for an iframe-only page: the id still arrives in
    the clipboard's address (SourceURL)."""
    t = (html_text or "").replace('<br class="Apple-interchange-newline">', "")
    return not t.strip()


def parse_player_shell(html_text, source_url):
    """The PLAYER'S OWN document (you clicked inside the tease, then copied).
    Ctrl+A then selects the eosscript iframe's content, so there is NO Milovana
    id anywhere - but the player's top bar always carries the tease's real
    title + author (running or not). Verified on live clipboard probes.
    Returns ({'title','author'}, None) or (None, reason)."""
    src = source_url or ""
    if "eosscript.com" not in src and "_authorButton_" not in (html_text or ""):
        return None, "not a player-shell copy"

    def grab(pattern):
        m = re.search(pattern, html_text)
        return strip_tags(m.group(1)).strip() if m else ""

    title = grab(r'class="_tease_[^"]*"[^>]*>([^<]+)<')            # top bar
    author = grab(r'class="_authorButton_[^"]*"[^>]*>([^<]+)<')   # top bar
    if not title:
        title = grab(r"<h1[^>]*>(.+?)</h1>")                      # start screen
    if not author:
        author = grab(r"(?is)<h2[^>]*>\s*by\s+(.+?)</h2>")        # start screen
    if not title or not author:
        return None, "no title/author in the player shell"
    return {"title": title, "author": author}, None


def parse_address_copy(html_text, source_url):
    """An EMPTY Ctrl+A copy whose address is a showtease page: only the id
    survived. That is the normal parent-page copy for player teases (their
    page is a single iframe - nothing else copies); static pages copy WITH
    content, so "empty + showtease address" doubles as a player-type hint.
    Returns ({'id': ...}, None) or (None, reason)."""
    tid = tease_id_from_url(source_url)
    if not tid:
        return None, "no tease id in the page address"
    if not _is_empty_copy(html_text):
        return None, "copy is not empty"
    return {"id": tid}, None


def classify_page(html_text, source_url):
    """A copied page -> payload dict (or None when unrecognized):
       {'kind':'listing','entries':[...]}
       {'kind':'simple','page':{...}}
       {'kind':'player','id':...,'title':...,'author':...}  (parent page, markers survived)
       {'kind':'player_shell','title':...,'author':...}     (copy made inside the player)
       {'kind':'address','id':...}                          (empty parent copy: id only)"""
    if not html_text:
        addr, _reason = parse_address_copy(html_text, source_url)
        if addr:
            return {"kind": "address", "id": addr["id"]}
        return None
    entries = parse_listing(html_text)
    if entries:
        return {"kind": "listing", "entries": entries}
    info, _reason = parse_simple_page(html_text, source_url)
    if info is not None:
        return {"kind": "simple", "page": info}
    if tease_id_from_url(source_url):
        pg, _r2 = parse_player_page(html_text, source_url)
        if pg:
            return {"kind": "player", "id": pg["id"], "title": pg.get("title", ""),
                    "author": pg.get("author", "")}
    shell, _r3 = parse_player_shell(html_text, source_url)
    if shell:
        return {"kind": "player_shell", "title": shell["title"], "author": shell["author"]}
    addr, _r4 = parse_address_copy(html_text, source_url)
    if addr:
        return {"kind": "address", "id": addr["id"]}
    return None
