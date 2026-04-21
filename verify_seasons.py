#!/usr/bin/env python3
"""Verify season assignments in the database."""

import sqlite3
import json

conn = sqlite3.connect('rivals_stats.db')
row = conn.execute('SELECT state_value FROM app_state WHERE state_key = "scrims"').fetchone()
scrims = json.loads(row[0])

season_groups = {'6': [], '6.5': [], '7': []}
for scrim in scrims:
    if isinstance(scrim, dict):
        season = scrim.get('season', 'none')
        if season in season_groups:
            date_str = scrim.get('scrim_date', 'no date')
            season_groups[season].append(f'  ID {scrim.get("id")}: {date_str}')

for season in ['6', '6.5', '7']:
    count = len(season_groups[season])
    print(f'Season {season}: {count} scrims')
    if count <= 5:
        for entry in season_groups[season]:
            print(entry)
    else:
        for entry in season_groups[season][:3]:
            print(entry)
        print(f'  ... and {count - 3} more')

conn.close()
