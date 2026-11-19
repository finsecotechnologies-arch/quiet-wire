#!/usr/bin/env python3
"""Quiet Wire: a news-only reader.

Pulls headlines from public RSS feeds (feeds.json), groups the same story
across outlets, ranks stories by how many outlets carry them, and serves one
calm page with no images, no feeds of opinions and no infinite scroll.
Standard library only (Python 3.8+).

    python3 server.py            # http://localhost:8791
    python3 server.py --lan      # also reachable from your phone on the same Wi-Fi
    python3 server.py --check    # fetch once and print what the page would show
    python3 server.py --build site   # write a static copy (page + news.json) for hosting
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import email.utils
import hashlib
import html
import json
import math
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "static", "index.html")
CONFIG = os.path.join(HERE, "feeds.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
WINDOW_HOURS = 36        # ignore anything older than this
PER_FEED_LIMIT = 40      # newest N items per feed, so a busy feed can't flood a section
CACHE_SECONDS = 10 * 60  # reuse fetched news for this long
MIN_FORCED_GAP = 60      # the Refresh button can refetch at most once a minute
TOP_COUNT = 12
SUMMARY_CHARS = 260

FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           b'<rect width="32" height="32" rx="7" fill="#2450b8"/>'
           b'<rect x="7" y="9" width="18" height="3" rx="1.5" fill="#fff"/>'
           b'<rect x="7" y="15" width="14" height="2" rx="1" fill="#fff" opacity=".8"/>'
           b'<rect x="7" y="20" width="16" height="2" rx="1" fill="#fff" opacity=".8"/></svg>')


# --------------------------------------------------------------------------- app icons and manifest

BRAND = (0x24, 0x50, 0xB8)
# Three "wire" lines: (x start, x end, y centre, thickness, opacity) as fractions of the icon,
# kept inside the centre safe zone so Android's round mask doesn't clip them.
ICON_BARS = ((0.28, 0.72, 0.37, 0.085, 1.0), (0.28, 0.61, 0.51, 0.06, 0.82), (0.28, 0.67, 0.63, 0.06, 0.82))


def png_icon(size):
    """Render the app icon as a PNG (full-bleed square; iOS and Android round it themselves)."""
    bars = [(x0 * size, x1 * size, yc * size, h * size / 2, a) for x0, x1, yc, h, a in ICON_BARS]
    plain = bytes([0]) + bytes(BRAND) * size
    raw = bytearray()
    ss = 3  # 3x3 supersampling for smooth edges
    for y in range(size):
        if not any(abs(y + 0.5 - yc) <= r + 1 for _, _, yc, r, _ in bars):
            raw += plain
            continue
        raw.append(0)
        for x in range(size):
            cover = 0.0
            for sy in range(ss):
                py = y + (sy + 0.5) / ss
                for sx in range(ss):
                    px = x + (sx + 0.5) / ss
                    for x0, x1, yc, r, a in bars:
                        dy = py - yc
                        if abs(dy) > r:
                            continue
                        cx = min(max(px, x0 + r), x1 - r)
                        if (px - cx) ** 2 + dy * dy <= r * r:
                            cover += a
                        break
            k = cover / (ss * ss)
            raw += bytes(round(c + (255 - c) * k) for c in BRAND)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def manifest():
    return json.dumps({
        "name": "Quiet Wire",
        "short_name": "Quiet Wire",
        "description": "Headlines only, ranked by how many newsrooms carry them.",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": "#f2f4f7",
        "theme_color": "#2450b8",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, indent=2).encode()


ASSETS = {
    "favicon.svg": ("image/svg+xml", lambda: FAVICON),
    "apple-touch-icon.png": ("image/png", lambda: png_icon(180)),
    "icon-192.png": ("image/png", lambda: png_icon(192)),
    "icon-512.png": ("image/png", lambda: png_icon(512)),
    "manifest.webmanifest": ("application/manifest+json", manifest),
    "robots.txt": ("text/plain; charset=utf-8", lambda: b"User-agent: *\nDisallow: /\n"),
}
_asset_cache = {}


def asset(name):
    if name not in _asset_cache:
        ctype, make = ASSETS[name]
        _asset_cache[name] = (ctype, make())
    return _asset_cache[name]


# --------------------------------------------------------------------------- feeds

def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def localname(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)  # some feeds escape twice (&amp;#8217;)
    return WS_RE.sub(" ", s).strip()


def shorten(s, limit):
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:-–— ")
    return cut + "…"


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        iso = s.replace("Z", "+00:00")
        iso = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], iso)
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except ValueError:
        return None


def url_key(url):
    return url.split("#", 1)[0].split("?", 1)[0].rstrip("/").lower()


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(8_000_000)


def parse_feed(data, feed, section, now, window_hours=WINDOW_HOURS, skip=None):
    data = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if b"<!ENTITY" in data[:4096]:
        raise ValueError("feed declares XML entities; refusing to parse")
    root = ET.fromstring(data)
    cutoff = now - window_hours * 3600
    items = []
    for el in root.iter():
        if localname(el.tag) not in ("item", "entry"):
            continue
        title = link = desc = content = date = guid = ""
        for c in el:
            n = localname(c.tag)
            if n == "title":
                title = "".join(c.itertext())
            elif n == "link":
                href = c.get("href")
                if href:
                    if c.get("rel", "alternate") == "alternate" and not link:
                        link = href.strip()
                elif c.text and not link:
                    link = c.text.strip()
            elif n in ("description", "summary") and not desc:
                desc = "".join(c.itertext())
            elif n in ("encoded", "content") and not content:
                content = "".join(c.itertext())
            elif n in ("pubDate", "published", "date", "issued") and not date:
                date = c.text or ""
            elif n in ("updated", "modified") and not date:
                date = c.text or ""
            elif n in ("guid", "id") and not guid:
                guid = (c.text or "").strip()
        if not link and guid.startswith("http"):
            link = guid
        title = clean_text(title)
        if not title or not link.startswith(("http://", "https://")):
            continue
        if skip is not None and skip.search(title):
            continue
        d = parse_date(date)
        ts = d.timestamp() if d else now
        if ts < cutoff:
            continue
        ts = min(ts, now)  # a few feeds post-date items
        summary = clean_text(desc) or clean_text(content)
        if summary.lower().startswith(title.lower()):
            summary = summary[len(title):].lstrip(" .:-–—|")
        if len(summary.split()) < 6:  # tags or bylines posing as a summary
            summary = ""
        summary = re.sub(r"\s*(Continue reading\.*|Read more\.*|\[…\]|\[\.\.\.\])\s*$", "", summary)
        items.append({
            "title": title,
            "url": link,
            "key": url_key(link),
            "summary": shorten(summary, SUMMARY_CHARS),
            "ts": ts,
            "source": feed["name"],
            "section": section,
        })
    items.sort(key=lambda it: -it["ts"])
    return items[:PER_FEED_LIMIT]


# --------------------------------------------------------------------------- grouping

STOP = set("""
a about above across after again against ahead all almost also am amid among an and another any are around as at
away back be because been before being below between big both but by can cannot could day days did do does doing
done down during each early end even ever every few first five for four from further get gets getting go goes going
good got great had has have having he her here hers him his how however i if in inside into is it its just know
last late latest least less like live look make makes making man many may me might more most much must my near need
new news next no nor not now of off on once one only or other others our out over own part people photo photos plan
plans really report reports said same say says see set she should show shows since six so some still such take takes
taking talk talks than that the their them then there these they thing things this those three through time to
today told too top two under until up update updates us use very video videos vs want wants was watch way ways we
week weeks well were what when where which while who whose why will with without women would year years yet you
your opinion analysis explained explainer exclusive breaking watch listen podcast newsletter india indian world
""".split())


def tokens(text, limit=None):
    t = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"'s\b", "", t).replace("'", "")
    words = re.findall(r"[a-z0-9]+", t)
    if limit:
        words = words[:limit]
    out = set()
    for w in words:
        if len(w) < 3 or w in STOP:
            continue
        if w.isdigit():
            if len(w) == 4 and w[:2] in ("19", "20"):
                continue
            out.add(w)
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w[:6])
    return out


MATCH_THRESHOLD = 0.28


def cluster(items, feed_rank):
    """Group articles that report the same story.

    Each article becomes a TF-IDF vector of its headline (weighted double) and
    the start of its summary, so rare words like names and places count most.
    An article joins the story whose combined vector it is closest to, which
    stops one roundup headline from chaining two unrelated stories together.
    """
    n = len(items)
    df = Counter()
    for it in items:
        it["tt"] = tokens(it["title"])
        it["st"] = tokens(it["summary"], 35) - it["tt"]
        df.update(it["tt"] | it["st"])
    idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}
    for it in items:
        v = {w: 2.0 * idf[w] for w in it["tt"]}
        for w in it["st"]:
            v[w] = v.get(w, 0.0) + idf[w]
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        it["vec"] = {w: x / norm for w, x in v.items()}

    groups, sums, norms, index = [], [], [], {}
    for it in sorted(items, key=lambda it: (feed_rank[it["feed"]], -it["ts"])):
        cands = set()
        for w in it["tt"]:
            if idf[w] >= 2.0:  # skip headline words that appear almost everywhere
                cands.update(index.get(w, ()))
        best, best_cos = None, MATCH_THRESHOLD
        for gi in cands:
            g = sums[gi]
            cos = sum(x * g.get(w, 0.0) for w, x in it["vec"].items()) / norms[gi]
            if cos >= best_cos:
                best, best_cos = gi, cos
        if best is None:
            best = len(groups)
            groups.append([])
            sums.append({})
            norms.append(1.0)
        groups[best].append(it)
        g = sums[best]
        for w, x in it["vec"].items():
            g[w] = g.get(w, 0.0) + x
        norms[best] = math.sqrt(sum(x * x for x in g.values()))
        for w in it["tt"]:
            index.setdefault(w, set()).add(best)
    return groups


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def freshness(age_h, half_life):
    return 2.0 * 0.5 ** (age_h / half_life)


def build_story(group, feed_rank, now):
    by_priority = sorted(group, key=lambda it: (feed_rank[it["feed"]], -it["ts"]))
    lead = next((it for it in by_priority if it["summary"]), by_priority[0])
    by_source = {}
    for it in by_priority:
        by_source.setdefault(it["source"], it)
    others = sorted((it for src, it in by_source.items() if src != lead["source"]), key=lambda it: -it["ts"])
    n = len(by_source)
    latest = max(it["ts"] for it in group)
    earliest = min(it["ts"] for it in group)

    # Which tabs the story belongs in: its main section, plus any other
    # section where at least two of that section's outlets carry it.
    local = {}
    for it in group:
        for sec in [it["section"], *it.get("also_sections", ())]:
            local.setdefault(sec, set()).add(it["source"])
    home = max(local, key=lambda sec: (len(local[sec]), sec == lead["section"]))
    sections = [sec for sec in local if sec == home or len(local[sec]) >= 2]

    return {
        "id": hashlib.sha1(lead["key"].encode()).hexdigest()[:12],
        "title": lead["title"],
        "summary": lead["summary"],
        "url": lead["url"],
        "source": lead["source"],
        "time": iso(latest),
        "first_seen": iso(earliest),
        "n": n,
        "sections": sections,
        "others": [{"source": it["source"], "title": it["title"], "url": it["url"], "time": iso(it["ts"])}
                   for it in others[:10]],
        "urls": sorted({it["url"] for it in group}),
        "_local": {sec: len(local[sec]) for sec in sections},
        "_age_h": max(0.0, (now - latest) / 3600),
    }


def diversify(stories, score):
    """Rank by score, nudging down runs of stories led by the same outlet."""
    pool = sorted(stories, key=lambda s: -score(s))
    out, used = [], Counter()
    while pool:
        best = max(range(len(pool)), key=lambda i: score(pool[i]) - 0.15 * used[pool[i]["source"]])
        s = pool.pop(best)
        out.append(s)
        used[s["source"]] += 1
    return out


def build():
    cfg = load_config()
    now = time.time()
    skip_patterns = cfg.get("skip_title_patterns") or []
    skip = re.compile("|".join(f"(?:{p})" for p in skip_patterns), re.I) if skip_patterns else None
    jobs, feed_rank, windows = [], {}, {}
    for sec in cfg["sections"]:
        windows[sec["id"]] = float(sec.get("window_hours", WINDOW_HOURS))
        for f in sec["feeds"]:
            fid = f"{sec['id']}:{f['url']}"
            feed_rank[fid] = len(feed_rank)
            jobs.append((fid, f, sec["id"]))

    def run(job):
        fid, f, sec_id = job
        try:
            items = parse_feed(fetch(f["url"]), f, sec_id, now, windows[sec_id], skip)
            for it in items:
                it["feed"] = fid
            return fid, f, sec_id, items, None
        except Exception as e:  # one broken feed must not break the page
            return fid, f, sec_id, [], f"{type(e).__name__}: {e}"[:160]

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(run, jobs))

    feeds_status, items, seen = [], [], {}
    for fid, f, sec_id, its, err in results:
        feeds_status.append({"name": f["name"], "section": sec_id, "ok": err is None,
                             "count": len(its), "error": err})
        for it in its:
            prev = seen.get(it["key"])
            if prev is not None:  # same article in two feeds (e.g. BBC World and BBC India)
                if it["section"] != prev["section"]:
                    prev.setdefault("also_sections", set()).add(it["section"])
                continue
            seen[it["key"]] = it
            items.append(it)

    stories = [build_story(g, feed_rank, now) for g in cluster(items, feed_rank)]

    def global_score(st):
        return (min(st["n"], 8) - 1) + freshness(st["_age_h"], 8)

    sections = []
    for sec in cfg["sections"]:
        sid = sec["id"]
        half_life = windows[sid] / 4.5

        def score(st, sid=sid, half_life=half_life):
            loc = st["_local"][sid]
            return (loc - 1) + 0.3 * (st["n"] - loc) + freshness(st["_age_h"], half_life)

        mine = [st for st in stories if sid in st["sections"]]
        names = []
        for f in sec["feeds"]:
            if f["name"] not in names:
                names.append(f["name"])
        sections.append({"id": sid, "label": sec["label"], "sources": names,
                         "window_hours": windows[sid],
                         "order": [st["id"] for st in diversify(mine, score)]})

    ranked = sorted(stories, key=lambda st: -global_score(st))
    top = [st for st in ranked if st["n"] >= 3][:TOP_COUNT]
    if len(top) < 8:
        top += [st for st in ranked if st["n"] == 2][: 8 - len(top)]

    for st in stories:
        st.pop("_local", None)
        st.pop("_age_h", None)

    return {
        "generated_at": iso(now),
        "window_hours": WINDOW_HOURS,
        "outlets": len({f["name"] for sec in cfg["sections"] for f in sec["feeds"]}),
        "sections": sections,
        "top": [st["id"] for st in top],
        "stories": stories,
        "feeds": feeds_status,
    }


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.body = b""
        self.fetched = 0.0

    def get(self, force=False):
        with self.lock:
            age = time.time() - self.fetched
            if self.data is None or age > CACHE_SECONDS or (force and age > MIN_FORCED_GAP):
                self.data = build()
                self.body = json.dumps(self.data, ensure_ascii=False).encode("utf-8")
                self.fetched = time.time()
            return self.body


STORE = Store()


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "QuietWire/1.0"

    def send(self, code, ctype, body, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        try:
            if path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    self.send(200, "text/html; charset=utf-8", f.read())
            elif path == "/api/news":
                body = STORE.get(force="fresh=1" in query.split("&"))
                self.send(200, "application/json; charset=utf-8", body)
            elif path.lstrip("/") in ASSETS:
                ctype, body = asset(path.lstrip("/"))
                self.send(200, ctype, body, cache="max-age=86400")
            else:
                self.send(404, "text/plain; charset=utf-8", b"Not found")
        except Exception as e:
            self.send(500, "text/plain; charset=utf-8", f"Server error: {e}".encode())

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        if "/api/news" in (args[0] if args else ""):
            sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def check():
    t = time.time()
    data = json.loads(STORE.get())
    print(f"Built in {time.time() - t:.1f}s · {len(data['stories'])} stories from {data['outlets']} outlets\n")
    bad = [f for f in data["feeds"] if not f["ok"]]
    for f in data["feeds"]:
        print(f"  {'ok ' if f['ok'] else 'ERR'} {f['section']:9} {f['name']:18} {f['count']:3}  {f['error'] or ''}")
    by_id = {s["id"]: s for s in data["stories"]}
    print("\nTop stories:")
    for sid in data["top"]:
        s = by_id[sid]
        print(f"  [{s['n']}] {s['title'][:90]}  ({', '.join([s['source']] + [o['source'] for o in s['others']])})")
    for sec in data["sections"]:
        print(f"\n{sec['label']} ({len(sec['order'])} stories), first 6:")
        for sid in sec["order"][:6]:
            s = by_id[sid]
            print(f"  [{s['n']}] {s['source']:16} {s['title'][:88]}")
    biggest = sorted(data["stories"], key=lambda s: -len(s["urls"]))[:3]
    print("\nLargest groups (sanity check):")
    for s in biggest:
        print(f"  {len(s['urls'])} articles / {s['n']} outlets: {s['title'][:80]}")
    return 1 if len(bad) == len(data["feeds"]) else 0


DATA_META_LOCAL = '<meta name="qw-data" content="/api/news">'
DATA_META_STATIC = '<meta name="qw-data" content="news.json">'


def build_site(out):
    """Write a static copy of the reader: the page, today's news.json, icons and manifest."""
    t = time.time()
    data = build()
    ok = sum(1 for f in data["feeds"] if f["ok"])
    if not ok:
        print("No feed responded; not writing an empty site.")
        return 1
    with open(INDEX, encoding="utf-8") as f:
        page = f.read()
    if DATA_META_LOCAL not in page:
        print("static/index.html is missing the qw-data meta tag.")
        return 1
    files = {
        "index.html": page.replace(DATA_META_LOCAL, DATA_META_STATIC).encode("utf-8"),
        "news.json": json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        ".nojekyll": b"",
    }
    for name in ASSETS:
        files[name] = asset(name)[1]
    os.makedirs(out, exist_ok=True)
    for name, body in files.items():
        with open(os.path.join(out, name), "wb") as f:
            f.write(body)
    print(f"Built {out} in {time.time() - t:.1f}s: {len(data['stories'])} stories, "
          f"{ok}/{len(data['feeds'])} feeds responded")
    for f in data["feeds"]:
        if not f["ok"]:
            print(f"  didn't respond: {f['name']} ({f['section']}): {f['error']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Quiet Wire news reader")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--lan", action="store_true", help="listen on your network so a phone on the same Wi-Fi can open it")
    ap.add_argument("--check", action="store_true", help="fetch once, print a summary and exit")
    ap.add_argument("--build", metavar="DIR", help="write a static copy of the reader into DIR and exit")
    args = ap.parse_args()
    if args.check:
        sys.exit(check())
    if args.build:
        sys.exit(build_site(args.build))

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    httpd = ThreadingHTTPServer((host, args.port), Handler)
    threading.Thread(target=STORE.get, daemon=True).start()  # warm the cache
    print(f"Quiet Wire is running at http://localhost:{args.port}")
    if args.lan:
        ip = lan_ip()
        if ip:
            print(f"On your phone (same Wi-Fi): http://{ip}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
