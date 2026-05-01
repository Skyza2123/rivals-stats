from collections import defaultdict
from datetime import date, datetime, timedelta


def _parse_activity_date(raw_value: str) -> date | None:
    raw_text = (raw_value or "").strip()
    if not raw_text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw_text, fmt).date()
        except ValueError:
            continue
    return None


def _build_activity_heatmap(map_rows: list[dict], *, days: int = 182) -> dict:
    dated_rows: list[tuple[date, dict]] = []
    for row in map_rows:
        parsed = _parse_activity_date(row.get("scrim_date", ""))
        if parsed is not None:
            dated_rows.append((parsed, row))
    if not dated_rows:
        return {"month_labels": [], "weeks": [], "max_count": 0}

    latest_day = max(parsed for parsed, _row in dated_rows)
    start_day = latest_day - timedelta(days=days - 1)
    start_day -= timedelta(days=start_day.weekday())
    end_day = latest_day + timedelta(days=6 - latest_day.weekday())

    by_day: dict[date, dict] = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "unresolved": 0})
    for parsed, row in dated_rows:
        if parsed < start_day or parsed > end_day:
            continue
        bucket = by_day[parsed]
        bucket["count"] += 1
        if row.get("outcome") == "Win":
            bucket["wins"] += 1
        elif row.get("outcome") == "Loss":
            bucket["losses"] += 1
        else:
            bucket["unresolved"] += 1

    max_count = max((bucket["count"] for bucket in by_day.values()), default=0)
    weeks = []
    cursor = start_day
    while cursor <= end_day:
        week_days = []
        for offset in range(7):
            day = cursor + timedelta(days=offset)
            bucket = by_day.get(day, {"count": 0, "wins": 0, "losses": 0, "unresolved": 0})
            count = bucket["count"]
            intensity = 0 if not count or not max_count else max(1, min(4, round((count / max_count) * 4)))
            if bucket["wins"] > bucket["losses"]:
                outcome = "win"
            elif bucket["losses"] > bucket["wins"]:
                outcome = "loss"
            elif count:
                outcome = "neutral"
            else:
                outcome = "empty"
            week_days.append(
                {
                    "date": day.isoformat(),
                    "label": f"{day.strftime('%b')} {day.day}",
                    "count": count,
                    "wins": bucket["wins"],
                    "losses": bucket["losses"],
                    "unresolved": bucket["unresolved"],
                    "intensity": intensity,
                    "outcome": outcome,
                }
            )
        weeks.append(week_days)
        cursor += timedelta(days=7)

    month_labels = []
    previous_month = None
    for index, week in enumerate(weeks, start=1):
        month_name = week[0]["date"][5:7]
        if month_name != previous_month:
            label_day = datetime.strptime(week[0]["date"], "%Y-%m-%d").date()
            month_labels.append({"label": label_day.strftime("%b"), "column": index})
            previous_month = month_name

    return {"month_labels": month_labels, "weeks": weeks, "max_count": max_count}


def build_team_map_overview(
    team_scrims: list[dict],
    *,
    map_modes: dict[str, str],
    map_types: list[str] | tuple[str, ...],
    normalize_map_type_value,
    get_map_outcome_for_slot,
    get_map_image_url,
) -> dict:
    map_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    mode_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    map_type_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    opponent_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    recent_map_visual_rows: list[dict] = []
    map_timeline_targets: dict[str, int] = {}

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name", "") or "").strip()
            if not map_name:
                continue

            if map_name not in map_timeline_targets and scrim.get("id") is not None:
                map_timeline_targets[map_name] = scrim.get("id")

            mode_name = map_modes.get(map_name, "Other")
            map_type_name = normalize_map_type_value(map_entry.get("map_type", ""))
            outcome = get_map_outcome_for_slot(map_entry, map_entry.get("our_team_slot", "team1"))
            opponent_name = ((scrim.get("enemy_team", "") or "").strip() or (scrim.get("opponent", "") or "").strip() or "Opponent")

            map_records[map_name]["maps"] += 1
            mode_records[mode_name]["maps"] += 1
            map_type_records[map_type_name]["maps"] += 1
            opponent_records[opponent_name]["maps"] += 1

            recent_map_visual_rows.append(
                {
                    "scrim_id": scrim.get("id"),
                    "scrim_date": scrim.get("scrim_date", ""),
                    "map_name": map_name,
                    "mode": mode_name,
                    "map_type": map_type_name,
                    "outcome": outcome,
                    "opponent": opponent_name,
                }
            )

            if outcome == "Win":
                map_records[map_name]["wins"] += 1
                map_records[map_name]["decided"] += 1
                mode_records[mode_name]["wins"] += 1
                mode_records[mode_name]["decided"] += 1
                map_type_records[map_type_name]["wins"] += 1
                map_type_records[map_type_name]["decided"] += 1
                opponent_records[opponent_name]["wins"] += 1
                opponent_records[opponent_name]["decided"] += 1
            elif outcome == "Loss":
                map_records[map_name]["losses"] += 1
                map_records[map_name]["decided"] += 1
                mode_records[mode_name]["losses"] += 1
                mode_records[mode_name]["decided"] += 1
                map_type_records[map_type_name]["losses"] += 1
                map_type_records[map_type_name]["decided"] += 1
                opponent_records[opponent_name]["losses"] += 1
                opponent_records[opponent_name]["decided"] += 1
            else:
                map_records[map_name]["unresolved"] += 1
                mode_records[mode_name]["unresolved"] += 1
                map_type_records[map_type_name]["unresolved"] += 1
                opponent_records[opponent_name]["unresolved"] += 1

    team_map_cards = []
    for map_name, stats in map_records.items():
        decided_maps = stats["decided"]
        win_rate = round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0
        team_map_cards.append(
            {
                "map_name": map_name,
                "mode": map_modes.get(map_name, "Other"),
                "maps": stats["maps"],
                "decided_maps": decided_maps,
                "unresolved_maps": stats["unresolved"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "image": get_map_image_url(map_name),
                "timeline_scrim_id": map_timeline_targets.get(map_name),
            }
        )
    team_map_cards.sort(key=lambda row: (row["win_rate"], row["maps"]), reverse=True)

    team_map_mode_rows = []
    for mode_name, stats in mode_records.items():
        decided_maps = stats["decided"]
        win_rate = round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0
        mode_maps = [card for card in team_map_cards if card["mode"] == mode_name]
        best_map = max(mode_maps, key=lambda row: (row["win_rate"], row["maps"]), default=None)
        worst_map = min(mode_maps, key=lambda row: (row["win_rate"], -row["maps"]), default=None)
        team_map_mode_rows.append(
            {
                "mode": mode_name,
                "maps": stats["maps"],
                "decided_maps": decided_maps,
                "unresolved_maps": stats["unresolved"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "best_map": best_map,
                "worst_map": worst_map,
            }
        )
    team_map_mode_rows.sort(key=lambda row: (row["win_rate"], row["maps"]), reverse=True)

    best_mode = team_map_mode_rows[0] if team_map_mode_rows else None
    worst_mode = team_map_mode_rows[-1] if team_map_mode_rows else None

    map_type_visual_rows = []
    for map_type in map_types:
        stats = map_type_records.get(map_type)
        if not stats or not stats["maps"]:
            continue
        decided_maps = stats["decided"]
        win_rate = round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0
        map_type_visual_rows.append(
            {
                "map_type": map_type,
                "maps": stats["maps"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "unresolved": stats["unresolved"],
                "win_rate": win_rate,
            }
        )

    total_maps_for_type_visual = sum(row["maps"] for row in map_type_visual_rows)
    for row in map_type_visual_rows:
        row["share"] = round((row["maps"] / total_maps_for_type_visual) * 100, 1) if total_maps_for_type_visual else 0
    map_type_visual_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

    opponent_visual_rows = []
    for opponent_name, stats in opponent_records.items():
        decided_maps = stats["decided"]
        win_rate = round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0
        opponent_visual_rows.append(
            {
                "opponent": opponent_name,
                "maps": stats["maps"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "unresolved": stats["unresolved"],
                "win_rate": win_rate,
            }
        )
    opponent_visual_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)
    opponent_visual_rows = opponent_visual_rows[:8]

    activity_heatmap = _build_activity_heatmap(recent_map_visual_rows)
    recent_map_visual_rows = list(reversed(recent_map_visual_rows[-24:]))

    return {
        "team_map_cards": team_map_cards,
        "team_map_mode_rows": team_map_mode_rows,
        "best_mode": best_mode,
        "worst_mode": worst_mode,
        "map_type_visual_rows": map_type_visual_rows,
        "opponent_visual_rows": opponent_visual_rows,
        "recent_map_visual_rows": recent_map_visual_rows,
        "activity_heatmap": activity_heatmap,
    }
