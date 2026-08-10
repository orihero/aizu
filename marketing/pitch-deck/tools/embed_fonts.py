#!/usr/bin/env python3
"""Inline the AIZU brand webfonts into a self-contained deck HTML file.

Each deck is authored under src/ as a document FRAGMENT — <title>, <style>,
the slides and the shell <script>, with no <!doctype>/<html>/<head>/<body>
wrapper — because that is the shape the Artifact publisher wants (it supplies
the skeleton itself). Each fragment contains the literal marker

    /*__AIZU_FONTS__*/

which this script replaces with @font-face rules whose src is a base64 data:
URI, so the built deck renders in the real brand type with zero external
requests (required: published Artifacts run under a CSP that blocks every
external host).

Two builds come out of one source, because the two destinations need opposite
things:

    dist/<name>.html           standalone document, doctype-wrapped.
                               Open this locally; a fragment with no doctype
                               would render in quirks mode.
    dist/artifact/<name>.html  fragment, fonts inlined. Feed this to Artifact.

Usage:
    python tools/embed_fonts.py src/deck-a.html
    python tools/embed_fonts.py src/*.html
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

MARKER = "/*__AIZU_FONTS__*/"

ROOT = Path(__file__).resolve().parents[3]
FONT_DIR = ROOT / "admin-panel" / "public" / "landing" / "fonts"
OUT_DIR = Path(__file__).resolve().parents[1] / "dist"

# Latin subsets only: the decks are English. Cyrillic/latin-ext would add
# ~130 KB of base64 for glyphs no slide uses.
FACES = [
    ("Inter Tight", "100 900", "inter-tight-latin.woff2"),
    ("JetBrains Mono", "400", "jetbrains-mono-400-latin.woff2"),
    ("JetBrains Mono", "500", "jetbrains-mono-500-latin.woff2"),
]


def font_css() -> str:
    blocks = []
    for family, weight, filename in FACES:
        data = (FONT_DIR / filename).read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        blocks.append(
            f"@font-face{{font-family:'{family}';font-style:normal;"
            f"font-weight:{weight};font-display:block;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}"
        )
    return "\n".join(blocks)


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
    art_dir = OUT_DIR / "artifact"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    art_dir.mkdir(parents=True, exist_ok=True)
    for arg in argv:
        src = Path(arg).resolve()
        html = src.read_text(encoding="utf-8")
        if MARKER not in html:
            print(f"!! {src.name}: marker {MARKER} not found - copied verbatim")
        fragment = html.replace(MARKER, css)

        art = art_dir / src.name
        art.write_text(fragment, encoding="utf-8")

        out = OUT_DIR / src.name
        out.write_text(SHELL.format(fragment=fragment), encoding="utf-8")

        print(
            f"ok {src.name} -> dist/{out.name} ({out.stat().st_size/1024:.0f} KB) "
            f"+ dist/artifact/{art.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
