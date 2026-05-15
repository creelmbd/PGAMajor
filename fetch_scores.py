#!/usr/bin/env python3
"""Fetches PGA Championship leaderboard scores from ESPN and writes scores.json."""

import datetime
import json
import os
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Installing requests...")
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

OUTPUT_FILE = Path(__file__).parent / "scores.json"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard"
USER_AGENT = "Mozilla/5.0 (compatible; GitHubActions/1.0; +https://github.com)"

TOURNAMENT_START = datetime.datetime(2026, 5, 14, 12, 0, tzinfo=datetime.timezone.utc)
TOURNAMENT_END = datetime.datetime(2026, 5, 18, 4, 0, tzinfo=datetime.timezone.utc)  # midnight ET Sunday


def is_within_play_window(now=None):
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    # PGA tee times start ~7am ET = 11am UTC; rounds finish ~midnight UTC.
    # Remove the hour restriction so early starters aren't missed.
    return TOURNAMENT_START <= now <= TOURNAMENT_END


def fetch_leaderboard_json():
    response = requests.get(ESPN_URL, timeout=25, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return response.json()


def parse_score_value(score_obj):
    """Fallback: parse displayValue from raw score object."""
    if not score_obj:
        return None
    display = str(score_obj.get("displayValue", "")).strip()
    if display in ("", "-", "--"):
        return None
    if display.upper() == "E":
        return 0
    try:
        return int(float(display.replace("+", "").replace("−", "-")))
    except Exception:
        try:
            return int(float(score_obj.get("value")))
        except Exception:
            return None


def parse_score_to_par(comp):
    """
    Read scoreToPar from the competitor's statistics array — this is the
    correct to-par value. ESPN's comp['score'] holds raw stroke totals,
    not to-par, so we must use statistics instead.
    """
    for stat in comp.get("statistics", []):
        if stat.get("name") == "scoreToPar":
            display = str(stat.get("displayValue", "")).strip()
            if display in ("", "-", "--"):
                return None
            if display.upper() == "E":
                return 0
            try:
                return int(float(display.replace("+", "").replace("−", "-")))
            except Exception:
                try:
                    val = stat.get("value")
                    if val is not None:
                        return int(float(val))
                except Exception:
                    pass
    # Fallback to raw score field if statistics missing
    return parse_score_value(comp.get("score", {}))


def has_cut_status(status_obj):
    if not status_obj:
        return False
    typ = status_obj.get("type", {}) or {}
    name = str(typ.get("name", "")).upper()
    detail = str(status_obj.get("detail", "")).upper()
    position = str(status_obj.get("position", {}).get("displayName", "")).upper()
    for term in ("CUT", "OUT", "WD", "WITHDRAWN"):
        if term in name or term in detail or position == term:
            return True
    return False


def format_tournament_status(event):
    competition = event.get("competitions", [{}])[0]
    status = competition.get("status", {}) or {}
    typ = status.get("type", {}) or {}
    state = str(typ.get("state", "")).lower()
    period = status.get("period")

    if state == "pre":
        return "pre"
    if state == "in":
        try:
            period_int = int(period)
            return f"round{period_int}"
        except Exception:
            return "round1"
    if state == "post":
        return "final"
    if state:
        return state
    return "unknown"


def rank_key(comp):
    position = str(comp.get("status", {}).get("position", {}).get("displayName", ""))
    match = re.match(r"^T?(\d+)$", position)
    return int(match.group(1)) if match else 9999


def parse_leader(comps):
    sorted_comps = sorted(comps, key=rank_key)
    if sorted_comps:
        athlete = sorted_comps[0].get("athlete", {})
        return athlete.get("displayName") or athlete.get("fullName") or ""
    return ""


def parse_top_players(comps, n=5):
    sorted_comps = sorted(comps, key=rank_key)
    result = []
    for comp in sorted_comps[:n]:
        athlete = comp.get("athlete", {})
        name = athlete.get("displayName") or athlete.get("fullName") or ""
        if not name:
            continue
        status = comp.get("status", {}) or {}
        position = status.get("position", {}).get("displayName", "")
        display_thru = status.get("displayThru", "")
        thru = status.get("thru")
        state = str((status.get("type") or {}).get("state", "")).lower()
        if state == "post":
            thru_label = "F"
        elif display_thru:
            thru_label = str(display_thru)
        elif thru:
            thru_label = str(thru)
        else:
            thru_label = "-"
        score = parse_score_to_par(comp)
        result.append({
            "name": name,
            "position": position,
            "score": score,
            "thru": thru_label,
        })
    return result



def load_existing_champion():
    if not OUTPUT_FILE.exists():
        return ""
    try:
        data = json.loads(OUTPUT_FILE.read_text())
        return data.get("champion", "")
    except Exception:
        return ""


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    if not is_within_play_window(now):
        print(f"Outside tournament play window ({now.isoformat()}) — skipping update.")
        return

    print("Fetching PGA Championship leaderboard from ESPN...")
    data = fetch_leaderboard_json()
    events = data.get("events", [])
    if not events:
        raise RuntimeError("No event data returned from ESPN API")

    event = events[0]
    competition = event.get("competitions", [])[0]
    competitors = competition.get("competitors", [])

    if not competitors:
        raise RuntimeError("No competitors found in ESPN leaderboard response")

    tournament_status = format_tournament_status(event)
    leader = parse_leader(competitors)
    top_players = parse_top_players(competitors, 5)

    scores = {}
    for comp in competitors:
        athlete = comp.get("athlete", {})
        name = athlete.get("displayName") or athlete.get("fullName")
        if not name:
            continue
        score_value = parse_score_to_par(comp)
        status = "cut" if has_cut_status(comp.get("status", {})) else "active"
        if score_value is None and status == "active":
            # If event hasn't started yet, keep status active but no score.
            scores[name] = {"score": None, "status": status}
        else:
            scores[name] = {"score": score_value, "status": status}

    # Only carry a champion when the tournament is finished
    if tournament_status == "final" and leader:
        champion = leader
    else:
        champion = ""

    output = {
        "scores": scores,
        "champion": champion,
        "leader": leader,
        "topPlayers": top_players,
        "tournamentStatus": tournament_status,
        "exportedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "ESPN leaderboard",
        "url": ESPN_URL,
        "note": "Auto-updated by GitHub Actions schedule."
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2) + "\n")
    print(f"Wrote {OUTPUT_FILE} with {len(scores)} players")
    print(f"Tournament status: {tournament_status}")
    print(f"Leader: {leader}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)