#!/usr/bin/env python3
"""
pinterest-dl — download images from Pinterest pins and multi-pin share links.

Usage:
    python pinterest_dl.py <url> [<url> ...]
    python pinterest_dl.py --input links.txt

Single pins are resolved statically (fast, no browser). Multi-pin share links
are rendered client-side by Pinterest and carry no pin data in their HTML, so
those switch themselves to browser mode, which needs Playwright:

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("This tool needs 'requests':  pip install requests")

CDN = "https://i.pinimg.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Pinterest serves the same image at many sizes. Bigger is not alphabetical
# (600x315 vs 564x), so ranking is explicit.
SIZE_PREF = [
    "originals",
    "orig",
    "750x",
    "736x",
    "600x315",
    "564x",
    "474x",
    "400x300",
    "236x",
    "200x150",
    "170x",
    "136x136",
    "75x75",
    "60x60",
    "50x50",
]
IMAGE_EXTS = ("jpg", "png", "webp", "gif")

# i.pinimg.com/<size>/<aa>/<bb>/<cc>/<32-hex>.<ext>
PINIMG_RE = re.compile(
    r"https://i\.pinimg\.com/(?P<size>[0-9a-z]+)/"
    r"(?P<path>(?:[0-9a-f]{2}/){3}(?P<digest>[0-9a-f]{32}))\.(?P<ext>jpg|png|webp|gif)",
    re.I,
)
# The pin's own original image, inside a JSON payload.
IMAGES_ORIG_RE = re.compile(
    r'"images_orig"\s*:\s*\{[^{}]*?"url"\s*:\s*"(https://i\.pinimg\.com/[^"]+)"'
)
LD_JSON_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
PRELOAD_RE = re.compile(r'<link\b[^>]*rel=["\']preload["\'][^>]*>', re.I)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
ATTR_RE = r"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)')"""

# Magic bytes, so a 200 response carrying an HTML error page is rejected.
MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
}


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


@dataclass
class Image:
    """A downloadable image: its canonical (highest-res) URL plus the hash path
    that lets us try to upgrade it to /originals/."""

    url: str
    digest: str = ""
    hash_path: str = ""
    ext: str = ""
    size: str = ""
    source: str = ""
    # set when the image came from a pin-list payload, so it can be named
    # after its own pin rather than after the page
    pin_id: str = ""
    title: str = ""
    # every variant seen for this digest, best first
    variants: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.digest or self.url


@dataclass
class Page:
    url: str
    kind: str  # "pin" | "multi-pin-share" | "unknown"
    pin_id: str = ""
    title: str = ""
    images: list[Image] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# url handling
# --------------------------------------------------------------------------- #


def classify(url: str) -> tuple[str, str, str]:
    """Return (kind, pin_id, canonical_url) for a Pinterest URL."""
    p = urlparse(url if "://" in url else "https://" + url)
    path = unquote(p.path)

    m = re.search(r"/pin/([^/]+)", path)
    if m:
        seg = m.group(1)
        idm = re.search(r"(\d{8,})$", seg)  # plain id, or slug--id
        pin_id = idm.group(1) if idm else seg
        # /pin/<id>/feedback/ etc. all resolve from the bare pin path
        return "pin", pin_id, f"https://www.pinterest.com/pin/{pin_id}/"

    m = re.search(r"/multi-pin-share/([^/]+)", path)
    if m:
        # Keep the query string: these links need their invite_code.
        return "multi-pin-share", m.group(1), url

    return "unknown", "", url


def resolve_short_url(session: requests.Session, url: str) -> str:
    """Follow pin.it/<code> style redirects."""
    if "pin.it" not in url:
        return url
    try:
        r = session.get(url, allow_redirects=True, timeout=25, stream=True)
        final = r.url
        r.close()
        return final
    except requests.RequestException:
        return url


# --------------------------------------------------------------------------- #
# static extraction
# --------------------------------------------------------------------------- #


def _attr(tag: str, name: str) -> str:
    m = re.search(ATTR_RE.format(name=name), tag, re.I | re.S)
    if not m:
        return ""
    return m.group(1) if m.group(1) is not None else m.group(2)


def _images_from_jsonld(html: str) -> list[str]:
    out: list[str] = []
    for block in LD_JSON_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            img = node.get("image")
            if isinstance(img, str):
                out.append(img)
            elif isinstance(img, dict) and isinstance(img.get("url"), str):
                out.append(img["url"])
            elif isinstance(img, list):
                for it in img:
                    if isinstance(it, str):
                        out.append(it)
                    elif isinstance(it, dict) and isinstance(it.get("url"), str):
                        out.append(it["url"])
    return out


def _images_from_meta(html: str) -> list[str]:
    """og:image / twitter:image — reliable, and unambiguously this page's pin."""
    out: list[str] = []
    for tag in META_TAG_RE.findall(html):
        prop = (_attr(tag, "property") or _attr(tag, "name")).lower()
        if prop in (
            "og:image",
            "og:image:url",
            "og:image:secure_url",
            "twitter:image",
            "twitter:image:src",
        ):
            content = _attr(tag, "content")
            if content:
                out.append(content)
    return out


def _images_from_preload(html: str) -> list[str]:
    """<link rel=preload as=image> — weak: also preloads related pins."""
    out: list[str] = []
    for tag in PRELOAD_RE.findall(html):
        if "image" in _attr(tag, "as").lower():
            href = _attr(tag, "href")
            if href:
                out.append(href)
    return out


def _images_from_tags(html: str) -> list[str]:
    """Images referenced by <img> tags — the last-resort static source."""
    out: list[str] = []
    for tag in IMG_TAG_RE.findall(html):
        for name in ("src", "data-src"):
            v = _attr(tag, name)
            if v.startswith("http"):
                out.append(v)
        srcset = _attr(tag, "srcset")
        if srcset:
            for part in srcset.split(","):
                cand = part.strip().split(" ")[0]
                if cand.startswith("http"):
                    out.append(cand)
    return out


def _norm(url: str) -> str:
    return url.replace("&amp;", "&").replace("\\/", "/").strip()


def _is_noise(url: str) -> bool:
    """Reject non-pin assets: uploads, board thumbnails, expiring share covers,
    and Pinterest's own CSS background images."""
    low = url.lower()
    if any(s in low for s in ("/upload/", "retention-1days", "/videos/", "mps_")):
        return True
    # Real pin images are always /<size>/<aa>/<bb>/<cc>/<32 hex>.<ext>
    return not (
        re.match(rf"^{re.escape(CDN)}/[0-9a-z]+/(?:[0-9a-f]{{2}}/){{3}}[0-9a-f]{{32}}\.", low)
    )


def collect_images(html: str, extra_json: list[str] | None = None) -> list[Image]:
    """Extract candidate pin images, best-resolution URL per digest.

    Sources are tiered. The strong tier identifies *this* pin's own image(s);
    the weak tier (preload links, <img> tags) also picks up related pins, so it
    is only consulted when the strong tier finds nothing.
    """
    strong: dict[str, Image] = {}
    weak: dict[str, Image] = {}
    seen: set[str] = set()

    # Most authoritative: an explicit pin list, each image tagged with its own
    # pin id and title. Used as-is, so nothing neighbouring sneaks in.
    listed: dict[str, Image] = {}
    for text in extra_json or []:
        for img in _pins_from_api(text):
            listed.setdefault(img.key, img)
    if listed:
        return list(listed.values())

    # this pin's own image, plus any extra images belonging to it (story pins)
    for text in [html, *(extra_json or [])]:
        for u in _images_from_jsonld(text):
            _absorb(strong, seen, u, "json-ld")
        for u in _images_from_meta(text):
            _absorb(strong, seen, u, "meta")
        for m in IMAGES_ORIG_RE.finditer(text):
            _absorb(strong, seen, m.group(1), "originals")
        if text is not html:
            for m in PINIMG_RE.finditer(text):
                _absorb(strong, seen, m.group(0), "api")

    if not strong:
        for u in _images_from_preload(html):
            _absorb(weak, seen, u, "preload")
        for u in _images_from_tags(html):
            _absorb(weak, seen, u, "img-tag")

    return list(strong.values()) or list(weak.values())


def _image_from_url(raw_url: str, source: str) -> Image | None:
    """Build an Image from a CDN URL, or None if it isn't a pin image."""
    url = _norm(raw_url)
    if not url.startswith("http") or "pinimg.com" not in url or _is_noise(url):
        return None
    m = PINIMG_RE.match(url)
    if not m:
        return Image(url=url, source=source)
    return Image(
        url=url,
        digest=m.group("digest").lower(),
        hash_path=m.group("path").lower(),
        ext=m.group("ext").lower(),
        size=m.group("size").lower(),
        source=source,
    )


def _absorb(bucket: dict[str, Image], seen: set[str], raw_url: str, source: str) -> None:
    """Add a URL to a bucket, keyed by image digest, keeping the best size."""
    img = _image_from_url(raw_url, source)
    if img is None or (img.key in seen and img.key not in bucket):
        return
    if img.key not in bucket:
        bucket[img.key] = img
        seen.add(img.key)
        return
    existing = bucket[img.key]
    if not img.size:
        return
    existing.variants[img.size] = img.url
    if _rank(img.size) < _rank(existing.size) or not existing.url:
        existing.url, existing.size = img.url, img.size
        existing.ext, existing.hash_path = img.ext, img.hash_path


PIN_IMAGE_KEYS = ("orig", "originals", "736x", "564x", "474x", "236x", "170x")


def _pin_title(pin: dict) -> str:
    for key in ("title", "grid_title", "alt_text"):
        val = pin.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict) and isinstance(val.get("text"), str):
            if val["text"].strip():
                return val["text"].strip()
    return ""


def _pins_from_api(text: str) -> list[Image]:
    """Pull the authoritative pin list out of a captured resource response.

    A multi-pin share (and every recommendations rail) responds with
    `resource_response.data.pins`, each pin carrying `images.orig`. Trusting
    that array keeps related and recommended pins out of the results — generic
    URL scanning would otherwise sweep up `cover_images` from neighbouring
    modules on the same page.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []

    pins: list[dict] = []

    def visit(node) -> None:
        if isinstance(node, dict):
            found = node.get("pins")
            if isinstance(found, list):
                pins.extend(p for p in found if isinstance(p, dict))
            for val in node.values():
                visit(val)
        elif isinstance(node, list):
            for val in node:
                visit(val)

    visit(data)

    out: list[Image] = []
    for pin in pins:
        images = pin.get("images")
        if not isinstance(images, dict):
            continue
        for key in PIN_IMAGE_KEYS:
            entry = images.get(key)
            url = entry.get("url") if isinstance(entry, dict) else entry
            if not isinstance(url, str) or not url:
                continue
            img = _image_from_url(url, "api")
            if img is None:
                continue
            img.pin_id = str(pin.get("id") or "")
            img.title = _pin_title(pin)
            out.append(img)
            break
    return out


def _rank(size: str) -> int:
    try:
        return SIZE_PREF.index(size)
    except ValueError:
        return len(SIZE_PREF)


def page_title(html: str) -> str:
    for block in LD_JSON_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict):
                for k in ("headline", "name", "description"):
                    if isinstance(node.get(k), str) and node[k].strip():
                        return node[k].strip()
    for tag in META_TAG_RE.findall(html):
        if (_attr(tag, "property") or "").lower() == "og:title":
            t = _attr(tag, "content")
            if t:
                return t
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s*\|\s*Pinterest\s*$", "", m.group(1).strip()) if m else ""


# --------------------------------------------------------------------------- #
# browser extraction (multi-pin share, and anything JS-rendered)
# --------------------------------------------------------------------------- #

BROWSER_HINT = (
    "This link needs a real browser to resolve.\n"
    "Install Playwright once, then re-run:\n"
    "    pip install playwright && playwright install chromium"
)


def print_browser_hint() -> None:
    lines = BROWSER_HINT.splitlines()
    print(f"    ! {lines[0]}")
    for line in lines[1:]:
        print(f"      {line}")


def browser_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def needs_browser(kind: str) -> bool:
    """Link kinds whose pins only exist after the page runs its JavaScript."""
    return kind == "multi-pin-share"


def fetch_via_browser(url: str, timeout: int, headless: bool = True):
    """Render with Chromium. Returns (html, captured_json_bodies, img_srcs).

    Multi-pin share pages hold no pin data in their HTML — the list arrives via
    an XHR after hydration — so we capture JSON responses as well as the DOM.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(BROWSER_HINT)

    html = ""
    captured: list[str] = []
    srcs: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 960})
        page = ctx.new_page()

        def on_response(resp):
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                if int((resp.headers or {}).get("content-length") or 0) > 12_000_000:
                    return
                body = resp.text()
                if "pinimg.com" in body:
                    captured.append(body)
            except Exception:
                pass  # a failed capture must never break the run

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception as exc:
            print(f"  ! page load slow ({type(exc).__name__}); continuing", file=sys.stderr)

        # Pinterest streams pins in as you scroll.
        try:
            page.wait_for_selector('img[src*="pinimg.com"]', timeout=15000)
        except Exception:
            pass
        for _ in range(6):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(700)
        page.wait_for_timeout(1500)

        try:
            html = page.content()
            srcs = [
                s
                for s in page.eval_on_selector_all(
                    "img", "els => els.map(e => e.currentSrc || e.src || '')"
                )
                if isinstance(s, str) and "pinimg.com" in s
            ]
            for s in page.eval_on_selector_all(
                "img", "els => els.map(e => e.getAttribute('srcset') || '')"
            ):
                for part in (s or "").split(","):
                    cand = part.strip().split(" ")[0]
                    if cand.startswith("http") and "pinimg.com" in cand:
                        srcs.append(cand)
        except Exception as exc:
            # an extraction failure must not lose the captured XHR payloads
            print(
                f"  ! extraction from rendered page failed ({type(exc).__name__})", file=sys.stderr
            )
        finally:
            browser.close()

    return html, captured, srcs


# --------------------------------------------------------------------------- #
# downloading
# --------------------------------------------------------------------------- #


def _looks_like_image(blob: bytes) -> bool:
    if blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"


def _ext_for(blob: bytes, fallback: str) -> str:
    for magic, ext in MAGIC.items():
        if blob.startswith(magic):
            return ext
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return fallback or "jpg"


def candidate_urls(img: Image, quality: str) -> list[str]:
    """Ordered list of URLs to try. For 'originals' we probe extensions because
    the original's extension often differs from the thumbnail's
    (736x/....jpg is frequently originals/....png)."""
    urls: list[str] = []

    def push(u: str) -> None:
        if u and u not in urls:
            urls.append(u)

    if quality == "originals" and img.hash_path:
        exts = ([img.ext] if img.ext in IMAGE_EXTS else []) + [
            e for e in IMAGE_EXTS if e != img.ext
        ]
        for e in exts:
            push(f"{CDN}/originals/{img.hash_path}.{e}")
    elif quality != "originals":
        # honour the requested size: that exact size, then anything larger,
        # before falling back to whatever else the page offered
        want = _rank(quality)
        for size in sorted(img.variants, key=_rank):
            if _rank(size) >= want:
                push(img.variants[size])
    if img.url:
        push(img.url)
    for size in sorted(img.variants, key=_rank):  # last resort: best first
        push(img.variants[size])
    return urls


def download(
    session: requests.Session,
    img: Image,
    dest_dir: Path,
    quality: str,
    pin_id: str,
    title: str,
    index: int,
    total: int,
    force: bool,
    retries: int = 3,
) -> Path | None:
    # a pin-list image is named after its own pin, not the page it was on
    pid = img.pin_id or pin_id
    name_title = img.title or title

    stem = pid or img.digest[:16] or "image"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name_title).strip("-")[:60]
    if total > 1:
        stem = f"{stem}-{index:02d}"
    if slug:
        stem = f"{stem}-{slug}"

    # Cheap re-run: the response's magic bytes decide the final extension, so
    # before spending any requests, see whether a file with a candidate
    # extension is already on disk. (A previous run at a smaller -q counts as
    # "have it" too; --force re-downloads at the requested quality.)
    if not force:
        for url in candidate_urls(img, quality):
            out = dest_dir / f"{stem}.{url.rsplit('.', 1)[-1].lower()}"
            if out.exists():
                print(f"    · exists  {out.name}")
                return out

    for url in candidate_urls(img, quality):
        for attempt in range(retries):
            try:
                r = session.get(
                    url, timeout=30, headers={**HEADERS, "Referer": "https://www.pinterest.com/"}
                )
                if r.status_code in (403, 404, 410):
                    break  # wrong extension guess or gone — next URL
                if r.status_code != 200:
                    if attempt == retries - 1:
                        break
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if not _looks_like_image(r.content):
                    break
                ext = _ext_for(r.content, img.ext)
                out = dest_dir / f"{stem}.{ext}"
                if out.exists() and not force:
                    print(f"    · exists  {out.name}")
                    return out
                out.write_bytes(r.content)
                kb = len(r.content) / 1024
                print(f"    ✓ {out.name}  ({kb:,.0f} KB)")
                return out
            except requests.RequestException:
                if attempt == retries - 1:
                    break
                time.sleep(1.5 * (attempt + 1))
    print(f"    ✗ failed   {img.url}")
    return None


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def process(
    session: requests.Session,
    raw_url: str,
    args: argparse.Namespace,
) -> list[Path]:
    url = resolve_short_url(session, raw_url.strip())
    kind, pin_id, canonical = classify(url)
    print(f"\n▸ {raw_url}")
    if url.strip() != raw_url.strip():
        print(f"    · resolves to {url}")

    if kind == "unknown":
        print("    ! not a Pinterest pin or multi-pin-share URL")
        return []

    html = ""
    captured: list[str] = []
    dom_srcs: list[str] = []

    # Switch modes by itself: some links simply have no pin data in their HTML.
    required = needs_browser(kind)
    use_browser = args.browser or (required and not args.no_browser)

    if use_browser:
        if not browser_available():
            print_browser_hint()
            return []
        if required and not args.browser:
            print("    · this link loads its pins with JavaScript — using browser mode")
        print("    rendering with Chromium…")
        html, captured, dom_srcs = fetch_via_browser(canonical, args.timeout, not args.show_browser)
    elif required:
        print("    ! --no-browser set; this link's pins will not be found")

    if not html:
        r = session.get(canonical, timeout=args.timeout, headers=HEADERS)
        r.raise_for_status()
        html = r.text

    images = collect_images(html, extra_json=captured)
    if not images:
        # browser-only last resort: the rendered DOM's thumbnails
        known: set[str] = set()
        for s in dom_srcs:
            for i in collect_images(f'<img src="{s}">'):
                if i.key not in known:
                    images.append(i)
                    known.add(i.key)

    title = page_title(html)
    if not title and captured:
        title = page_title(captured[0])
    if not images:
        print("    ! no images found")
        return []

    if args.max:
        images = images[: args.max]
    if args.dry_run:
        print(f"    {len(images)} image(s):")
        for i in images:
            print(f"      [{i.source:7}] {candidate_urls(i, args.quality)[0]}")
        return []

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [
            pool.submit(
                download,
                session,
                img,
                dest,
                args.quality,
                pin_id,
                title,
                n,
                len(images),
                args.force,
            )
            for n, img in enumerate(images, 1)
        ]
        for f in concurrent.futures.as_completed(futures):
            got = f.result()
            if got:
                saved.append(got)
    return saved


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pinterest-dl",
        description="Download images from Pinterest pins and multi-pin share links.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  python pinterest_dl.py "https://www.pinterest.com/pin/<pin-id>/"\n'
            '  python pinterest_dl.py "https://www.pinterest.com/multi-pin-share/<id>/?invite_code=<code>"\n'
            "  python pinterest_dl.py --input links.txt --out ~/Pictures/pins\n"
            "\n"
            "URLs above are placeholders; supply your own links.\n"
        ),
    )
    p.add_argument("urls", nargs="*", help="one or more Pinterest URLs")
    p.add_argument("-i", "--input", help="file of URLs, one per line")
    p.add_argument(
        "-o", "--out", default=".", help="output directory (default: current directory)"
    )
    p.add_argument(
        "-q",
        "--quality",
        default="originals",
        choices=["originals", "736x", "564x", "474x", "236x"],
        help="preferred resolution; falls back to smaller if unavailable (default: originals)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--browser",
        action="store_true",
        help="always render with Chromium (auto for links that need it)",
    )
    mode.add_argument(
        "--no-browser",
        action="store_true",
        help="never use a browser, even for links that need one",
    )
    p.add_argument(
        "--show-browser", action="store_true", help="run Chromium visibly instead of headless"
    )
    p.add_argument("-j", "--jobs", type=int, default=4, help="parallel downloads (default: 4)")
    p.add_argument("--max", type=int, default=0, help="cap images per URL (0 = all)")
    p.add_argument("--timeout", type=int, default=40, help="request timeout, seconds")
    p.add_argument("-n", "--dry-run", action="store_true", help="list what would download")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing files")
    p.add_argument("--json", action="store_true", help="print results as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    urls = list(args.urls)
    if args.input:
        try:
            lines = Path(args.input).read_text(encoding="utf-8").splitlines()
            urls += [line.strip() for line in lines if line.strip() and not line.startswith("#")]
        except OSError as exc:
            sys.exit(f"cannot read {args.input}: {exc}")
    if not urls:
        build_parser().print_help()
        return 2

    session = requests.Session()
    session.headers.update(HEADERS)

    saved: list[str] = []
    for url in urls:
        try:
            saved += [str(p) for p in process(session, url, args)]
        except requests.HTTPError as exc:
            print(f"    ! HTTP error: {exc}")
        except requests.RequestException as exc:
            print(f"    ! network error: {exc}")

    if args.json:
        print(json.dumps({"downloaded": saved}, indent=2))
    else:
        dest = Path(args.out).resolve()
        print(f"\n{len(saved)} file(s) saved to {dest}")
    # an empty --dry-run is a successful listing, not a failure
    return 0 if (saved or args.dry_run) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
