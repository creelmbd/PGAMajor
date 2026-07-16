#!/usr/bin/env python3
"""Fetches The Open Championship leaderboard scores from ESPN and writes scores.json."""

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

TOURNAMENT_START = datetime.datetime(2026, 7, 16, 6, 0, tzinfo=datetime.timezone.utc)
TOURNAMENT_END = datetime.datetime(2026, 7, 20, 4, 0, tzinfo=datetime.timezone.utc)  # early Monday UTC after Sunday final


def is_within_play_window(now=None):
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    # Typical Open Championship tee times start around 7am BST = 06:00 UTC; rounds finish ~midnight BST.
    # Use UTC for a consistent schedule regardless of local machine timezone.
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
        # ESPN sets state="post" between rounds too, not just after R4.
        # Only call it "final" when period == 4 (all 4 rounds complete).
        try:
            if int(period) >= 4:
                return "final"
            else:
                return f"round{int(period)}"
        except Exception:
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


def load_existing_output():
    """Load the full existing scores.json, returning empty dict on failure."""
    if not OUTPUT_FILE.exists():
        return {}
    try:
        return json.loads(OUTPUT_FILE.read_text())
    except Exception:
        return {}


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    if not is_within_play_window(now):
        print(f"Outside tournament play window ({now.isoformat()}) — skipping update.")
        return

    print("Fetching The Open leaderboard from ESPN...")
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

        # Extract thru (holes completed)
        comp_status = comp.get("status", {}) or {}
        state = str((comp_status.get("type") or {}).get("state", "")).lower()
        if state == "post":
            thru = "F"
        else:
            display_thru = comp_status.get("displayThru", "")
            thru_raw = comp_status.get("thru")
            if display_thru:
                thru = str(display_thru)
            elif thru_raw:
                thru = str(thru_raw)
            else:
                thru = None

        if score_value is None and status == "active":
            scores[name] = {"score": None, "status": status, "thru": thru}
        else:
            scores[name] = {"score": score_value, "status": status, "thru": thru}

    # Only carry a champion when the tournament is finished
    if tournament_status == "final" and leader:
        champion = leader
    else:
        champion = ""

    # Preserve or create cutScores: frozen R2 per-player scores used for Score at Cut.
    # Once written (when transitioning to round3), cutScores is never overwritten.
    existing = load_existing_output()
    existing_cut_scores = existing.get("cutScores", {})
    existing_status = existing.get("tournamentStatus", "")

    # Write cutScores when we first reach round3 (freeze the R2 scores)
    if tournament_status == "round3" and not existing_cut_scores:
        # Snapshot current scores as the frozen cut scores
        cut_scores = {name: info["score"] for name, info in scores.items() if info["score"] is not None}
        print(f"Freezing cutScores for {len(cut_scores)} players at start of R3")
    elif existing_cut_scores:
        # Already frozen — preserve forever
        cut_scores = existing_cut_scores
    else:
        cut_scores = {}

    output = {
        "scores": scores,
        "cutScores": cut_scores,
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