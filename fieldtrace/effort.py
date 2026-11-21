"""Survey available hours, negative evidence and reviewed encounter rates."""

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .catalog import revision
from .clock import interval, local_day_range
from .config import EFFORT_MODES, MAX_SPAN_DAYS, OUTAGES, TAXA
from .contracts import Limit, SurveyError, day, identifier, identity_list, integer, option, utc_text, word

ZERO = timedelta(0)


def outage_data(form, deployment):
    left, right = interval(form)
    if utc_text(left) < deployment["started_at"] or utc_text(right) > deployment["ended_at"]:
        raise SurveyError("Record an outage within the camera deployment.", field="start")
    cause = option(form.get("cause"), "cause", OUTAGES, default="unknown")
    return {"deployment_id": deployment["id"], "name": OUTAGES[cause], "cause": cause,
            "start_utc": utc_text(left), "end_utc": utc_text(right),
            "note": word(form.get("note"), "outage note", max_length=1400, default="", multiline=True)}


def save_outage(store, form, key=None):
    with store.use(write=True) as db:
        previous = db.one("outages", key) if key else None
        deployment_id = previous["deployment_id"] if previous else identifier(form.get("deployment_id"), "deployment")
        deployment = db.one("deployments", deployment_id)
        fields = outage_data(form, deployment)
        if key is None:
            return db.append("outages", fields, action="camera outage recorded")
        return db.replace("outages", key, revision(form), lambda old: old | fields, action="camera outage reviewed")


def outages_for(store, key):
    with store.use() as db:
        db.one("deployments", key)
        return {"outages": db.matching("outages", where=(("deployment_id", "=", key),), order=("start_utc", "ASC"))}


def overlap(first, second):
    a, b = max(first[0], second[0]), min(first[1], second[1])
    return (a, b) if b > a else None


def keep_inside(intervals, bound):
    return [v for i in intervals if (v := overlap(bound, i)) is not None]


def as_timespan(begin, end):
    return datetime.fromisoformat(begin), datetime.fromisoformat(end)


def merged_intervals(intervals):
    if not intervals:
        return []
    ordered = sorted(intervals)
    groups = [ordered[0]]
    for left, right in ordered[1:]:
        start, end = groups[-1]
        if left <= end:
            groups[-1] = (start, max(end, right))
        else:
            groups.append((left, right))
    return groups


def subtract_intervals(span, holes):
    intervals = []
    last = span[0]
    for left, right in merged_intervals(keep_inside(holes, span)):
        if last < left:
            intervals.append((last, left))
        last = max(last, right)
    if last < span[1]:
        intervals.append((last, span[1]))
    return intervals


def intersect_intervals(first, second):
    left = merged_intervals(first)
    right = merged_intervals(second)
    answer = []
    i = j = 0
    while i < len(left) and j < len(right):
        start, end = max(left[i][0], right[j][0]), min(left[i][1], right[j][1])
        if start < end:
            answer.append((start, end))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return answer


def difference_intervals(base, holes):
    return [part for span in merged_intervals(base) for part in subtract_intervals(span, holes)]


def measure_hours(intervals):
    return sum((b - a for a, b in intervals), ZERO).total_seconds() / 3600


def split_by_day(parts, tz):
    bins = defaultdict(list)
    zone = ZoneInfo(tz)
    for start, end in parts:
        cursor = start
        while cursor < end:
            local_day = cursor.astimezone(zone).date()
            next_day = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=zone)
            upto = min(end, next_day.astimezone(timezone.utc))
            if upto <= cursor:
                upto = min(end, cursor + timedelta(hours=1))
            bins[local_day.isoformat()].append((cursor, upto))
            cursor = upto
    return dict(bins)


def valid_local_interval(start_day, start_hour, stop_hour, zone):
    end_day = start_day if stop_hour > start_hour else start_day + timedelta(days=1)
    left = datetime.combine(start_day, time(start_hour), tzinfo=zone).astimezone(timezone.utc)
    right = datetime.combine(end_day, time(stop_hour), tzinfo=zone).astimezone(timezone.utc)
    return left, right


def light_windows(range_utc, timezone_name, mode, day_from, day_to):
    if mode == "active":
        return [range_utc]
    zone = ZoneInfo(timezone_name)
    start = range_utc[0].astimezone(zone).date() - timedelta(days=1)
    end = range_utc[1].astimezone(zone).date()
    count = (end - start).days + 1
    first, last = (day_from, day_to) if mode == "day-window" else (day_to, day_from)
    windows = [valid_local_interval(start + timedelta(days=i), first, last, zone) for i in range(count)]
    return keep_inside(windows, range_utc)


def this_light_window(parts, bounds):
    answers = []
    cursor = 0
    for a, b in parts:
        while cursor < len(bounds) and bounds[cursor][1] <= a:
            cursor += 1
        i = cursor
        while i < len(bounds) and bounds[i][0] < b:
            if hit := overlap((a, b), bounds[i]):
                answers.append(hit)
            i += 1
    return answers


def effort_inputs(form):
    start_day = day(form.get("first_day"), "first_day")
    end_day = day(form.get("last_day"), "last_day")
    days = (end_day - start_day).days + 1
    if days < 1:
        raise SurveyError("The last survey day must follow the first.", field="last_day")
    if days > MAX_SPAN_DAYS:
        raise Limit("Survey summaries can span at most 365 calendar days.")
    mode = option(form.get("mode"), "mode", EFFORT_MODES, default="active")
    day_from = integer(form.get("day_from"), "day_from", 0, 22, default=6)
    day_to = integer(form.get("day_to"), "day_to", 1, 23, default=18)
    if day_from >= day_to:
        raise SurveyError("The day window must end after it starts.", field="day_to")
    raw = form.get("taxon")
    taxon = option(raw, "taxon", TAXA) if raw else None
    ids = identity_list(form.get("stations"), "stations", limit=24, required=False) if "stations" in form else []
    return {
        "first_day": start_day.isoformat(), "last_day": end_day.isoformat(), "mode": mode,
        "day_from": day_from, "day_to": day_to, "days": days, "taxon": taxon, "stations": ids,
    }


def station_intervals(db, station, deployment, selected, local_span):
    bounds = as_timespan(deployment["started_at"], deployment["ended_at"])
    intersection = overlap(bounds, local_span)
    if intersection is None or deployment["state"] == "service":
        return {"available": [], "recorded_outage": [], "calendar": []}
    departures = db.matching("outages", where=(("deployment_id", "=", deployment["id"]),))
    excluded = [as_timespan(row["start_utc"], row["end_utc"]) for row in departures]
    windows = light_windows(intersection, station["timezone"], selected["mode"],
                            selected["day_from"], selected["day_to"])
    active = subtract_intervals(intersection, excluded)
    return {
        "available": this_light_window(active, windows),
        "recorded_outage": this_light_window(merged_intervals(keep_inside(excluded, intersection)), windows),
        "calendar": windows,
    }


def event_status(db, event, taxon):
    frames = db.event_members(event["id"])
    reviewed = [frame for frame in frames if frame["review"] in ("seen", "empty")]
    seen = [frame for frame in reviewed if frame["review"] == "seen"
            and (taxon is None or taxon in frame["taxa"])]
    if len(seen):
        outcome = "seen"
    elif len(reviewed) == len(frames) and reviewed:
        outcome = "not_seen"
    else:
        outcome = "pending"
    return outcome, len(frames), len(reviewed)


def inside_intervals(utc_time, intervals):
    return any(start <= utc_time < end for start, end in intervals)


def summarize_events(db, deployment, station, selected, local_span, active):
    rows = db.matching("events", where=(
        ("deployment_id", "=", deployment["id"]), ("stamp_utc", ">=", utc_text(local_span[0])),
        ("stamp_utc", "<", utc_text(local_span[1])),
    ), order=("stamp_utc", "ASC"))
    observed = []
    local_zone = ZoneInfo(station["timezone"])
    statuses = Counter()
    for event in rows:
        key = event["id"]
        stamp = datetime.fromisoformat(event["stamp_utc"])
        status, total, reviewed = event_status(db, event, selected["taxon"])
        if not inside_intervals(stamp, active):
            statuses["outside active window"] += 1
            continue
        if event["state"] != "confirmed":
            statuses["not confirmed"] += 1
            continue
        statuses[status] += 1
        observed.append({
            "id": key, "event_name": event["name"], "deployment_id": deployment["id"],
            "stamp_utc": event["stamp_utc"], "date_local": stamp.astimezone(local_zone).date().isoformat(),
            "outcome": status, "frame_count": total, "reviewed": reviewed,
        })
    return observed, statuses


def daily_rows(active_by_day, calendar_by_day, observed, range_first, count):
    detections = defaultdict(Counter)
    for row in observed:
        detections[row["date_local"]][row["outcome"]] += 1
    start = day(range_first)
    rows = []
    for offset in range(count):
        label = (start + timedelta(days=offset)).isoformat()
        active = measure_hours(active_by_day.get(label, []))
        planned = measure_hours(calendar_by_day.get(label, []))
        counts = detections[label]
        rows.append({
            "date": label, "active_hours": round(active, 4), "calendar_hours": round(planned, 4),
            "seen": counts["seen"], "pending": counts["pending"], "not_seen": counts["not_seen"],
            "rate_per_24h": round(counts["seen"] * 24 / active, 4) if active else None,
        })
    return rows


def station_report(db, station, selected):
    span = local_day_range(selected["first_day"], selected["last_day"], station["timezone"])
    deployments = db.matching("deployments", where=(("station_id", "=", station["id"]),))
    active_intervals, planned_intervals, observed, details = [], [], [], []
    statuses = Counter()
    for camera in deployments:
        windows = station_intervals(db, station, camera, selected, span)
        active_intervals.extend(windows["available"])
        planned_intervals.extend(windows["calendar"])
        items, info = summarize_events(db, camera, station, selected, span, windows["available"])
        observed.extend(items)
        statuses.update(info)
        details.append({
            "id": camera["id"], "name": camera["name"], "state": camera["state"],
            "hours_active": round(measure_hours(windows["available"]), 4),
            "hours_outage": round(measure_hours(windows["recorded_outage"]), 4),
            "hours_expected": round(measure_hours(windows["calendar"]), 4),
            "event_outcomes": dict(info),
        })
    active = merged_intervals(active_intervals)
    expected = merged_intervals(planned_intervals)
    active_hours = measure_hours(active)
    separate_hours = measure_hours(active_intervals)
    return {
        "station_id": station["id"], "station_name": station["name"], "timezone": station["timezone"],
        "deployment_count": len(deployments), "hours_active": round(active_hours, 4),
        "hours_expected": round(measure_hours(expected), 4),
        "overlapping_camera_hours": round(separate_hours - active_hours, 4),
        "outcomes": dict(statuses), "events": observed, "deployments": details,
        "rate_per_24h": round(statuses["seen"] * 24 / active_hours, 4) if active_hours else None,
        "days": daily_rows(split_by_day(active, station["timezone"]),
                           split_by_day(expected, station["timezone"]), observed,
                           selected["first_day"], selected["days"]),
    }


def survey_effort(store, form, *, save=False):
    inputs = effort_inputs(form)
    with store.use() as db:
        stations = db.documents("stations", inputs["stations"]) if inputs["stations"] else db.matching("stations", limit=24)
        if not stations:
            raise SurveyError("Add a station before summarizing survey effort.")
        reports = [station_report(db, station, inputs) for station in stations]
    results = {
        "settings": inputs,
        "stations": reports,
        "method": "Reviewed confirmed events divided by the union of active station hours",
        "note": "This is an observed encounter rate. Blank effort and unresolved events provide no negative evidence.",
    }
    if not save:
        return results
    with store.use(write=True) as db:
        return db.append("reports", {
            "kind": "effort", "name": word(form.get("name"), "summary name", default="Survey effort"),
            "report": results,
        }, action="survey rate saved")
