#!/usr/bin/env python3
"""Generate local SVG placeholders for all map names.

This gives an offline-safe image set when remote map APIs are unavailable.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import MAPS, MAP_SUBMAPS  # noqa: E402

OUT_DIR = ROOT / "static" / "maps"


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _svg_for(name: str) -> str:
    label = (name or "Map")[:28]
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='1280' height='720' viewBox='0 0 1280 720'>"
        "<defs><linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0%' stop-color='#0f172a'/><stop offset='55%' stop-color='#1e293b'/>"
        "<stop offset='100%' stop-color='#111827'/></linearGradient></defs>"
        "<rect width='1280' height='720' fill='url(#bg)'/>"
        "<circle cx='1140' cy='120' r='180' fill='#1d4ed8' opacity='0.15'/>"
        "<circle cx='120' cy='620' r='220' fill='#06b6d4' opacity='0.12'/>"
        f"<text x='640' y='372' text-anchor='middle' font-size='58' font-family='Arial, sans-serif' fill='#e6edf3'>{label}</text>"
        "</svg>"
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_names: set[str] = set(MAPS)
    for submaps in MAP_SUBMAPS.values():
        all_names.update(submaps)

    created = 0
    skipped = 0
    for map_name in sorted(all_names):
        slug = _slugify(map_name)
        has_existing = any((OUT_DIR / f"{slug}{ext}").exists() for ext in (".jpg", ".jpeg", ".png", ".webp", ".svg"))
        if has_existing:
            skipped += 1
            continue
        out_path = OUT_DIR / f"{slug}.svg"
        out_path.write_text(_svg_for(map_name), encoding="utf-8")
        created += 1

    print(f"Created placeholders: {created}")
    print(f"Skipped existing: {skipped}")
    print(f"Output directory: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
