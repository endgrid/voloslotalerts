#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
List all available Volo volleyball pickup/drop-in openings and include raw start time.

Usage:
  python3 get_volo_openings.py

Optional env vars:
  LOCAL_TIMEZONE=America/Denver
"""

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib import request, error
from typing import Dict, Any, List

VOLO_GRAPHQL = "https://volosports.com/hapi/v1/graphql"

VENUE_IDS = [
    "6ef3e03d-9655-4102-9779-a717c28523ef",  # DU Gates Fieldhouse
    "ef20648e-2eb2-4eee-8a12-6faf00fccac9",  # Club Volo SoBo Indoor
    "8c856ee8-30f6-45ac-9f02-983178ba0722",  # Volo Sports Arena (formerly RiNo)
]

DISCOVER_QUERY = """
query DiscoverDaily($where: discover_daily_bool_exp!, $limit: Int = 100) {
  discover_daily(where: $where, limit: $limit) {
    game_id
    game {
      _id
      start_time
      venueByVenue { _id shorthand_name }
      drop_in_capacity { total_available_spots }
      leagueByLeague { _id name display_name program_type }
    }
    league_id
    league {
      _id
      name
      display_name
      program_type
      start_date
      start_time_estimate
      venueByVenue { _id shorthand_name }
      registrants_aggregate { aggregate { count } }
      registrationByRegistration { max_registration_size }
    }
    event_start_date
  }
}
""".strip()


def get_local_timezone():
    tz_name = os.environ.get("LOCAL_TIMEZONE", "America/Denver")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def format_datetime_pretty(dt: datetime) -> str:
    if not dt:
        return "TBD"
    return f"{dt.strftime('%B')} {dt.day} {int(dt.strftime('%I'))}{dt.strftime('%p')}"


def format_estimated(event_date: str, hhmm: str) -> str:
    if not event_date or not hhmm:
        return "TBD"
    try:
        d = datetime.strptime(event_date[:10], "%Y-%m-%d")
        h, m = map(int, hhmm.split(":"))
        local_tz = get_local_timezone()
        local_dt = d.replace(hour=h, minute=m, tzinfo=local_tz)
        return format_datetime_pretty(local_dt)
    except Exception:
        return "TBD"


def format_game_start(start_iso: str) -> str:
    if not start_iso:
        return "TBD"
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        local_dt = dt.astimezone(get_local_timezone())
        return format_datetime_pretty(local_dt)
    except Exception:
        return "TBD"


def post_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "operationName": "DiscoverDaily",
        "query": query,
        "variables": variables,
    }
    req = request.Request(
        VOLO_GRAPHQL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-hasura-role": "PLAYER"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(exc.read().decode())
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def build_where():
    return {
        "_or": [
            {
                "league_id": {"_is_null": False},
                "league": {
                    "organizationByOrganization": {"name": {"_eq": "Denver"}},
                    "sportBySport": {"name": {"_in": ["Volleyball"]}},
                    "program_type": {"_in": ["PICKUP"]},
                    "status": {"_eq": "registration_open"},
                    "registrationByRegistration": {"available_spots": {"_gte": 1}},
                    "venueByVenue": {"_id": {"_in": VENUE_IDS}},
                },
            },
            {
                "game_id": {"_is_null": False},
                "game": {
                    "leagueByLeague": {
                        "organizationByOrganization": {"name": {"_eq": "Denver"}},
                        "sportBySport": {"name": {"_in": ["Volleyball"]}},
                        "program_type": {"_in": ["PICKUP"]},
                    },
                    "venueByVenue": {"_id": {"_in": VENUE_IDS}},
                    "drop_in_capacity": {"total_available_spots": {"_gte": 1}},
                },
            },
        ]
    }


def find_open_events() -> List[Dict[str, Any]]:
    data = post_graphql(DISCOVER_QUERY, {"where": build_where()})
    rows = []
    for item in data.get("discover_daily", []):
        if item.get("game"):
            game = item["game"]
            spots = game["drop_in_capacity"]["total_available_spots"]
            if spots > 0:
                prog = game["leagueByLeague"]
                rows.append(
                    {
                        "type": "drop-in",
                        "program": prog.get("display_name") or prog.get("name"),
                        "venue": game["venueByVenue"]["shorthand_name"],
                        "when_local": format_game_start(game["start_time"]),
                        "raw_start_time": game["start_time"],
                        "available": spots,
                    }
                )
        elif item.get("league"):
            league = item["league"]
            reg = league["registrants_aggregate"]["aggregate"]["count"]
            cap = league["registrationByRegistration"]["max_registration_size"]
            avail = cap - reg
            if avail > 0:
                rows.append(
                    {
                        "type": "pickup",
                        "program": league.get("display_name") or league.get("name"),
                        "venue": league["venueByVenue"]["shorthand_name"],
                        "when_local": format_estimated(
                            item["event_start_date"], league["start_time_estimate"]
                        ),
                        "raw_start_time": {
                            "event_start_date": item["event_start_date"],
                            "start_time_estimate": league["start_time_estimate"],
                        },
                        "available": avail,
                    }
                )
    return rows


def main() -> None:
    events = find_open_events()
    if not events:
        print("No openings found.")
        return
    for event in events:
        print(
            f"{event['type'].upper():7} | {event['program']} | {event['venue']} | "
            f"{event['when_local']} | raw={event['raw_start_time']} | "
            f"{event['available']} spots"
        )


if __name__ == "__main__":
    main()
