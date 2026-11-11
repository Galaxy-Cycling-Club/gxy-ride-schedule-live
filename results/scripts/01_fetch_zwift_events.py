"""Fetch upcoming Galaxy (GXY) events from Zwift's public events feed and
maintain a rolling weekly ride schedule.

Zwift's public feed (no login) only reaches about 22 hours ahead, so this
script is run every few hours (GitHub Action). Each run:
  1. pages through the feed and keeps events run by the Galaxy club;
  2. merges them into the upcoming-events list;
  3. updates the weekly schedule: one "slot" per ride (name + weekday + UTC
     time). Slots not seen for STALE_DAYS are dropped, so retired rides
     disappear on their own and new rides appear on their own.

Times are stored in UTC. The dashboard shows them in the viewer's time zone,
so clock changes need no edits here.

Standard library only. Paths are relative to this script.
Run:  python results/scripts/01_fetch_zwift_events.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEED = "https://us-or-rly101.zwift.com/api/public/events/upcoming?limit=200&start={start}"
GXY_CLUB_ID = "0d04ac16-6fad-4905-8407-98969e30595d"
MAX_PAGES = 10          # feed currently ends after ~3 pages
STALE_DAYS = 8          # drop a weekly slot not seen for this many days
KEEP_PAST_HOURS = 3     # keep events in the upcoming list until 3 h after start

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
OUT_FILES = {
    "events": [PROJECT / "data" / "processed" / "gxy-events.json",
               PROJECT / "site" / "data" / "gxy-events.json"],
    "schedule": [PROJECT / "data" / "processed" / "gxy-schedule.json",
                 PROJECT / "site" / "data" / "gxy-schedule.json"],
}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    # Zwift format: 2026-11-11T11:45:00.000+0000
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc)


def get_json(url: str, tries: int = 3):
    req = urllib.request.Request(url, headers={
        "User-Agent": "gxy-ride-schedule/1.0 (club ride dashboard)",
        "Accept": "application/json",
    })
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1:
                raise
            print(f"  retry after error: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def fetch_feed() -> list[dict]:
    events: list[dict] = []
    for page in range(MAX_PAGES):
        batch = get_json(FEED.format(start=page * 200))
        if not batch:
            break
        events.extend(batch)
        time.sleep(1)  # be polite
    return events


def first_paragraph(text: str, limit: int = 400) -> str:
    text = (text or "").replace("\r", "")
    text = re.sub(r"^\s*EVENT DESCRIPTION\s*", "", text, flags=re.I)
    para = text.strip().split("\n\n")[0].strip()
    return para[:limit] + ("…" if len(para) > limit else "")


def simplify(e: dict) -> dict:
    """Keep the fields the dashboard needs."""
    start = parse_time(e["eventStart"])
    tags = e.get("tags") or []
    pens = []
    for s in e.get("eventSubgroups") or []:
        pens.append({
            "label": s.get("subgroupLabel"),
            "wkg_from": s.get("fromPaceValue"),
            "wkg_to": s.get("toPaceValue"),
            "range": s.get("rangeAccessLabel"),
            "route_id": s.get("routeId"),
            "laps": s.get("laps"),
            "distance_m": s.get("distanceInMeters"),
            "duration_s": s.get("durationInSeconds"),
            "signups": s.get("totalSignedUpCount"),
        })
    name = (e.get("name") or "").strip()
    return {
        "id": e["id"],
        "name": name,
        "start": iso(start),
        "type": e.get("eventType"),
        "sport": e.get("sport"),
        "route_id": e.get("routeId"),
        "map_id": e.get("mapId"),
        "laps": e.get("laps"),
        "distance_m": e.get("distanceInMeters"),
        "duration_s": e.get("durationInSeconds"),
        "pens": pens,
        "banded": "BANDED" in name.upper() or e.get("rangeAccessLabel") is not None,
        "jersey_unlock": "jerseyunlock" in tags,
        "category_enforcement": e.get("categoryEnforcement"),
        "signups": e.get("totalSignedUpCount"),
        "summary": first_paragraph(e.get("description", "")),
        "description": (e.get("description") or "").strip(),
        "image": e.get("imageUrl"),
        "template_id": e.get("eventTemplateId"),
        "tags": [t for t in tags if "=" not in t],
        "url": f"https://www.zwift.com/events/view/{e['id']}",
    }


def slot_key(ev: dict) -> str:
    start = datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%SZ")
    return f"{ev['name'].upper()}|{start.weekday()}|{start:%H:%M}"


def load(path: Path, default):
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return default


def save(paths: list[Path], data) -> None:
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        tmp.replace(p)


def main() -> int:
    run_at = now_utc()
    print(f"Fetching Zwift public events feed at {iso(run_at)}")
    feed = fetch_feed()
    gxy = [simplify(e) for e in feed if e.get("microserviceExternalResourceId") == GXY_CLUB_ID]
    feed_end = max((e["eventStart"] for e in feed), default=None)
    print(f"  {len(feed)} events in feed (to {feed_end}); {len(gxy)} Galaxy events")
    if not feed:
        print("Feed empty: leaving data unchanged", file=sys.stderr)
        return 1

    # 1. Upcoming events: merge by id, newest data wins, drop finished ones
    ev_store = load(OUT_FILES["events"][0], {"events": []})
    by_id = {e["id"]: e for e in ev_store.get("events", [])}
    for e in gxy:
        by_id[e["id"]] = e
    cutoff = run_at - timedelta(hours=KEEP_PAST_HOURS)
    events = sorted(
        (e for e in by_id.values() if datetime.strptime(e["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) >= cutoff),
        key=lambda e: e["start"],
    )

    # 2. Weekly schedule slots
    sch_store = load(OUT_FILES["schedule"][0], {"slots": []})
    slots = {s["key"]: s for s in sch_store.get("slots", [])}
    for e in gxy:
        start = datetime.strptime(e["start"], "%Y-%m-%dT%H:%M:%SZ")
        key = slot_key(e)
        old = slots.get(key, {})
        slot = {k: v for k, v in e.items() if k not in ("id", "start", "signups", "url")}
        slot.update({
            "key": key,
            "weekday_utc": start.weekday(),
            "weekday_name_utc": WEEKDAYS[start.weekday()],
            "time_utc": f"{start:%H:%M}",
            "next_start": e["start"],
            "next_url": e["url"],
            "first_seen": old.get("first_seen", iso(run_at)),
            "last_seen": iso(run_at),
            "times_seen": old.get("times_seen", 0) + 1,
            "source": "zwift-public-feed",
        })
        slots[key] = slot
    stale_before = run_at - timedelta(days=STALE_DAYS)
    dropped = [k for k, s in slots.items()
               if s.get("source") == "zwift-public-feed"
               and datetime.strptime(s["last_seen"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < stale_before]
    for k in dropped:
        del slots[k]
    ordered = sorted(slots.values(), key=lambda s: (s["weekday_utc"], s["time_utc"], s["name"]))

    first_run = sch_store.get("collecting_since", iso(run_at))
    meta = {
        "updated": iso(run_at),
        "collecting_since": first_run,
        "feed_reaches": feed_end,
        "club_id": GXY_CLUB_ID,
        "source": "Zwift public events feed (api/public/events/upcoming)",
    }
    save(OUT_FILES["events"], {**meta, "events": events})
    save(OUT_FILES["schedule"], {**meta, "stale_days": STALE_DAYS, "slots": ordered})
    print(f"  {len(events)} upcoming events saved; {len(ordered)} weekly slots ({len(dropped)} dropped as stale)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
