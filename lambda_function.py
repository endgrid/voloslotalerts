#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AWS Lambda: Volo "DiscoverDaily" notifier with DynamoDB de-dupe (NO time filtering)

What it does:
- Queries Volo's unauthenticated GraphQL API
- Finds ALL open Volleyball pickup / drop-in slots
- ONLY at:
    * University of Denver – Gates Fieldhouse (indoor)
      ID: 6ef3e03d-9655-4102-9779-a717c28523ef
    * Club Volo SoBo – Indoor
      ID: ef20648e-2eb2-4eee-8a12-6faf00fccac9
    * Volo Sports Arena (formerly RiNo Sports Arena)
      ID: 8c856ee8-30f6-45ac-9f02-983178ba0722
- NO date or time window (future events included)
- Only program_type = PICKUP
- Only events with available spots
- Uses DynamoDB to ensure you are alerted ONCE per unique event
- Sends SMS via SNS ONLY when a new event appears

Required Lambda environment variables:
    SNS_TOPIC_ARN   -> ARN of SNS topic (phone subscribed)
    DDB_TABLE_NAME  -> DynamoDB table name with PK: EventKey (String)
"""

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import List, Dict, Any

import boto3
from urllib import request, error

VOLO_GRAPHQL = "https://volosports.com/hapi/v1/graphql"

# Indoor volleyball venues only
VENUE_IDS = [
    "6ef3e03d-9655-4102-9779-a717c28523ef",  # DU Gates Fieldhouse
    "ef20648e-2eb2-4eee-8a12-6faf00fccac9",  # Club Volo SoBo Indoor
    "8c856ee8-30f6-45ac-9f02-983178ba0722",  # Volo Sports Arena (formerly RiNo)
]


# ---------- Formatting helpers ----------

def coalesce(a, b):
    return a if (a is not None and str(a).strip() != "") else b


def format_datetime_pretty(dt: datetime) -> str:
    if not dt:
        return "TBD"
    return f"{dt.strftime('%B')} {dt.day} {int(dt.strftime('%I'))}{dt.strftime('%p')}"

def get_local_timezone() -> timezone:
    tz_name = os.environ.get("LOCAL_TIMEZONE", "America/Denver")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def format_estimated(event_date: str, hhmm: str) -> str:
    if not event_date or not hhmm:
        return "TBD"
    try:
        d = datetime.strptime(event_date[:10], "%Y-%m-%d")
        h, m = map(int, hhmm.split(":"))
        dt = d.replace(hour=h, minute=m, tzinfo=timezone.utc)
        local_dt = dt.astimezone(get_local_timezone())
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


# ---------- GraphQL ----------

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


def post_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "operationName": "DiscoverDaily",
        "query": query,
        "variables": variables,
    }

    req = request.Request(
        VOLO_GRAPHQL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-hasura-role": "PLAYER",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        raise RuntimeError(e.read().decode())

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
                    "registrationByRegistration": {
                        "available_spots": {"_gte": 1}
                    },
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


# ---------- Core logic ----------

def find_open_events() -> List[Dict[str, Any]]:
    data = post_graphql(DISCOVER_QUERY, {"where": build_where()})
    rows = []

    for i in data.get("discover_daily", []):
        # Game-based
        if i.get("game"):
            g = i["game"]
            spots = g["drop_in_capacity"]["total_available_spots"]
            if spots > 0:
                prog = g["leagueByLeague"]
                when = format_game_start(g["start_time"])
                rows.append({
                    "ProgramName": coalesce(prog.get("display_name"), prog.get("name")),
                    "When": when,
                    "VenueName": g["venueByVenue"]["shorthand_name"],
                    "Available": spots,
                    "GameId": g["_id"],
                    "LeagueId": prog["_id"],
                })

        # League-based
        elif i.get("league"):
            l = i["league"]
            reg = l["registrants_aggregate"]["aggregate"]["count"]
            cap = l["registrationByRegistration"]["max_registration_size"]
            avail = cap - reg
            if avail > 0:
                when = format_estimated(i["event_start_date"], l["start_time_estimate"])
                rows.append({
                    "ProgramName": coalesce(l.get("display_name"), l.get("name")),
                    "When": when,
                    "VenueName": l["venueByVenue"]["shorthand_name"],
                    "Available": avail,
                    "GameId": None,
                    "LeagueId": l["_id"],
                })

    return rows


def compute_event_key(e):
    return f"GAME#{e['GameId']}" if e["GameId"] else f"LEAGUE#{e['LeagueId']}#{e['When']}"


# ---------- DynamoDB ----------

def get_existing_keys(table_name, keys):
    if not keys:
        return set()
    ddb = boto3.client("dynamodb")
    resp = ddb.batch_get_item(
        RequestItems={
            table_name: {
                "Keys": [{"EventKey": {"S": k}} for k in keys]
            }
        }
    )
    return {i["EventKey"]["S"] for i in resp["Responses"].get(table_name, [])}


def put_new_keys(table_name, events):
    ddb = boto3.client("dynamodb")
    now = datetime.now(timezone.utc).isoformat()
    for e in events:
        ddb.put_item(
            TableName=table_name,
            Item={
                "EventKey": {"S": e["EventKey"]},
                "CreatedAt": {"S": now},
            },
        )


# ---------- SNS ----------

def send_sms(topic_arn, events):
    lines = ["New Volo volleyball openings (DU / SoBo / Volo Sports Arena):"]
    for e in events:
        lines.append(f"- {e['ProgramName']} @ {e['VenueName']} {e['When']} ({e['Available']} spots)")
    boto3.client("sns").publish(
        TopicArn=topic_arn,
        Message="\n".join(lines),
        Subject="Volo Volleyball Alert",
    )


# ---------- Lambda handler ----------

def lambda_handler(event, context):
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    table = os.environ.get("DDB_TABLE_NAME")

    if not topic_arn or not table:
        return {"status": "missing_env_vars"}

    events = find_open_events()
    keys = [compute_event_key(e) for e in events]
    existing = get_existing_keys(table, keys)

    new_events = []
    for e, k in zip(events, keys):
        if k not in existing:
            e["EventKey"] = k
            new_events.append(e)

    if not new_events:
        return {"status": "ok", "new_events": 0}

    put_new_keys(table, new_events)
    send_sms(topic_arn, new_events)

    return {"status": "ok", "new_events": len(new_events)}
