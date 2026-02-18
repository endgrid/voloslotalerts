#!/usr/bin/env python3
"""GraphQL connectivity probe for local runs and AWS Lambda test events.

Purpose:
- Confirm whether requests to Volo's GraphQL endpoint succeed from the current runtime.
- Surface HTTP status/body snippets (including Cloudflare-style 1010 failures) for debugging.
- Optionally run the same DiscoverDaily query shape used by production Lambda logic.

Usage (local):
  python connectivity_probe.py

Usage (Lambda):
  Set handler to `connectivity_probe.lambda_handler` and invoke with any test payload.

Optional environment variables:
- PROBE_TIMEOUT_SECONDS (default: 20)
- PROBE_ENDPOINT (default: https://volosports.com/hapi/v1/graphql)
- PROBE_USER_AGENT (default: empty)
- PROBE_MODE (default: minimal, allowed: minimal|discover)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Tuple
from urllib import error, request

PROBE_ENDPOINT = os.environ.get("PROBE_ENDPOINT", "https://volosports.com/hapi/v1/graphql")
PROBE_TIMEOUT_SECONDS = int(os.environ.get("PROBE_TIMEOUT_SECONDS", "20"))
PROBE_USER_AGENT = os.environ.get("PROBE_USER_AGENT", "")
PROBE_MODE = os.environ.get("PROBE_MODE", "minimal").strip().lower()

VENUE_IDS = [
    "6ef3e03d-9655-4102-9779-a717c28523ef",  # DU Gates Fieldhouse
    "ef20648e-2eb2-4eee-8a12-6faf00fccac9",  # Club Volo SoBo Indoor
    "8c856ee8-30f6-45ac-9f02-983178ba0722",  # Volo Sports Arena
]

MINIMAL_QUERY = "query Probe { __typename }"
DISCOVER_QUERY = """
query DiscoverDaily($where: discover_daily_bool_exp!, $limit: Int = 10) {
  discover_daily(where: $where, limit: $limit) {
    game_id
    league_id
    event_start_date
  }
}
""".strip()


def build_where() -> Dict[str, Any]:
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


def _build_query_payload() -> Tuple[str, Dict[str, Any], str]:
    if PROBE_MODE == "discover":
        return "DiscoverDaily", DISCOVER_QUERY, {"where": build_where(), "limit": 10}
    return "Probe", MINIMAL_QUERY, {}


def _build_request() -> request.Request:
    operation_name, query, variables = _build_query_payload()
    payload = {
        "operationName": operation_name,
        "query": query,
        "variables": variables,
    }

    headers = {
        "Content-Type": "application/json",
        "x-hasura-role": "PLAYER",
    }
    if PROBE_USER_AGENT:
        headers["User-Agent"] = PROBE_USER_AGENT

    return request.Request(
        PROBE_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _detect_common_blockers(http_status: int | None, body_text: str, response_headers: Dict[str, Any]) -> Dict[str, Any]:
    body_lc = (body_text or "").lower()
    server_lc = str(response_headers.get("Server", "")).lower()
    is_cloudflare = "cloudflare" in server_lc or any(h.startswith("CF-") for h in response_headers.keys())
    is_1010 = "error code: 1010" in body_lc

    diagnosis = {
        "is_cloudflare": is_cloudflare,
        "is_1010": is_1010,
        "classification": "unknown",
        "next_step": "Collect this JSON from local and Lambda to compare egress behavior.",
    }

    if http_status == 403 and is_1010:
        diagnosis["classification"] = "blocked_by_cloudflare_access_rules"
        diagnosis["next_step"] = (
            "Request-path is being denied before GraphQL executes. Try a different egress IP/runtime "
            "or coordinate with endpoint owner for allowlisting/official API access."
        )
    elif http_status and http_status >= 500:
        diagnosis["classification"] = "upstream_server_error"
    elif http_status and 400 <= http_status < 500:
        diagnosis["classification"] = "client_or_access_error"
    elif http_status and 200 <= http_status < 300:
        diagnosis["classification"] = "request_reached_endpoint"

    return diagnosis


def run_probe() -> Dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    req = _build_request()

    try:
        with request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")
            response_headers = dict(resp.headers.items())
            parsed = None
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                parsed = None

            return {
                "ok": True,
                "started_at": started_at,
                "endpoint": PROBE_ENDPOINT,
                "mode": PROBE_MODE,
                "http_status": resp.getcode(),
                "response_headers": response_headers,
                "body_preview": body_text[:500],
                "graphql_errors": parsed.get("errors") if isinstance(parsed, dict) else None,
                "diagnosis": _detect_common_blockers(resp.getcode(), body_text, response_headers),
            }
    except error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        return {
            "ok": False,
            "started_at": started_at,
            "endpoint": PROBE_ENDPOINT,
            "mode": PROBE_MODE,
            "http_status": exc.code,
            "reason": exc.reason,
            "response_headers": response_headers,
            "body_preview": err_body[:1000],
            "diagnosis": _detect_common_blockers(exc.code, err_body, response_headers),
        }
    except Exception as exc:  # broad by design for diagnostics
        return {
            "ok": False,
            "started_at": started_at,
            "endpoint": PROBE_ENDPOINT,
            "mode": PROBE_MODE,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "diagnosis": {
                "classification": "runtime_network_or_environment_error",
                "next_step": "Validate local DNS/proxy/firewall settings and compare with Lambda runtime.",
            },
        }


def lambda_handler(event, context):
    result = run_probe()
    result["event_echo"] = event
    return result


if __name__ == "__main__":
    print(json.dumps(run_probe(), indent=2, sort_keys=True))
