# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def save_team_logo(file: FileStorage | None, team_name: str) -> str:
    if file is None or not file.filename:
        return ""

    raw_name = secure_filename(file.filename)
    ext = Path(raw_name).suffix.lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        return ""

    safe_team = secure_filename(team_name.strip()) or "team"
    filename = f"{safe_team}-{uuid4().hex[:10]}{ext}"
    TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)
    destination = TEAM_LOGO_DIR / filename
    file.save(destination)
    # When logos live on a persistent disk outside static/, store the bare
    # filename so the /team-logo/<filename> route can serve it.
    if _LOGOS_ON_DISK:
        return f"__disk__/{filename}"
    return f"uploads/team_logos/{filename}"


def migrate_legacy_logo_paths_to_disk(conn: sqlite3.Connection) -> None:
    """Move legacy static logo references to persistent disk-backed paths."""
    if not _LOGOS_ON_DISK:
        return

    static_logo_root = (Path(app.static_folder) / "uploads" / "team_logos").resolve()
    TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)

    for table_name in ("teams", "enemy_teams"):
        rows = conn.execute(
            f"SELECT id, logo_path FROM {table_name} WHERE logo_path LIKE ?",
            ("uploads/team_logos/%",),
        ).fetchall()
        for row in rows:
            stored_path = (row["logo_path"] or "").strip()
            if not stored_path:
                continue

            source = (Path(app.static_folder) / stored_path).resolve()
            try:
                source.relative_to(static_logo_root)
            except ValueError:
                continue
            if not source.exists() or not source.is_file():
                continue

            source_name = secure_filename(source.name)
            if not source_name:
                continue

            destination = TEAM_LOGO_DIR / source_name
            if destination.exists():
                stem = Path(source_name).stem or "team-logo"
                suffix = Path(source_name).suffix.lower()
                destination = TEAM_LOGO_DIR / f"{stem}-{uuid4().hex[:8]}{suffix}"

            try:
                shutil.copy2(source, destination)
            except OSError:
                continue

            conn.execute(
                f"UPDATE {table_name} SET logo_path = ? WHERE id = ?",
                (f"__disk__/{destination.name}", row["id"]),
            )


def _resolve_logo_file_path(relative_path: str) -> Path | None:
    """Return the absolute Path for a stored logo_path value, or None."""
    if not relative_path:
        return None
    if relative_path.startswith("__disk__/"):
        filename = relative_path[len("__disk__/"):]
        return TEAM_LOGO_DIR / filename
    return Path(app.static_folder) / relative_path


def delete_team_logo_file(relative_path: str) -> None:
    if not relative_path:
        return
    logo_path = _resolve_logo_file_path(relative_path)
    if logo_path is None:
        return
    try:
        if logo_path.exists() and logo_path.is_file():
            logo_path.unlink()
    except OSError:
        # Failing to remove an old logo file should not block team updates.
        pass


@app.route("/team-logo/<path:filename>")
def serve_team_logo(filename: str):
    """Serve team logo files stored on the persistent disk (outside static/)."""
    from flask import send_from_directory
    safe = secure_filename(filename)
    if not safe:
        abort(404)
    return send_from_directory(str(TEAM_LOGO_DIR), safe)


def build_default_comp_sections(map_name: str, first_submap: str = "") -> list[dict]:
    submaps = MAP_SUBMAPS.get(map_name, [])
    if submaps:
        chosen_submap = (first_submap or "").strip()
        if not chosen_submap or chosen_submap not in submaps:
            return []
        return [
            {
                "submap": chosen_submap,
                "side": "",
                "score": "",
                "team1": [{"hero": "", "player": ""} for _ in range(6)],
                "team2": [{"hero": "", "player": ""} for _ in range(6)],
            }
        ]

    if map_name in ATTACK_DEFENSE_MAPS:
        return [
            {
                "submap": "",
                "side": side,
                "score": "",
                "team1": [{"hero": "", "player": ""} for _ in range(6)],
                "team2": [{"hero": "", "player": ""} for _ in range(6)],
            }
            for side in SIDES
        ]

    return [
        {
            "submap": "",
            "side": "",
            "score": "",
            "team1": [{"hero": "", "player": ""} for _ in range(6)],
            "team2": [{"hero": "", "player": ""} for _ in range(6)],
        }
    ]



def _strip_bracket_hint(name: str) -> str:
    """Remove trailing parenthetical abbreviation hints like '(FA, SSF, EM)'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _team_name_match_keys(raw_value: str | None) -> set[str]:
    normalized = (raw_value or "").strip().lower()
    compact = _compact_text(normalized)
    if not compact:
        return set()

    keys = {compact}
    alias_groups = [
        {"100t", "100thieves"},
        {"swamp", "swampgaming"},
    ]
    for group in alias_groups:
        if compact in group:
            keys.update(group)

    tokens = re.findall(r"[a-z0-9]+", normalized)
    filtered_tokens = [
        token for token in tokens
        if token not in {"gaming", "esports", "esport", "team", "club"}
    ]
    if filtered_tokens:
        keys.add("".join(filtered_tokens))

    # 3-char prefix abbreviation: "Sentinels" -> "sen", "Liquid" -> "liq"
    if len(compact) >= 5:
        keys.add(compact[:3])

    # Initials from meaningful (non-generic) tokens only.
    # Use filtered_tokens so "Spacestation Gaming" -> ["spacestation"] -> only 1 token -> no initials.
    # This prevents "Swamp Gaming" and "Spacestation Gaming" both generating "sg".
    if len(filtered_tokens) >= 2:
        meaningful_initials = "".join(t[0] for t in filtered_tokens if t[:1].isalpha())
        if len(meaningful_initials) >= 2:
            keys.add(meaningful_initials)
            keys.add("t" + meaningful_initials)

    # Extract explicit abbreviation from parenthetical hint: "Spacestation Gaming (SSG)" -> "ssg"
    paren_match = re.search(r"\(([^)]+)\)", normalized)
    if paren_match:
        paren_abbrev = _compact_text(paren_match.group(1))
        if paren_abbrev:
            keys.add(paren_abbrev)

    return {key for key in keys if key}


def _team_names_match(left: str | None, right: str | None) -> bool:
    left_keys = _team_name_match_keys(left)
    right_keys = _team_name_match_keys(right)
    return bool(left_keys and right_keys and left_keys.intersection(right_keys))


def normalize_map_type_value(raw_value: str | None) -> str:
    normalized = _compact_text(raw_value or "")
    return MAP_TYPE_ALIASES.get(normalized, DEFAULT_MAP_TYPE)


def update_map_type_from_form(map_entry: dict) -> None:
    if "map_type" not in request.form:
        return
    map_entry["map_type"] = normalize_map_type_value(request.form.get("map_type", ""))


def _match_map_name(raw: str) -> str:
    """
    Try to find the closest canonical map name from MAPS for a raw string.
    Falls back to the raw string stripped of bracket hints if no match found.
    """
    base = _strip_bracket_hint(raw)
    base_lower = base.lower()
    compact = _compact_text(base)

    alias_lookup = {
        "hellsheaven": "Hell's Haven",
        "hellshaven": "Hell's Haven",
        "birnintchalla": "Birin T'Challa",
        "birintchalla": "Birin T'Challa",
        "celestialhusk": "Celestial",
    }
    aliased = alias_lookup.get(compact)
    if aliased:
        return aliased

    # Exact match
    for m in MAPS:
        if m.lower() == base_lower:
            return m
    compact_map = {_compact_text(m): m for m in MAPS}
    if compact in compact_map:
        return compact_map[compact]
    # Prefix match: raw starts with canonical map name
    for m in sorted(MAPS, key=len, reverse=True):
        if base_lower.startswith(m.lower()):
            return m
        compact_name = _compact_text(m)
        if compact and compact_name and compact.startswith(compact_name):
            return m

    best_match = None
    best_score = 0.0
    for m in MAPS:
        score = SequenceMatcher(None, compact, _compact_text(m)).ratio()
        if score > best_score:
            best_score = score
            best_match = m
    if best_match and best_score >= 0.74:
        return best_match
    return base


def _compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def normalize_hero_slot_value(raw_hero: str | None) -> str:
    hero_text = (raw_hero or "").strip()
    if not hero_text:
        return ""

    resolved = _resolve_hero_transform_key(hero_text)
    return resolved or hero_text


def _resolve_hero_transform_key(hero_name: str) -> str | None:
    """Return the best HERO_TRANSFORMATIONS key for a potentially misspelled hero name."""
    raw = (hero_name or "").strip()
    if not raw:
        return None

    if raw in HERO_TRANSFORMATIONS:
        return raw

    compact = _compact_text(raw)
    if not compact:
        return None

    mapped = HERO_NAME_ALIASES.get(compact)
    if mapped:
        return mapped

    if compact == "suppool":
        return "SupportPool"
    if compact.startswith("deadpool"):
        if any(token in compact for token in ("tank", "vanguard")):
            return "Tankpool"
        if any(token in compact for token in ("support", "strategist", "supp", "strat")):
            return "SupportPool"
        if any(token in compact for token in ("dps", "duelist", "duel")):
            return "DpsPool"

    compact_map = { _compact_text(k): k for k in HERO_TRANSFORMATIONS.keys() }
    if compact in compact_map:
        return compact_map[compact]

    best_key = None
    best_score = 0.0
    for key in HERO_TRANSFORMATIONS.keys():
        score = SequenceMatcher(None, compact, _compact_text(key)).ratio()
        if score > best_score:
            best_score = score
            best_key = key

    if best_key and best_score >= 0.78:
        return best_key
    return None


_POOL_HERO_ROLE_ICONS: dict[str, str] = {
    "Tankpool": "/static/role-icons/Vanguard.webp",
    "DpsPool": "/static/role-icons/Duelist.webp",
    "SupportPool": "/static/role-icons/Strategist.webp",
}


def _hero_image_url(hero_name: str) -> str:
    safe_name = (hero_name or "Hero").strip() or "Hero"
    return f"/hero-image/{quote(safe_name[:80], safe='')}?v=20260514a"


HERO_IMAGE_CACHE_TTL_SECONDS = 60 * 60 * 24
_HERO_IMAGE_CACHE: dict[str, tuple[float, bytes, str]] = {}


def _hero_image_local_file(hero_name: str) -> Path | None:
    hero_dir = Path(app.static_folder) / "heroes"
    if not hero_dir.exists() or not hero_dir.is_dir():
        return None

    transform_key = _resolve_hero_transform_key(hero_name) or (hero_name or "").strip()
    compact_key = _compact_text(transform_key)
    if not compact_key:
        return None

    # Explicit mapping for locally curated portrait files with non-standard names.
    explicit_local_files = {
        "Devil Dinosaur": "Devil_Dinosaur_Full_Hero_Portrait.webp",
    }
    explicit_name = explicit_local_files.get(transform_key)
    if explicit_name:
        explicit_path = hero_dir / explicit_name
        if explicit_path.exists() and explicit_path.is_file():
            return explicit_path

    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
    for file_path in hero_dir.iterdir():
        if not file_path.is_file() or file_path.suffix.lower() not in image_suffixes:
            continue
        compact_stem = _compact_text(file_path.stem)
        if compact_key in compact_stem or compact_stem in compact_key:
            return file_path

    return None


def _hero_image_candidate_urls(hero_name: str) -> list[str]:
    transform_key = _resolve_hero_transform_key(hero_name)
    candidates: list[str] = []

    if transform_key:
        images = HERO_TRANSFORMATIONS.get(transform_key) or []
        if images:
            filename = Path(images[0]).name
            dotgg_filename = re.sub(r"-\d+(?=\.webp$)", "", filename)
            candidates.append(f"https://marvelrivalsapi.com/rivals{images[0]}")
            candidates.append(f"https://static.dotgg.gg/rivals/characters/{dotgg_filename}")

    return candidates


def _hero_image_placeholder_svg(hero_name: str) -> str:
    text = (hero_name or "Hero").strip()[:24] or "Hero"
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='80' height='80' viewBox='0 0 80 80'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0%' stop-color='#111827'/><stop offset='100%' stop-color='#1f2937'/></linearGradient></defs>"
        "<rect width='80' height='80' fill='url(#g)' rx='10'/>"
        f"<text x='40' y='44' text-anchor='middle' font-size='11' font-family='Arial, sans-serif' fill='#e6edf3'>{text}</text>"
        "</svg>"
    )


@app.route("/hero-image/<path:hero_name>")
def hero_image_proxy(hero_name: str):
    requested = (hero_name or "").strip() or "Hero"
    cache_key = _resolve_hero_transform_key(requested) or requested
    now = time.time()
    cached = _HERO_IMAGE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < HERO_IMAGE_CACHE_TTL_SECONDS and cached[2] != "image/svg+xml":
        return Response(
            cached[1],
            mimetype=cached[2],
            headers={"Cache-Control": "public, max-age=86400"},
        )

    local_file = _hero_image_local_file(requested)
    if local_file is not None:
        try:
            payload = local_file.read_bytes()
            suffix = local_file.suffix.lower()
            content_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
                ".avif": "image/avif",
            }.get(suffix, "image/webp")
            _HERO_IMAGE_CACHE[cache_key] = (now, payload, content_type)
            return Response(
                payload,
                mimetype=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except OSError:
            pass

    for image_url in _hero_image_candidate_urls(requested):
        try:
            remote_request = Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(remote_request, timeout=1.25) as remote_response:
                content_type = remote_response.headers.get_content_type() or "image/webp"
                if not content_type.startswith("image/"):
                    continue
                payload = remote_response.read()
                if not payload:
                    continue
                _HERO_IMAGE_CACHE[cache_key] = (now, payload, content_type)
                return Response(
                    payload,
                    mimetype=content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            continue

    placeholder = _hero_image_placeholder_svg(requested).encode("utf-8")
    _HERO_IMAGE_CACHE[cache_key] = (now, placeholder, "image/svg+xml")
    return Response(
        placeholder,
        mimetype="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _hero_pool_label(hero_name: str) -> str:
    canonical = _resolve_hero_transform_key(hero_name) or (hero_name or "").strip()
    if canonical == "Tankpool":
        return "tank"
    if canonical == "DpsPool":
        return "dps"
    if canonical == "SupportPool":
        return "supp"
    return ""


def _hero_display_name(hero_name: str) -> str:
    canonical = _resolve_hero_transform_key(hero_name) or (hero_name or "").strip()
    if canonical == "Tankpool":
        return "Deadpool (T)"
    if canonical == "DpsPool":
        return "Deadpool (D)"
    if canonical == "SupportPool":
        return "Deadpool (S)"
    return canonical or (hero_name or "")


@app.context_processor
def inject_template_helpers():
    def _sample_warn(n, threshold: int = 5) -> Markup:
        """Return the count with a &#9888; icon when below the sample threshold."""
        try:
            count = int(n)
        except (TypeError, ValueError):
            return Markup(str(n) if n is not None else "")
        if count < threshold:
            return Markup(
                f'{count}\u202f<span class="sample-warn-icon" '
                f'title="Low sample size (fewer than {threshold} maps)">&#9888;</span>'
            )
        return Markup(str(count))

    def _team_logo_url(logo_path: str) -> str:
        """Return the URL to display for a team logo_path value."""
        if not logo_path:
            return ""
        if logo_path.startswith("__disk__/"):
            filename = logo_path[len("__disk__/"):]
            from flask import url_for as _url_for
            return _url_for("serve_team_logo", filename=filename)
        from flask import url_for as _url_for
        return _url_for("static", filename=logo_path)

    return {
        "hero_image_url": _hero_image_url,
        "map_image_url": get_map_image_url,
        "hero_pool_label": _hero_pool_label,
        "hero_display_name": _hero_display_name,
        "sample_warn": _sample_warn,
        "pool_role_icons": _POOL_HERO_ROLE_ICONS,
        "team_logo_url": _team_logo_url,
    }


