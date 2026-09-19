# pinterest-dl

Download images from Pinterest pins and multi-pin share links, at the highest
resolution Pinterest serves.

```bash
git clone <this-repo> && cd pinterest-dl
pip install .            # requests is pulled in automatically
pip install ".[browser]" # + Playwright, for multi-pin share links
python pinterest_dl.py "https://www.pinterest.com/pin/<pin-id>/"
# or, once installed:
pinterest-dl "https://www.pinterest.com/pin/<pin-id>/"
```

(Every URL in this README is a placeholder — `<pin-id>`, `<code>`, `<url>` are
never real identifiers.)

## Link types

| Link | Support |
|---|---|
| `pinterest.com/pin/<id>/` | ✅ static (fast, no browser) |
| `pinterest.com/pin/<id>/feedback/?invite_code=…` | ✅ static — `/feedback/` and query params are handled |
| `pinterest.com/pin/some-slug--<id>/` | ✅ static |
| `pin.it/<code>` | ✅ static — short links are resolved, then re-classified |
| `pinterest.com/multi-pin-share/<id>/?invite_code=…` | ✅ automatic browser mode (see below) |

The tool picks its own mode. Links whose pins only exist after JavaScript runs
switch themselves to browser mode; you don't pass a flag.

## Usage

```bash
# one or more URLs
python pinterest_dl.py <url> <url> ...

# a file of URLs, one per line (# comments allowed)
python pinterest_dl.py --input links.txt

# choose an output directory / prefer a smaller size
python pinterest_dl.py -o ~/Pictures/pins -q 736x <url>

# see what would download, without downloading
python pinterest_dl.py --dry-run <url>

# machine-readable output
python pinterest_dl.py --json <url>
```

| Flag | Meaning |
|---|---|
| `-o, --out` | output directory (default `downloads`) |
| `-q, --quality` | `originals` (default), `736x`, `564x`, `474x`, `236x` |
| `--browser` | always render with Chromium (otherwise automatic when needed) |
| `--no-browser` | never render — static only, even for links that need it |
| `--show-browser` | run Chromium visibly (useful if Pinterest shows a login wall) |
| `-j, --jobs` | parallel downloads (default 4) |
| `--max` | cap images per URL |
| `-n, --dry-run` | list URLs without downloading |
| `-f, --force` | overwrite existing files |
| `--json` | print results as JSON |

## Multi-pin share links

A `multi-pin-share` page ships **no pin data** in its HTML — the page knows only
how many pins it holds ("Alice sent you 3 Pins"), and the list arrives via an
XHR after the page hydrates. Its only static image is an `og:image` collage that
**expires after one day**, and it is not the individual pins.

So these links need a real browser. Install Playwright once — after that the
mode switch is automatic:

```bash
pip install playwright && playwright install chromium
python pinterest_dl.py "https://www.pinterest.com/multi-pin-share/…"
```

Browser mode renders the page and captures the JSON that Pinterest loads the
pins from. It reads the `resource_response.data.pins` array specifically, and
takes each pin's `images.orig` — so you get full-resolution originals rather
than the DOM's thumbnails.

Reading the `pins` array matters more than it looks: those same responses also
contain recommendations rails whose `cover_images` are pinimg URLs too. Trusting
any pinimg URL found in the payload pulls in a pin the sender never shared —
which is how a "3 Pins" link yields 4 files. The explicit array is the only
thing that says which pins are actually in the share.

Each downloaded file is named after **its own** pin (`<pin-id>-<index>-<title>`),
not after the page, so a multi-pin share produces one correctly-named file per
pin. Pins that carry no title fall back to the page title.

## How resolution works

Pinterest serves each image at many sizes under the same 32-hex digest:

```
https://i.pinimg.com/736x/36/0c/f1/360cf11a…72f.jpg     <- thumbnail size
https://i.pinimg.com/originals/36/0c/f1/360cf11a…72f.png <- the original
```

Note the extension changes between them (`.jpg` → `.png`), so you cannot simply
swap the size segment. This tool instead reads the original URL from the page's
JSON-LD where possible, and otherwise probes
`originals/<hash>.<ext>` across `jpg/png/webp/gif`, keeping the first response
that is a real image. Because the probe uses a normal `GET`, the winning
response *is* the download — nothing is wasted.

Sources are tiered so a single pin yields that pin's image and not its
neighbours: JSON-LD, `og:image`, and the page's own `images_orig` payloads are
trusted; `<link rel=preload>` and `<img>` tags (which also carry *related* pins)
are consulted only when nothing structured is present.

Downloads are written as `<pin-id>-<title-slug>.<ext>`, skip files that already
exist unless `--force` is given (without hitting the network at all — the
skip is checked before any request), and are validated by magic bytes so an
HTML error page returned with status 200 is never saved.

## Tests

```bash
python3 test_extract.py
ruff check .          # lint (ruff is in the [dev] extra)
```

31 offline tests covering URL parsing, noise rejection, source tiering,
original-extension probing, quality selection, and filename generation. CI
runs the tests plus ruff on Python 3.10 and 3.12.

## Note

For downloading images you have the right to use, subject to Pinterest's Terms
of Service.
