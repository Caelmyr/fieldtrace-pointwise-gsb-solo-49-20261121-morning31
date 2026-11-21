"""Compare two stations only during hours when both cameras were recording."""

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from .contracts import SurveyError, utc_text, word
from .effort import (
    difference_intervals, event_status, effort_inputs, intersect_intervals, local_day_range,
    measure_hours, merged_intervals, station_intervals,
)

LIGHT_CLASSES = (("day", "Daytime", "day-window"), ("night", "Night", "night-window"))
MIN_BLOCK_MINUTES = 15
MAX_UNPAIRED_BLOCKS = 120
MAX_BLOCK_EVENTS = 40


def pairing_inputs(form):
    info = effort_inputs(form)
    keys = info["stations"]
    if len(keys) != 2:
        raise SurveyError("Choose exactly two stations to compare.", field="stations")
    if keys[0] == keys[1]:
        raise SurveyError("Choose two different stations to compare.", field="stations")
    return info


def light_class(stamp, zone, day_from, day_to):
    local = stamp.astimezone(zone)
    minute = local.hour * 60 + local.minute
    return "day" if day_from * 60 <= minute < day_to * 60 else "night"


def station_classes(db, station, selected):
    span = local_day_range(selected["first_day"], selected["last_day"], station["timezone"])
    deployments = db.matching("deployments", where=(("station_id", "=", station["id"]),))
    parts = {key: [] for key, _, _ in LIGHT_CLASSES}
    for camera in deployments:
        for key, _, mode in LIGHT_CLASSES:
            windows = station_intervals(db, station, camera, selected | {"mode": mode}, span)
            parts[key].extend(windows["available"])
    return {key: merged_intervals(values) for key, values in parts.items()}


def station_events(db, station, selected):
    zone = ZoneInfo(station["timezone"])
    span = local_day_range(selected["first_day"], selected["last_day"], station["timezone"])
    deployments = db.matching("deployments", where=(("station_id", "=", station["id"]),))
    result = []
    for camera in deployments:
        windows = station_intervals(db, station, camera, selected | {"mode": "active"}, span)
        rows = db.matching("events", where=(
            ("deployment_id", "=", camera["id"]), ("stamp_utc", ">=", utc_text(span[0])),
            ("stamp_utc", "<", utc_text(span[1])),
        ), order=("stamp_utc", "ASC"))
        for event in rows:
            if event["state"] != "confirmed":
                continue
            stamp = datetime.fromisoformat(event["stamp_utc"])
            if not any(start <= stamp < end for start, end in windows["available"]):
                continue
            status, total, reviewed = event_status(db, event, selected["taxon"])
            result.append({
                "event": event, "stamp": stamp, "outcome": status, "frames": total, "reviewed": reviewed,
                "light": light_class(stamp, zone, selected["day_from"], selected["day_to"]),
            })
    return result


def inside(stamp, intervals):
    return any(start <= stamp < end for start, end in intervals)


def outcome_counts(items):
    counts = Counter(item["outcome"] for item in items)
    return {name: counts[name] for name in ("seen", "not_seen", "pending")}


def event_entry(item, zone):
    return {
        "event_id": item["event"]["id"], "event_name": item["event"]["name"],
        "stamp_utc": item["event"]["stamp_utc"],
        "time_local": item["stamp"].astimezone(zone).isoformat(timespec="seconds"),
        "outcome": item["outcome"], "frame_count": item["frames"], "reviewed": item["reviewed"],
    }


def outage_spans(db, station):
    spans = []
    for camera in db.matching("deployments", where=(("station_id", "=", station["id"]),)):
        for row in db.matching("outages", where=(("deployment_id", "=", camera["id"]),)):
            spans.append((row["start_utc"], row["end_utc"], row["name"]))
    return spans


def unpaired_blocks(intervals, items, zone, reasons):
    blocks = []
    for start, end in intervals:
        observed = [item for item in items if start <= item["stamp"] < end]
        left, right = utc_text(start), utc_text(end)
        blocks.append({
            "start_utc": left, "end_utc": right,
            "start_local": start.astimezone(zone).isoformat(timespec="minutes"),
            "end_local": end.astimezone(zone).isoformat(timespec="minutes"),
            "hours": round(measure_hours([(start, end)]), 4),
            **outcome_counts(observed),
            "partner_unavailable": sorted({name for a, b, name in reasons if a < right and b > left}),
            "events": [event_entry(item, zone) for item in observed[:MAX_BLOCK_EVENTS]],
            "events_truncated": len(observed) > MAX_BLOCK_EVENTS,
        })
    return blocks


def block_hours(block):
    return measure_hours([(datetime.fromisoformat(block["start_utc"]),
                           datetime.fromisoformat(block["end_utc"]))])

def class_window_label(key, selected):
    if key == "day":
        return f"{selected['day_from']:02d}:00–{selected['day_to']:02d}:00 station local time"
    return f"{selected['day_to']:02d}:00–{selected['day_from']:02d}:00 station local time, crossing midnight"


def class_section(key, label, selected, stations, activity, events, reasons):
    left, right = stations
    sides = {left["id"]: activity[left["id"]][key], right["id"]: activity[right["id"]][key]}
    paired = intersect_intervals(sides[left["id"]], sides[right["id"]])
    hours = measure_hours(paired)
    entries = []
    for station, other in ((left, right), (right, left)):
        zone = ZoneInfo(station["timezone"])
        alone = difference_intervals(sides[station["id"]], sides[other["id"]])
        own = [item for item in events[station["id"]] if item["light"] == key]
        observed = [item for item in own if inside(item["stamp"], paired)]
        apart = [item for item in own if inside(item["stamp"], alone)]
        counts = outcome_counts(observed)
        apart_counts = outcome_counts(apart)
        blocks = unpaired_blocks(alone, apart, zone, reasons[other["id"]])
        listed = [block for block in blocks if block_hours(block) * 60 >= MIN_BLOCK_MINUTES]
        entries.append({
            "station_id": station["id"], "name": station["name"], "timezone": station["timezone"],
            "paired_hours": round(hours, 4), **counts,
            "rate_per_24h": round(counts["seen"] * 24 / hours, 4) if hours else None,
            "unpaired_hours": round(measure_hours(alone), 4),
            "unpaired_block_count": len(listed),
            "unpaired_short_gap_hours": round(measure_hours(alone) - sum(block_hours(b) for b in listed), 4),
            **{"unpaired_" + name: value for name, value in apart_counts.items()},
            "unpaired_blocks": listed[:MAX_UNPAIRED_BLOCKS],
            "unpaired_blocks_truncated": len(listed) > MAX_UNPAIRED_BLOCKS,
        })
    return {
        "key": key, "label": label, "window": class_window_label(key, selected),
        "paired_hours": round(hours, 4), "stations": entries,
    }


def paired_effort(store, form, *, save=False):
    selected = pairing_inputs(form)
    with store.use() as db:
        stations = db.documents("stations", selected["stations"])
        activity = {station["id"]: station_classes(db, station, selected) for station in stations}
        events = {station["id"]: station_events(db, station, selected) for station in stations}
        reasons = {station["id"]: outage_spans(db, station) for station in stations}
    results = {
        "settings": selected,
        "stations": [{"station_id": station["id"], "name": station["name"], "timezone": station["timezone"]}
                     for station in stations],
        "classes": [class_section(key, label, selected, stations, activity, events, reasons)
                    for key, label, _ in LIGHT_CLASSES],
        "method": "Reviewed confirmed events divided by hours recorded at both stations in the same light window",
        "note": ("Only simultaneous active hours enter a paired count or rate. Hours recorded at one station "
                 "are listed beside the comparison and are not treated as absences at the other."),
    }
    if not save:
        return results
    with store.use(write=True) as db:
        return db.append("reports", {
            "kind": "pairing", "name": word(form.get("name"), "comparison name",
                                            default="Paired survey effort"),
            "report": results,
        }, action="paired survey comparison saved")
