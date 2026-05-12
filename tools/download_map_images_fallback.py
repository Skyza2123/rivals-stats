#!/usr/bin/env python3
"""Download map images from non-API sources into static/maps.

Usage:
    python tools/download_map_images_fallback.py
"""

from __future__ import annotations

import ssl
import re
import sys
from urllib.error import URLError
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import MAPS  # noqa: E402

STATIC_MAPS_DIR = ROOT / "static" / "maps"
USER_AGENT = "Mozilla/5.0"

LIQUIPEDIA_TITLE_ALIASES = {
    "Birin T'Challa": ["Birnin_T'Challa"],
    "Celestial": ["Celestial_Husk"],
    "Hell's Haven": ["Hell's_Heaven", "Hells_Heaven", "Hells_Haven", "Hell's_Haven"],
    "Throne of Knull": ["Klyntar:_Throne_of_Knull", "Throne_of_Knull"],
}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _filename_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _download_text(url: str, timeout: float = 12.0) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with _open_url(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _open_url(request: Request, timeout: float):
    try:
        return urlopen(request, timeout=timeout)
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            insecure_ctx = ssl._create_unverified_context()
            return urlopen(request, timeout=timeout, context=insecure_ctx)
        raise


def _extract_og_image(html: str) -> str | None:
    match = re.search(
        r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
        html,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    match = re.search(
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image[\"']",
        html,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return None


def _pick_extension(image_url: str) -> str:
    path = urlparse(image_url).path.lower()
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".webp"):
        return ".webp"
    if path.endswith(".jpeg"):
        return ".jpeg"
    return ".jpg"


def _download_binary(url: str, timeout: float = 15.0) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with _open_url(request, timeout=timeout) as response:
        return response.read()


def _try_source_pages(map_name: str) -> str | None:
    slug = _slugify(map_name)
    title_slug = map_name.replace(" ", "_")

    liquipedia_titles = [title_slug]
    liquipedia_titles.extend(LIQUIPEDIA_TITLE_ALIASES.get(map_name, []))
    liquipedia_links = []
    for title in liquipedia_titles:
        encoded_title = quote(title, safe="_():'")
        liquipedia_links.append(f"https://liquipedia.net/marvelrivals/{encoded_title}")

    candidate_pages = [
        f"https://marvelrivals.gg/{slug}/",
        f"https://marvelrivals.fandom.com/wiki/{title_slug}",
        *liquipedia_links,
    ]

    for page_url in candidate_pages:
        try:
            html = _download_text(page_url)
        except Exception:
            continue
        image_url = _extract_og_image(html)
        if image_url and image_url.startswith("http"):
            return image_url

    # Try direct Liquipedia commons naming patterns.
    image_base_titles = [title_slug]
    image_base_titles.extend(LIQUIPEDIA_TITLE_ALIASES.get(map_name, []))
    for base_title in image_base_titles:
        cleaned = base_title.replace("'", "")
        for ext in ("jpg", "png", "jpeg", "webp"):
            candidate = f"https://liquipedia.net/commons/images/Marvel_Rivals_map_{quote(cleaned, safe='_:-()')}.{ext}"
            try:
                payload = _download_binary(candidate, timeout=8.0)
                if payload:
                    return candidate
            except Exception:
                continue
    return None


def main() -> int:
    STATIC_MAPS_DIR.mkdir(parents=True, exist_ok=True)

    success = 0
    skipped = 0
    failed = 0

    for map_name in MAPS:
        file_stem = _filename_slug(map_name)
        existing = list(STATIC_MAPS_DIR.glob(f"{file_stem}.*"))
        existing_non_svg = [p for p in existing if p.suffix.lower() != ".svg"]
        if existing_non_svg:
            skipped += 1
            print(f"[SKIP] {map_name}: already exists ({existing_non_svg[0].name})")
            continue

        image_url = _try_source_pages(map_name)
        if not image_url:
            failed += 1
            print(f"[FAIL] {map_name}: no non-API image source found")
            continue

        try:
            payload = _download_binary(image_url)
            if not payload:
                raise ValueError("empty payload")
            ext = _pick_extension(image_url)
            for prior in existing:
                if prior.suffix.lower() == ".svg":
                    try:
                        prior.unlink()
                    except OSError:
                        pass
            out_path = STATIC_MAPS_DIR / f"{file_stem}{ext}"
            out_path.write_bytes(payload)
            success += 1
            print(f"[OK]   {map_name}: {out_path.name} <- {image_url}")
        except Exception as exc:
            failed += 1
            print(f"[FAIL] {map_name}: download failed ({exc})")

    print("\nSummary")
    print(f"  Downloaded: {success}")
    print(f"  Skipped:    {skipped}")
    print(f"  Failed:     {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
