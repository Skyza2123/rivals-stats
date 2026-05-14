# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.


def _parse_text_payload(file_bytes: bytes) -> str:
    try:
        return file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1")


def _extract_csv_text_from_xlsx(file_bytes: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except Exception as exc:  # pragma: no cover - dependency/runtime guard
        raise ValueError("XLSX import requires openpyxl to be installed.") from exc

    workbook = load_workbook(filename=io.BytesIO(file_bytes), data_only=True, read_only=True)
    worksheet = workbook.active

    output = io.StringIO()
    writer = csv.writer(output)
    for row in worksheet.iter_rows(values_only=True):
        writer.writerow(["" if value is None else str(value) for value in row])

    workbook.close()
    return output.getvalue()


_SCRIMCORE_LOG_EVENT_MAP = {
    "kill": "Pick",
    "ultimate_start": "Ult Used",
    "ultimate_end": "Ult Used",
    "ultimate_charged": "Ult Used",
    "objective_captured": "Objective Taken",
    "payload_progress": "Objective Taken",
    "point_progress": "Objective Taken",
}


def split_line_preserving_coords(line: str) -> list[str]:
    """Split a CSV-ish line while preserving commas inside coordinate tuples."""
    fields: list[str] = []
    current: list[str] = []
    depth = 0
    for char in line:
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1

        if char == "," and depth == 0:
            fields.append("".join(current).strip())
            current = []
            continue

        current.append(char)

    fields.append("".join(current).strip())
    return fields


def _coerce_scrimcore_field(value: str) -> str:
    cleaned = (value or "").strip()
    if "*" in cleaned:
        return "0"
    return cleaned


def _clean_scrimcore_log_rows(rows: list[list[str]]) -> list[list[str]]:
    cleaned_rows: list[list[str]] = []
    for row in rows:
        if not row:
            continue

        if len(row) >= 2:
            event_type = (row[1] or "").strip().lower()
        else:
            event_type = (row[0] or "").strip().lower()

        if event_type == "mercy_rez":
            tail = row[2:] if len(row) >= 2 else row[1:]
            if any((field or "").strip() == "" for field in tail):
                continue

        normalized = [_coerce_scrimcore_field(field) for field in row]
        if normalized and normalized[0] == "****":
            normalized[0] = "kill"
        if len(normalized) > 1 and normalized[1] == "****":
            normalized[1] = "kill"
        cleaned_rows.append(normalized)

    return cleaned_rows


def parse_scrimcore_log_text(raw_text: str) -> tuple[dict[str, list[list[str]]], dict]:
    """Parse raw ScrimCore-style log text into categorized rows and summary metadata."""
    raw_rows = [split_line_preserving_coords(line) for line in (raw_text or "").splitlines() if (line or "").strip()]
    cleaned_rows = _clean_scrimcore_log_rows(raw_rows)

    categorized: dict[str, list[list[str]]] = defaultdict(list)
    for row in cleaned_rows:
        if len(row) < 2:
            continue

        timestamp = row[0]
        event_type = (row[1] or "").strip().lower()
        payload = row[2:]
        if not event_type:
            continue

        normalized_row = [timestamp, *payload]
        categorized[event_type].append(normalized_row)

    event_counts = {event_type: len(rows) for event_type, rows in categorized.items()}
    map_name = ""
    team1_name = ""
    team2_name = ""
    match_start_rows = categorized.get("match_start", [])
    if match_start_rows:
        first_start = match_start_rows[0]
        if len(first_start) > 3:
            map_name = str(first_start[3]).strip()
        if len(first_start) > 5:
            team1_name = str(first_start[5]).strip()
        if len(first_start) > 6:
            team2_name = str(first_start[6]).strip()

    summary = {
        "total_rows": len(cleaned_rows),
        "event_type_count": len(event_counts),
        "event_counts": dict(sorted(event_counts.items(), key=lambda item: (-item[1], item[0]))),
        "map_name": map_name,
        "team1_name": team1_name,
        "team2_name": team2_name,
    }
    return categorized, summary


def _build_scrimcore_timeline_events(parsed_rows: dict[str, list[list[str]]], *, max_events: int = 200) -> list[dict]:
    timeline_events: list[dict] = []

    for event_name, rows in parsed_rows.items():
        for row in rows:
            timestamp = str(row[0] if row else "").strip()
            payload = row[1:] if len(row) > 1 else []
            mapped_event_type = _SCRIMCORE_LOG_EVENT_MAP.get(event_name, "Other")

            if event_name == "kill":
                attacker = payload[1] if len(payload) > 1 else "Unknown"
                attacker_hero = payload[2] if len(payload) > 2 else ""
                victim = payload[4] if len(payload) > 4 else "Unknown"
                victim_hero = payload[5] if len(payload) > 5 else ""
                description = f"{attacker} ({attacker_hero or 'Unknown'}) eliminated {victim} ({victim_hero or 'Unknown'})."
            elif event_name in {"match_start", "round_start"}:
                description = "Match/round start event parsed from log."
            elif event_name in {"match_end", "round_end"}:
                description = "Match/round end event parsed from log."
            else:
                description = f"{event_name.replace('_', ' ').title()} event parsed from log."

            timeline_events.append(
                {
                    "id": 0,
                    "timestamp": timestamp,
                    "event_type": mapped_event_type,
                    "description": description,
                }
            )

    if timeline_events:
        timeline_events[0]["event_type"] = "First Kill" if timeline_events[0]["event_type"] == "Pick" else timeline_events[0]["event_type"]

    return timeline_events[:max_events]


def _cache_only_log_import_mode() -> bool:
    """Return True when experiment log imports should skip DB persistence."""
    raw = (
        os.environ.get("PREVIEW_CACHE_ONLY_LOG_IMPORT")
        or os.environ.get("PREVIEW_CACHE_ONLY")
        or ""
    )
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def _canonicalize_submap_name(parent_map: str, raw_submap: str) -> str:
    """Normalize imported submap text to a canonical submap for the parent map."""
    clean = (raw_submap or "").strip()
    if not clean:
        return ""

    candidates = MAP_SUBMAPS.get(parent_map, [])
    if not candidates:
        return clean

    clean_compact = _compact_text(clean)
    parent_compact = _compact_text(parent_map)

    # Some sheets repeat the parent map in the submap value.
    if parent_compact and clean_compact.startswith(parent_compact):
        clean_compact = clean_compact[len(parent_compact):]

    # Direct/fuzzy match against known submaps for this map.
    for candidate in candidates:
        candidate_compact = _compact_text(candidate)
        if clean_compact == candidate_compact:
            return candidate
        if clean_compact and (clean_compact in candidate_compact or candidate_compact in clean_compact):
            return candidate

    # Known analyst-sheet abbreviations.
    alias_lookup = {
        "Hell's Haven": {
            "fa": "Frozen Airfield",
            "ssf": "Super-Soldier Factory",
            "em": "Eldritch Monument",
            "supersoilderfactory": "Super-Soldier Factory",
            "hellshavensupersoilderfactory": "Super-Soldier Factory",
        },
        "Birin T'Challa": {
            "iis": "Imperial Institute of Science",
            "impinsscience": "Imperial Institute of Science",
            "ss": "Stellar Spaceport",
            "wf": "Warrior Falls",
        },
        "Krakoa": {
            "ca": "Carousel",
            "gr": "Grove",
            "cr": "Cradle",
        },
        "Celestial": {
            "co": "Codex",
            "va": "Vault",
            "ha": "Hand",
        },
    }

    mapped = alias_lookup.get(parent_map, {}).get(clean_compact)
    if mapped:
        return mapped

    # Fuzzy match for misspellings (e.g. Soilder vs Soldier).
    best_candidate = ""
    best_score = 0.0
    for candidate in candidates:
        candidate_compact = _compact_text(candidate)
        score = SequenceMatcher(None, clean_compact, candidate_compact).ratio()
        if score > best_score:
            best_score = score
            best_candidate = candidate
    if best_candidate and best_score >= 0.72:
        return best_candidate

    return clean


def split_score_pair(raw_score: str) -> tuple[str, str]:
    value = (raw_score or "").strip()
    if not value:
        return "", ""

    matches = re.findall(r"\d+(?:\.\d+)?", value)
    if not matches:
        return "", ""
    if len(matches) == 1:
        return matches[0], ""
    return matches[0], matches[1]


def build_score_text(left_score: str, right_score: str, fallback: str = "") -> str:
    left = (left_score or "").strip()
    right = (right_score or "").strip()
    if left and right:
        return f"{left}-{right}"
    if left:
        return left
    if right:
        return right
    return (fallback or "").strip()


def score_for_perspective(raw_score: str, *, perspective: str = "left") -> float | None:
    left_score, right_score = split_score_pair(raw_score)
    target = left_score if perspective == "left" else right_score
    if not target:
        return None
    try:
        return float(target)
    except ValueError:
        return None


def flip_score_text(raw_score: str) -> str:
    left_score, right_score = split_score_pair(raw_score)
    if left_score and right_score:
        return f"{right_score}-{left_score}"
    return (raw_score or "").strip()


def _our_team_slot_from_protect_order(raw_value: str) -> str:
    """
    Determine our team slot from the CSV 1st/2nd protect column.
    User rule: 2nd protect = team1, so 1st protect = team2.
    """
    value = (raw_value or "").strip().lower()
    if not value:
        return "team1"
    if value.startswith("2") or "second" in value:
        return "team1"
    if value.startswith("1") or "first" in value:
        return "team2"
    return "team1"


def _parse_csv_into_scrims(
    raw_text: str,
    team_id: int | None,
    team_name: str,
) -> tuple[list[dict], list[str]]:
    """
    Parse the analyst CSV format and return (scrim_list, warning_list).

    CSV structure (0-indexed columns):
      0   Date
      1   Enemy team name
      2   Map name (may include submap/side suffix or bracket hint)
      3   Our score
      4   Their score
      5   Result (Won/Lost)
      6   Ban Us1          7  Ban Them1
      8   Ban Us2          9  Ban Them2
      10  Ban Us3          11 Ban Them3
      12  Ban Us4          13 Ban Them4
      14  Protect Us1      15 Protect Them1
      16  Protect Us2      17 Protect Them2
      18  1st/2nd Protect  (ignored)
      19  (separator)
      20  Date (right half - used when left is blank)
      21  Enemy (right half)
      22  Map (right half)
      23  Us Result
      24-29  Our heroes (Tank,Tank,DPS,DPS,Supp,Supp)
      30  (separator)
      31  Them Result
      32-37  Their heroes
    """

    warnings: list[str] = []

    reader = csv.reader(io.StringIO(raw_text))
    rows = list(reader)
    if not rows:
        return [], ["CSV file is empty."]

    # Skip the header row (it starts with empty cells then "Ban Us1" etc.)
    header = rows[0]
    data_rows = rows[1:]

    def _pad(row: list[str]) -> list[str]:
        while len(row) < _CSV_MIN_COLS:
            row.append("")
        return row

    def _cell(row: list[str], idx: int) -> str:
        return row[idx].strip() if idx < len(row) else ""

    def _heroes(row: list[str], sl: slice) -> list[dict]:
        return [{"hero": normalize_hero_slot_value(h), "player": ""} for h in row[sl]]

    # Group rows into (date, enemy) buckets, keeping insertion order.
    # We preserve the order that maps appear so that sub-rows stay near parents.
    # Structure: { (date, enemy): [padded_row, ...] }
    from collections import OrderedDict
    buckets: OrderedDict[tuple[str, str], list[list[str]]] = OrderedDict()
    short_row_count = 0

    for raw_row in data_rows:
        if len(raw_row) < _CSV_MIN_COLS:
            short_row_count += 1
            continue

        row = _pad(list(raw_row))
        left_filled  = any(row[i].strip() for i in range(20))
        right_filled = any(row[i].strip() for i in range(20, _CSV_MIN_COLS))
        if not left_filled and not right_filled:
            continue

        if left_filled:
            date_val  = _cell(row, _CSV_DATE)
            enemy_val = _cell(row, _CSV_ENEMY)
        else:
            date_val  = _cell(row, _CSV_R_DATE)
            enemy_val = _cell(row, _CSV_R_ENEMY)

        if not date_val and not enemy_val:
            continue

        key = (date_val, enemy_val)
        buckets.setdefault(key, []).append(row)

    if not buckets:
        if short_row_count:
            return [], [f"Skipped {short_row_count} row(s) with missing columns."]
        return [], ["No data rows found in CSV."]

    # Build scrim objects from each bucket.
    all_scrims: list[dict] = []

    for (scrim_date, enemy_name), bucket_rows in buckets.items():
        maps: list[dict] = []
        current_parent_idx: int | None = None

        for row in bucket_rows:
            left_filled = any(row[i].strip() for i in range(20))

            # Gather map name from whichever side is available
            if left_filled:
                raw_map_name = _cell(row, _CSV_MAP)
            else:
                raw_map_name = _cell(row, _CSV_R_MAP)

            if not raw_map_name:
                continue

            normalized_map_name = raw_map_name.strip()
            normalized_map_name_lower = normalized_map_name.lower()
            is_side_row = normalized_map_name_lower.endswith(" attack") or normalized_map_name_lower.endswith(" defense")

            # Right-side heroes / results
            our_heroes  = _heroes(row, _CSV_R_US_H)
            their_heroes = _heroes(row, _CSV_R_TH_H)
            has_comp = any(h["hero"] for h in our_heroes + their_heroes)

            has_bans = any(
                row[i].strip()
                for i in [_CSV_BAN_US1, _CSV_BAN_TH1, _CSV_BAN_US2, _CSV_BAN_TH2,
                           _CSV_BAN_US3, _CSV_BAN_TH3, _CSV_BAN_US4, _CSV_BAN_TH4,
                           _CSV_PROT_US1, _CSV_PROT_TH1, _CSV_PROT_US2, _CSV_PROT_TH2]
            )

            # Parent map rows are map-level rows. Side split rows like "Midtown Attack"
            # should never become standalone parent maps.
            is_parent = has_bans or (left_filled and not has_comp and not is_side_row)

            if is_parent:
                canonical = _match_map_name(raw_map_name)
                base_name = _strip_bracket_hint(raw_map_name)
                our_team_slot = _our_team_slot_from_protect_order(_cell(row, _CSV_PROTECT_ORDER))
                enemy_team_slot = opposite_team_slot(our_team_slot)

                left_result = _cell(row, _CSV_RESULT)
                result_str  = "Win" if left_result == "Won" else "Loss" if left_result == "Lost" else ""

                score_us  = _cell(row, _CSV_SCORE_US)
                score_thm = _cell(row, _CSV_SCORE_THM)
                score_str = f"{score_us}-{score_thm}" if score_us or score_thm else ""

                draft_our = {
                    "ban1":     _cell(row, _CSV_BAN_US1),
                    "ban2":     _cell(row, _CSV_BAN_US2),
                    "protect1": _cell(row, _CSV_PROT_US1),
                    "ban3":     _cell(row, _CSV_BAN_US3),
                    "protect2": _cell(row, _CSV_PROT_US2),
                    "ban4":     _cell(row, _CSV_BAN_US4),
                }
                draft_enemy = {
                    "ban1":     _cell(row, _CSV_BAN_TH1),
                    "ban2":     _cell(row, _CSV_BAN_TH2),
                    "protect1": _cell(row, _CSV_PROT_TH1),
                    "ban3":     _cell(row, _CSV_BAN_TH3),
                    "protect2": _cell(row, _CSV_PROT_TH2),
                    "ban4":     _cell(row, _CSV_BAN_TH4),
                }
                draft = {
                    our_team_slot: draft_our,
                    enemy_team_slot: draft_enemy,
                }

                map_entry = {
                    "_base_name": base_name,        # temp field for sub-row matching
                    "map_name":   canonical,
                    "side":       "",
                    "our_team_slot": our_team_slot,
                    "result":     result_str,
                    "score":      score_str,
                    "draft":      draft,
                    "comp":       build_default_comp_sections(canonical),
                    "notes":      "",
                    "vod_url":    "",
                    "events":     [],
                }
                maps.append(map_entry)
                current_parent_idx = len(maps) - 1

            elif has_comp or is_side_row:
                # Sub-row: comp section belonging to the preceding parent
                # Determine parent by name prefix match
                parent_idx = current_parent_idx
                if parent_idx is None:
                    # No parent yet. Only create an implicit parent if this row has comp
                    # payload. For empty side rows, skip to avoid inflating map counts.
                    if not has_comp:
                        warnings.append(
                            f"Skipped side row without parent: {raw_map_name} ({scrim_date} vs {enemy_name})."
                        )
                        continue

                    # No parent yet - create an implicit parent
                    canonical = _match_map_name(raw_map_name)
                    map_entry = {
                        "_base_name": canonical,
                        "map_name":   canonical,
                        "side":       "",
                        "our_team_slot": "team1",
                        "result":     "",
                        "score":      "",
                        "draft":      {"team1": {"ban1":"","ban2":"","protect1":"","ban3":"","protect2":"","ban4":""}, "team2": {"ban1":"","ban2":"","protect1":"","ban3":"","protect2":"","ban4":""}},
                        "comp":       build_default_comp_sections(canonical),
                        "notes":      "",
                        "vod_url":    "",
                        "events":     [],
                    }
                    maps.append(map_entry)
                    parent_idx = len(maps) - 1
                    current_parent_idx = parent_idx

                parent = maps[parent_idx]
                base = parent["_base_name"]
                suffix = raw_map_name[len(base):].strip() if raw_map_name.lower().startswith(base.lower()) else raw_map_name

                submap = ""
                section_side = ""
                if suffix in ("Attack", "Defense"):
                    section_side = suffix
                elif suffix:
                    submap = _canonicalize_submap_name(parent.get("map_name", ""), suffix)

                score_us = _cell(row, _CSV_SCORE_US)
                score_thm = _cell(row, _CSV_SCORE_THM)
                section_score = ""
                if score_us and score_thm:
                    section_score = f"{score_us}-{score_thm}"
                elif score_us:
                    section_score = score_us
                elif score_thm:
                    section_score = score_thm

                # Fallback: some analyst sheets only include side outcome (Won/Lost)
                # for Attack/Defense rows. Use 1/0 so averages still populate.
                if not section_score:
                    us_result = _cell(row, _CSV_R_US_RES).lower()
                    if us_result == "won":
                        section_score = "1"
                    elif us_result == "lost":
                        section_score = "0"

                # Pad heroes to 6 slots each
                while len(our_heroes) < 6:
                    our_heroes.append({"hero": "", "player": ""})
                while len(their_heroes) < 6:
                    their_heroes.append({"hero": "", "player": ""})

                parent_our_slot = parent.get("our_team_slot", "team1")
                if parent_our_slot not in TEAM_SLOTS:
                    parent_our_slot = "team1"

                section = {
                    "submap": submap,
                    "side":   section_side,
                    "score":  section_score,
                    "team1":  our_heroes[:6] if parent_our_slot == "team1" else their_heroes[:6],
                    "team2":  our_heroes[:6] if parent_our_slot == "team2" else their_heroes[:6],
                }

                # Assign comps to the matching prebuilt section for this map type
                # (submap for Control, side for Escort/Hybrid). If no match exists,
                # append as a fallback.
                target_index = None
                for idx, existing in enumerate(parent.get("comp", [])):
                    existing_submap = (existing.get("submap") or "").strip().lower()
                    existing_side = (existing.get("side") or "").strip().lower()
                    if section_side:
                        if existing_side == section_side.strip().lower():
                            target_index = idx
                            break
                    elif submap:
                        if _compact_text(existing_submap) == _compact_text(submap):
                            target_index = idx
                            break

                if target_index is None:
                    parent.setdefault("comp", []).append(section)
                else:
                    parent["comp"][target_index].update(
                        {
                            "score": section_score,
                            "team1": our_heroes[:6] if parent_our_slot == "team1" else their_heroes[:6],
                            "team2": our_heroes[:6] if parent_our_slot == "team2" else their_heroes[:6],
                        }
                    )

        # Remove temp field
        for m in maps:
            m.pop("_base_name", None)

        if not maps:
            warnings.append(f"No maps found for scrim {scrim_date} vs {enemy_name}.")
            continue

        scrim = {
            "opponent":      enemy_name,
            "enemy_team":    enemy_name,
            "enemy_team_id": None,
            "scrim_date":    scrim_date,
            "season":       "",
            "team_id":       team_id,
            "team_name":     team_name,
            "notes":         "",
            "maps":          maps,
        }
        all_scrims.append(scrim)

    return all_scrims, warnings


def _parse_template_csv_into_scrims(
    raw_text: str,
    team_id: int | None,
    team_name: str,
) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []

    reader = csv.reader(io.StringIO(raw_text))
    rows = list(reader)
    if not rows:
        return [], ["CSV file is empty."]

    data_rows = rows[1:]
    selected_team_name = (team_name or "").strip()

    from collections import OrderedDict
    buckets: OrderedDict[tuple[str, str], list[list[str]]] = OrderedDict()

    def _pad(row: list[str]) -> list[str]:
        while len(row) < _TEMPLATE_CSV_MIN_COLS:
            row.append("")
        return row

    def _cell(row: list[str], idx: int) -> str:
        return row[idx].strip() if idx < len(row) else ""

    def _build_team_comp(row: list[str], pairs: tuple[tuple[int, int], ...]) -> list[dict]:
        slots: list[dict] = []
        for player_idx, hero_idx in pairs:
            raw_player_name = _cell(row, player_idx)
            slots.append(
                {
                    "player": "" if is_ringer_player_name(raw_player_name) else normalize_player_name(raw_player_name),
                    "hero": normalize_hero_slot_value(_cell(row, hero_idx)),
                }
            )
        return slots

    def _result_for_selected_team(result_value: str, team1_name: str, team2_name: str, our_slot: str) -> str:
        normalized = (result_value or "").strip().lower()
        if not normalized:
            return ""
        if normalized in {"draw", "tie"}:
            return ""
        if normalized in {"win", "won"}:
            return "Win"
        if normalized in {"loss", "lost"}:
            return "Loss"

        team1_normalized = (team1_name or "").strip().lower()
        team2_normalized = (team2_name or "").strip().lower()
        if normalized == team1_normalized:
            return "Win" if our_slot == "team1" else "Loss"
        if normalized == team2_normalized:
            return "Win" if our_slot == "team2" else "Loss"
        return ""

    for raw_row in data_rows:
        if not any(cell.strip() for cell in raw_row):
            continue
        row = _pad(list(raw_row))

        team1_name = _cell(row, _TEMPLATE_CSV_TEAM1)
        team2_name = _cell(row, _TEMPLATE_CSV_TEAM2)
        map_name = _cell(row, _TEMPLATE_CSV_MAP)
        scrim_date = _cell(row, _TEMPLATE_CSV_DATE)

        if not team1_name or not team2_name or not map_name or not scrim_date:
            continue

        if _team_names_match(team1_name, selected_team_name):
            enemy_name = team2_name
        elif _team_names_match(team2_name, selected_team_name):
            enemy_name = team1_name
        else:
            warnings.append(f"Skipped row for {team1_name} vs {team2_name} on {scrim_date}: selected team not found in row.")
            continue

        buckets.setdefault((scrim_date, enemy_name), []).append(row)

    if not buckets:
        return [], warnings or ["No matching rows found for the selected team in CSV."]

    all_scrims: list[dict] = []

    for (scrim_date, enemy_name), bucket_rows in buckets.items():
        maps: list[dict] = []
        for row in bucket_rows:
            team1_name = _cell(row, _TEMPLATE_CSV_TEAM1)
            team2_name = _cell(row, _TEMPLATE_CSV_TEAM2)
            canonical_map = _match_map_name(_cell(row, _TEMPLATE_CSV_MAP))
            our_team_slot = "team1" if _team_names_match(team1_name, selected_team_name) else "team2"

            team1_score = _cell(row, _TEMPLATE_CSV_SCORE_TEAM1)
            team2_score = _cell(row, _TEMPLATE_CSV_SCORE_TEAM2)
            score_text = build_score_text(team1_score, team2_score)
            result_text = infer_result_from_score_text(score_text, slot=our_team_slot)
            if not result_text:
                result_text = _result_for_selected_team(_cell(row, _TEMPLATE_CSV_RESULT), team1_name, team2_name, our_team_slot)

            draft = {
                "team1": {
                    "ban1": _cell(row, _TEMPLATE_CSV_TEAM1_BAN1),
                    "ban2": _cell(row, _TEMPLATE_CSV_TEAM1_BAN2),
                    "protect1": _cell(row, _TEMPLATE_CSV_TEAM1_SAVE1),
                    "ban3": _cell(row, _TEMPLATE_CSV_TEAM1_BAN3),
                    "protect2": _cell(row, _TEMPLATE_CSV_TEAM1_SAVE2),
                    "ban4": _cell(row, _TEMPLATE_CSV_TEAM1_BAN4),
                },
                "team2": {
                    "ban1": _cell(row, _TEMPLATE_CSV_TEAM2_BAN1),
                    "ban2": _cell(row, _TEMPLATE_CSV_TEAM2_BAN2),
                    "protect1": _cell(row, _TEMPLATE_CSV_TEAM2_SAVE1),
                    "ban3": _cell(row, _TEMPLATE_CSV_TEAM2_BAN3),
                    "protect2": _cell(row, _TEMPLATE_CSV_TEAM2_SAVE2),
                    "ban4": _cell(row, _TEMPLATE_CSV_TEAM2_BAN4),
                },
            }

            comp_sections = build_default_comp_sections(canonical_map)
            if comp_sections:
                team1_comp = _build_team_comp(row, _TEMPLATE_CSV_TEAM1_PLAYERS)
                team2_comp = _build_team_comp(row, _TEMPLATE_CSV_TEAM2_PLAYERS)
                for idx, section in enumerate(comp_sections):
                    section["team1"] = copy.deepcopy(team1_comp)
                    section["team2"] = copy.deepcopy(team2_comp)
                    if idx == 0:
                        section["score"] = score_text

            maps.append(
                {
                    "map_name": canonical_map,
                    "map_type": normalize_map_type_value(_cell(row, _TEMPLATE_CSV_MAP_TYPE)),
                    "side": "",
                    "our_team_slot": our_team_slot,
                    "result": result_text,
                    "score": score_text,
                    "draft": draft,
                    "comp": comp_sections,
                    "notes": _cell(row, _TEMPLATE_CSV_NOTE),
                    "vod_url": "",
                    "events": [],
                    "team1_name": team1_name,
                    "team2_name": team2_name,
                }
            )

        if not maps:
            continue

        all_scrims.append(
            {
                "opponent": enemy_name,
                "enemy_team": enemy_name,
                "enemy_team_id": None,
                "scrim_date": scrim_date,
                "season": "",
                "team_id": team_id,
                "team_name": team_name,
                "notes": "",
                "maps": maps,
            }
        )

    return all_scrims, warnings


def parse_csv_into_scrims(
    raw_text: str,
    team_id: int | None,
    team_name: str,
) -> tuple[list[dict], list[str]]:
    reader = csv.reader(io.StringIO(raw_text))
    rows = list(reader)
    if not rows:
        return [], ["CSV file is empty."]

    header = [cell.strip().lower() for cell in rows[0]]
    is_template_csv = (
        len(header) >= _TEMPLATE_CSV_MIN_COLS
        and "replay code" in header
        and "map type" in header
        and "team 1" in header
        and "team 2" in header
        and "map" in header
    )

    if is_template_csv:
        return _parse_template_csv_into_scrims(raw_text, team_id, team_name)
    return _parse_csv_into_scrims(raw_text, team_id, team_name)


def summarize_import_warnings(warnings: list[str], *, preview_count: int = 5) -> str:
    if not warnings:
        return ""
    preview = warnings[:preview_count]
    remaining = len(warnings) - len(preview)
    message = " | ".join(preview)
    if remaining > 0:
        message += f" | {remaining} more warning(s)"
    return message


@app.route("/scrims/import-csv", methods=["POST"])
def import_csv_scrims():
    global NEXT_SCRIM_ID

    team_id  = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
    season = normalize_season_value(request.form.get("season", ""))
    if not team_name:
        flash("Please select your team before importing.", "error")
        return redirect(url_for("scrims"))
    if not season:
        flash("Please set a season for this import.", "error")
        return redirect(url_for("scrims"))

    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("No CSV file selected.", "error")
        return redirect(url_for("scrims"))

    ext = Path(file.filename).suffix.lower()
    if ext not in {".csv", ".txt", ".xlsx"}:
        flash("Only .csv, .txt, or .xlsx files are supported.", "error")
        return redirect(url_for("scrims"))

    try:
        file_bytes = file.read()
        if ext == ".xlsx":
            raw_text = _extract_csv_text_from_xlsx(file_bytes)
        else:
            raw_text = _parse_text_payload(file_bytes)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("scrims"))
    except Exception:
        flash("Could not parse the import file. Make sure the file is a valid CSV/TXT/XLSX export.", "error")
        return redirect(url_for("scrims"))

    parsed_scrims, warnings = parse_csv_into_scrims(raw_text, team_id, team_name)

    if not parsed_scrims:
        warning_summary = summarize_import_warnings(warnings)
        flash("No scrims could be imported from that CSV. " + warning_summary, "error")
        return redirect(url_for("scrims"))

    # Try to match enemy team names to existing teams in the global team database
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    enemy_rows = db.execute(
        "SELECT id, name FROM teams WHERE id != ?", (team_id,)
    ).fetchall() if team_id else []
    enemy_lookup: dict[str, int] = {}
    for row in enemy_rows:
        for key in _team_name_match_keys(row["name"]):
            enemy_lookup.setdefault(key, row["id"])

    imported = 0
    updated = 0
    for scrim in parsed_scrims:
        scrim["season"] = season
        normalize_scrim_record(scrim)
        _prepare_imported_scrim_context(scrim, team_id, team_name, enemy_lookup)
        _sync_scrim_rosters_with_database(scrim)

        if scrim.get("enemy_team_id"):
            for enemy_key in _team_name_match_keys(scrim["enemy_team"]):
                enemy_lookup[enemy_key] = scrim["enemy_team_id"]

        existing_scrim = _find_duplicate_scrim_for_import(scrim)
        if existing_scrim is not None:
            _merge_imported_scrim(existing_scrim, scrim)
            _assign_missing_scrim_ids(existing_scrim)
            updated += 1
            continue

        scrim["id"] = NEXT_SCRIM_ID
        NEXT_SCRIM_ID += 1
        _assign_missing_scrim_ids(scrim)
        SCRIMS.append(scrim)
        imported += 1

    save_app_state()

    msg_parts = []
    if imported:
        msg_parts.append(f"Imported {imported} scrim{'s' if imported != 1 else ''}")
    if updated:
        msg_parts.append(f"updated {updated} duplicate{'s' if updated != 1 else ''}")
    msg = ". ".join(msg_parts) + "."
    if warnings:
        msg += " Warnings: " + summarize_import_warnings(warnings)
    flash(msg, "success")
    return redirect(url_for("scrims"))


@app.route("/scrims/import-log", methods=["POST"])
def import_scrimcore_log_scrim():
    global NEXT_SCRIM_ID

    team_id = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
    season = normalize_season_value(request.form.get("season", ""))
    if not team_name:
        flash("Please select your team before importing a log.", "error")
        return redirect(url_for("scrims"))
    if not season:
        flash("Please set a season for this import.", "error")
        return redirect(url_for("scrims"))

    scrim_date = (request.form.get("scrim_date", "") or "").strip() or date.today().isoformat()
    enemy_name_input = (request.form.get("enemy_name", "") or "").strip()

    file = request.files.get("log_file")
    if not file or not file.filename:
        flash("No log file selected.", "error")
        return redirect(url_for("scrims"))

    ext = Path(file.filename).suffix.lower()
    if ext not in {".txt", ".csv", ".log", ".xlsx"}:
        flash("Only .txt, .csv, .log, or .xlsx files are supported for log import.", "error")
        return redirect(url_for("scrims"))

    try:
        file_bytes = file.read()
        if ext == ".xlsx":
            raw_text = _extract_csv_text_from_xlsx(file_bytes)
        else:
            raw_text = _parse_text_payload(file_bytes)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("scrims"))
    except Exception:
        flash("Could not read the log file. Make sure it is valid text/XLSX.", "error")
        return redirect(url_for("scrims"))

    parsed_rows, parser_summary = parse_scrimcore_log_text(raw_text)
    if not parsed_rows:
        flash("No parseable events were found in this log file.", "error")
        return redirect(url_for("scrims"))

    parsed_team1 = (parser_summary.get("team1_name") or "").strip()
    parsed_team2 = (parser_summary.get("team2_name") or "").strip()
    our_team_slot = "team1"
    if parsed_team2 and _team_names_match(parsed_team2, team_name):
        our_team_slot = "team2"

    inferred_enemy = enemy_name_input
    if not inferred_enemy:
        if our_team_slot == "team1":
            inferred_enemy = parsed_team2 or "Unknown Opponent"
        else:
            inferred_enemy = parsed_team1 or "Unknown Opponent"

    parsed_map_name = (parser_summary.get("map_name") or "").strip()
    map_name = _match_map_name(parsed_map_name) if parsed_map_name else "Unknown Map"
    timeline_events = _build_scrimcore_timeline_events(parsed_rows)

    map_entry = {
        "map_name": map_name,
        "map_type": DEFAULT_MAP_TYPE,
        "side": "",
        "our_team_slot": our_team_slot,
        "result": "",
        "score": "",
        "draft": {
            "team1": {"ban1": "", "ban2": "", "protect1": "", "ban3": "", "protect2": "", "ban4": ""},
            "team2": {"ban1": "", "ban2": "", "protect1": "", "ban3": "", "protect2": "", "ban4": ""},
        },
        "comp": build_default_comp_sections(map_name),
        "notes": "",
        "vod_url": "",
        "events": timeline_events,
        "parser_summary": parser_summary,
        "parser_source": "scrimcore-log-experiment",
        "team1_name": team_name if our_team_slot == "team1" else inferred_enemy,
        "team2_name": inferred_enemy if our_team_slot == "team1" else team_name,
    }

    scrim = {
        "opponent": inferred_enemy,
        "enemy_team": inferred_enemy,
        "enemy_team_id": None,
        "scrim_date": scrim_date,
        "season": season,
        "team_id": team_id,
        "team_name": team_name,
        "team_slot": our_team_slot,
        "notes": f"Imported from ScrimCore-style raw log parser experiment. Parsed {parser_summary.get('total_rows', 0)} rows across {parser_summary.get('event_type_count', 0)} event types.",
        "maps": [map_entry],
    }

    normalize_scrim_record(scrim)

    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    enemy_rows = db.execute(
        "SELECT id, name FROM teams WHERE id != ?", (team_id,)
    ).fetchall() if team_id else []
    enemy_lookup: dict[str, int] = {}
    for row in enemy_rows:
        for key in _team_name_match_keys(row["name"]):
            enemy_lookup.setdefault(key, row["id"])

    _prepare_imported_scrim_context(scrim, team_id, team_name, enemy_lookup)
    _sync_scrim_rosters_with_database(scrim)

    existing_scrim = _find_duplicate_scrim_for_import(scrim)
    cache_only_mode = _cache_only_log_import_mode()
    if existing_scrim is not None:
        _merge_imported_scrim(existing_scrim, scrim)
        _assign_missing_scrim_ids(existing_scrim)
        if not cache_only_mode:
            save_app_state()
            flash("Updated existing duplicate scrim with parsed log data.", "success")
        else:
            flash("Updated duplicate scrim in preview cache-only mode (not saved to DB).", "success")
        return redirect(url_for("scrim_detail", scrim_id=existing_scrim.get("id")))

    scrim["id"] = NEXT_SCRIM_ID
    NEXT_SCRIM_ID += 1
    _assign_missing_scrim_ids(scrim)
    SCRIMS.append(scrim)
    if not cache_only_mode:
        save_app_state()
        flash("Imported ScrimCore-style log into a new experimental scrim.", "success")
    else:
        flash("Imported ScrimCore-style log in preview cache-only mode (not saved to DB).", "success")
    return redirect(url_for("scrim_detail", scrim_id=scrim["id"]))


