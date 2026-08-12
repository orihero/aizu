#!/usr/bin/env python3
"""Inline the AIZU brand webfonts AND raster assets into a self-contained deck.

Extends tools/embed_fonts.py with a second marker family so a deck can carry a
real product screenshot without an external request (published Artifacts run
under a CSP that blocks every host, and a deck that loses its screenshot in the
room is worse than one that never had it).

Markers, replaced literally:
    /*__AIZU_FONTS__*/          -> @font-face rules, base64 woff2
    __ASSET:<name>__            -> a data: URI for ASSETS[<name>]

Usage:
    python tools/embed_assets.py src/deck-pitch.html
"""

from __future__ import annotations

import base64
import mimetypes
import sys
from pathlib import Path

FONT_MARKER = "/*__AIZU_FONTS__*/"

ROOT = Path(__file__).resolve().parents[3]
FONT_DIR = ROOT / "admin-panel" / "public" / "landing" / "fonts"
CAP_DIR = ROOT / "marketing" / "videos" / "madison" / "assets" / "capture"
TEAM_DIR = Path(__file__).resolve().parents[1] / "assets" / "team"
OUT_DIR = Path(__file__).resolve().parents[1] / "dist"

FACES = [
    ("Inter Tight", "100 900", "inter-tight-latin.woff2"),
    ("JetBrains Mono", "400", "jetbrains-mono-400-latin.woff2"),
    ("JetBrains Mono", "500", "jetbrains-mono-500-latin.woff2"),
]

# Real product captures, not mockups. Both are genuine screens of the shipped
# panel carrying demo/seed data - the deck labels them as such.
ASSETS = {
    "dashboard": CAP_DIR / "dashboard.png",
    "reports": CAP_DIR / "reports.png",
    # Team headshots, cropped square and face-centred so the circular avatar
    # frames the same amount of face for each of the three.
    "team_abdumutal": TEAM_DIR / "abdumutal.jpg",
    "team_sevinch": TEAM_DIR / "sevinch.jpg",
    "team_nurulloh": TEAM_DIR / "nurulloh.jpg",
}


def font_css() -> str:
    blocks = []
    for family, weight, filename in FACES:
        b64 = base64.b64encode((FONT_DIR / filename).read_bytes()).decode("ascii")
        blocks.append(
            f"@font-face{{font-family:'{family}';font-style:normal;"
            f"font-weight:{weight};font-display:block;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}"
        )
    return "\n".join(blocks)


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
{fragment}
</body>
</html>
"""


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    css = font_css()
    uris = {k: data_uri(p) for k, p in ASSETS.items() if p.exists()}
    missing = [k for k in ASSETS if k not in uris]
    if missing:
        print(f"!! missing assets, markers left intact: {', '.join(missing)}")

    art_dir = OUT_DIR / "artifact"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    art_dir.mkdir(parents=True, exist_ok=True)

    for arg in argv:
        src = Path(arg).resolve()
        html = src.read_text(encoding="utf-8")
        if FONT_MARKER not in html:
            print(f"!! {src.name}: {FONT_MARKER} not found")
        fragment = html.replace(FONT_MARKER, css)
        for name, uri in uris.items():
            fragment = fragment.replace(f"__ASSET:{name}__", uri)

        (art_dir / src.name).write_text(fragment, encoding="utf-8")
        out = OUT_DIR / src.name
        out.write_text(SHELL.format(fragment=fragment), encoding="utf-8")
        print(f"ok {src.name} -> dist/{out.name} ({out.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
