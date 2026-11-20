"""Time-of-day views of reviewed camera events with labelled observation support."""

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from math import cos, pi, sin
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw

from .contracts import Limit, SurveyError, integer, word
from .effort import (
    effort_inputs, event_status, local_day_range, measure_hours, merged_intervals, overlap, station_intervals,
)
from .images import image_bytes, label

STATUS_ORDER = ("seen", "not_seen", "pending")
SERIES_COLORS = [(97, 218, 160), (155, 184, 224), (231, 177, 111)]


def hour_bounds(day, timezone_name, step):
    zone = ZoneInfo(timezone_name)
    cursor = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
    finish = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    intervals = []
    while cursor < finish:
        local = cursor.astimezone(zone)
        skip = timedelta(minutes=step - (local.hour * 60 + local.minute) % step)
        end = min(finish, cursor + skip)
        bucket = (local.hour * 60 + local.minute) // step
        intervals.append((bucket, (cursor, end)))
        cursor = end
    return intervals


def time_buckets(first, days, tz, step):
    labels = []
    for k in range(days):
        current = first + timedelta(days=k)
        labels.extend(hour_bounds(current, tz, step))
    return labels


def interval_hours(known, buckets):
    index = 0
    totals = defaultdict(float)
    for label_index, block in buckets:
        while index < len(known) and known[index][1] <= block[0]:
            index += 1
        pos = index
        while pos < len(known) and known[pos][0] < block[1]:
            joint = overlap(known[pos], block)
            if joint:
                totals[label_index] += measure_hours([joint])
            pos += 1
    return totals


def activity_settings(raw):
    info = effort_inputs(raw)
    minutes = integer(raw.get("bucket_minutes"), "bucket_minutes", 15, 240, default=60)
    if 1440 % minutes:
        raise SurveyError("Use a bucket size that divides one full day.", field="bucket_minutes")
    if info["days"] * (1440 // minutes) > 35_100:
        raise Limit("Shorten the period or use wider activity buckets.")
    return info | {"bucket_minutes": minutes}


def event_times(db, deployment, window, available, taxon):
    candidates = db.matching("events", where=(
        ("deployment_id", "=", deployment), ("stamp_utc", ">=", window[0].isoformat(timespec="seconds")),
        ("stamp_utc", "<", window[1].isoformat(timespec="seconds")),
    ), order=("stamp_utc", "ASC"))
    result = []
    for event in candidates:
        stamp = datetime.fromisoformat(event["stamp_utc"])
        if event["state"] != "confirmed" or not any(left <= stamp < right for left, right in available):
            continue
        state, frames, reviewed = event_status(db, event, taxon)
        result.append((stamp, event["id"], state, frames, reviewed))
    return result


def clock_label(index, step):
    hour, minute = divmod(index * step, 60)
    return f"{hour:02d}:{minute:02d}"


def series_for_station(db, inputs, station):
    timezone_name = station["timezone"]
    zone = ZoneInfo(timezone_name)
    first = datetime.fromisoformat(inputs["first_day"]).date()
    span = local_day_range(inputs["first_day"], inputs["last_day"], timezone_name)
    deployments = db.matching("deployments", where=(("station_id", "=", station["id"]),))
    intervals, events = [], []
    for deployment in deployments:
        times = station_intervals(db, station, deployment, inputs, span)
        intervals.extend(times["available"])
        events.extend(event_times(db, deployment["id"], span, times["available"], inputs["taxon"]))
    active = merged_intervals(intervals)
    step = inputs["bucket_minutes"]
    buckets = time_buckets(first, inputs["days"], timezone_name, step)
    hours = interval_hours(active, buckets)
    groups = defaultdict(Counter)
    witnesses = defaultdict(list)
    for stamp, event, status, total, reviewed in events:
        local = stamp.astimezone(zone)
        index = (local.hour * 60 + local.minute) // step
        groups[index][status] += 1
        if len(witnesses[index]) < 15:
            witnesses[index].append({"event_id": event, "time": local.isoformat(timespec="seconds"),
                                     "reviewed": reviewed, "frames": total})
    entries = []
    for i in range(1440 // step):
        values = {name: groups[i][name] for name in STATUS_ORDER}
        value = hours[i]
        values.update({"hours": round(value, 5), "start": clock_label(i, step),
                       "end": clock_label(i + 1, step), "witnesses": witnesses[i],
                       "rate_per_24h": round(values["seen"] * 24 / value, 4) if value else None})
        entries.append(values)
    return {"station_id": station["id"], "name": station["name"], "timezone": timezone_name,
            "hours": measure_hours(active), "entries": entries, "deployments": len(deployments)}


def picture_board(title, stations, step):
    width = 1100
    each = 132
    image = Image.new("RGB", (width, 70 + each * len(stations)), (23, 32, 34))
    painter = ImageDraw.Draw(image)
    label(painter, (28, 17), title, size=25)
    label(painter, (28, 51), "Local wall clock · confirmed events · gray gaps have no logged camera effort", size=13)
    x0, x1 = 40, width - 45
    for i, section in enumerate(stations):
        y = 86 + i * each
        label(painter, (x0, y), f"{section['name']} · {section['timezone']}", size=16)
        buckets = section["entries"]
        bar_width = (x1 - x0) / len(buckets)
        max_events = max(1, max(sum(value[k] for k in STATUS_ORDER) for value in buckets))
        for j, bucket in enumerate(buckets):
            x = x0 + bar_width * j
            bottom = y + 84
            hue = (66, 75, 73) if bucket["hours"] > 0 else (46, 52, 52)
            painter.rectangle((x + 1, y + 26, x + bar_width - 1, bottom), fill=hue)
            for key, shade in zip(STATUS_ORDER, SERIES_COLORS):
                level = round(bucket[key] / max_events * 54)
                if not level:
                    continue
                painter.rectangle((x + 1, bottom - level, x + bar_width - 1, bottom), fill=shade)
                bottom -= level
        for hour in (0, 6, 12, 18, 24):
            x = x0 + (x1 - x0) * hour / 24
            label(painter, (min(x, x1 - 32), y + 91), f"{hour:02d}:00", size=11)
        label(painter, (x1 - 75, y + 5), f"{step} min", size=12)
    return image


def circular_summary(entries, minutes):
    scale = 2 * pi / 1440
    angles = [(i * minutes + minutes / 2) * scale for i in range(len(entries))]
    weights = [bucket["seen"] for bucket in entries]
    count = sum(weights)
    if not count:
        return {"positive_events": 0, "centre_minutes": None, "concentration": None}
    row = sum(w * cos(angle) for angle, w in zip(angles, weights)) / count
    col = sum(w * sin(angle) for angle, w in zip(angles, weights)) / count
    return {"positive_events": count, "centre_minutes": round(float(np.arctan2(col, row) % (2 * pi)) / scale, 2),
            "concentration": round(float(np.hypot(row, col)), 4)}


def activity_chart(store, raw, *, save=False):
    settings = activity_settings(raw)
    with store.use() as db:
        stations = db.documents("stations", settings["stations"]) if settings["stations"] else db.matching("stations", limit=12)
        if not stations:
            raise SurveyError("Add at least one survey station.")
        if len(stations) > 12:
            raise Limit("Select up to twelve stations per activity chart.")
        readings = [series_for_station(db, settings, station) for station in stations]
    name = word(raw.get("name"), "activity chart name", default="Camera activity by local clock")
    records = [station | {"activity": circular_summary(station["entries"], settings["bucket_minutes"])}
               for station in readings]
    report = {"settings": settings, "name": name, "stations": records,
              "note": "Timing is a description of confirmed sightings in the selected windows."}
    if not save:
        return report
    image = picture_board(name, records, settings["bucket_minutes"])
    with store.use(write=True) as db:
        record = db.append("reports", {"kind": "activity", "name": name, "report": report},
                           action="clock activity chart saved")
        db.add_asset(record["id"], "guide", image_bytes(image), "image/png")
    return record
