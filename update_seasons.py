#!/usr/bin/env python3
"""
Update all scrims in the database with correct season values based on dates.

Season ranges:
- Season 7: March 20 and after
- Season 6.5: Feb 13 to March 19
- Season 6: Jan 16 to Feb 12
"""

import sqlite3
import json
from datetime import date
from pathlib import Path

def _parse_scrim_date(date_str: str) -> date | None:
    """Parse various date formats from scrim_date field."""
    if not date_str:
        return None
    
    # Try common formats
    formats = [
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%b %d, %Y",
        "%B %d, %Y",
    ]
    
    date_str = str(date_str).strip()
    for fmt in formats:
        try:
            return date.fromisoformat(date_str.replace("-", "-").split()[0]) if " " in date_str else __import__('datetime').datetime.strptime(date_str, fmt).date()
        except (ValueError, AttributeError):
            pass
    
    # Fallback for YYYY-MM-DD format
    try:
        parts = date_str.split("-")
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        pass
    
    return None

def get_season_from_date(scrim_date_str: str) -> str:
    """Determine season from scrim_date based on season windows."""
    parsed_date = _parse_scrim_date(scrim_date_str)
    if not parsed_date:
        return ""
    
    # Season 7: March 20 and after
    if parsed_date >= date(2026, 3, 20):
        return "7"
    # Season 6.5: Feb 13 to March 19
    elif parsed_date >= date(2026, 2, 13):
        return "6.5"
    # Season 6: Jan 16 to Feb 12
    elif parsed_date >= date(2026, 1, 16):
        return "6"
    return ""

def main():
    db_path = Path("rivals_stats.db")
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    try:
        # Get current scrims from app_state
        row = conn.execute(
            "SELECT state_value FROM app_state WHERE state_key = 'scrims'"
        ).fetchone()
        
        if not row:
            print("No scrims found in database")
            return
        
        scrims = json.loads(row["state_value"])
        if not isinstance(scrims, list):
            print("Invalid scrims format")
            return
        
        updated_count = 0
        for scrim in scrims:
            if not isinstance(scrim, dict):
                continue
            
            scrim_date = scrim.get("scrim_date", "")
            new_season = get_season_from_date(scrim_date)
            
            if new_season and scrim.get("season") != new_season:
                old_season = scrim.get("season", "")
                scrim["season"] = new_season
                updated_count += 1
                scrim_id = scrim.get("id", "?")
                print(f"Scrim {scrim_id} ({scrim_date}): {old_season or 'none'} → {new_season}")
        
        if updated_count > 0:
            # Save updated scrims back to database
            conn.execute(
                "UPDATE app_state SET state_value = ? WHERE state_key = 'scrims'",
                (json.dumps(scrims),)
            )
            conn.commit()
            print(f"\n✓ Updated {updated_count} scrims in database")
        else:
            print("No scrims needed updating")
    
    finally:
        conn.close()

if __name__ == "__main__":
    main()
