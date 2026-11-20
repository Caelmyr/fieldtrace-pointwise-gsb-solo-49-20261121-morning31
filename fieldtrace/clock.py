"""Keep raw camera timestamps, offset evidence and resolved order distinct."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .contracts import SurveyError, date_time, day, integer, utc_text

EXIF_STAMP = 36867
EXIF_DIGITIZED = 36868
EXIF_DATE = 306
EXIF_OFFSET_ORIGINAL = 36881
EXIF_OFFSET = 36880
EXIF_CAMERA_SERIAL = 42033
EXIF_MAKE = 271
EXIF_MODEL = 272
EXIF_ORIENTATION = 274


def ascii_token(raw, *, length=60):
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="replace")
    if not isinstance(raw, str):
        return None
    return "".join(c for c in raw[:length] if c.isprintable()).strip() or None


def exif_fields(image):
    try:
        meta = image.getexif()
        photo = meta.get_ifd(34665)
    except (ValueError, TypeError, OSError, SyntaxError):
        return {}
    source = dict(meta)
    source.update(photo or {})
    stamp = next((ascii_token(source.get(key)) for key in (EXIF_STAMP, EXIF_DIGITIZED, EXIF_DATE)
                  if ascii_token(source.get(key))), None)
    raw_offset = ascii_token(source.get(EXIF_OFFSET_ORIGINAL) or source.get(EXIF_OFFSET))
    serial = ascii_token(source.get(EXIF_CAMERA_SERIAL))
    return {
        "camera_stamp": stamp,
        "camera_offset": raw_offset,
        "camera_make": ascii_token(source.get(EXIF_MAKE)),
        "camera_model": ascii_token(source.get(EXIF_MODEL)),
        "camera_serial": serial,
        "orientation": int(meta.get(EXIF_ORIENTATION, 1)) if isinstance(meta.get(EXIF_ORIENTATION, 1), int) else 1,
    }


def camera_time(text, offset=None):
    if not text:
        return None
    if len(text) >= 19 and text[4] == ":" and text[7] == ":":
        text = f"{text[:4]}-{text[5:7]}-{text[8:10]}T{text[11:]}"
    if offset and len(text) == 19:
        text += offset
    try:
        raw = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return raw if 1970 <= raw.year <= 2090 else None


def zone_choices(local, zone_name):
    zone = ZoneInfo(zone_name)
    outcomes = {}
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            outcomes[utc] = fold
    return sorted(outcomes)


def resolving_record(raw_value, resolved, zone, basis, uncertainty, camera_delta, candidates=()):
    return {
        "camera_time": raw_value,
        "utc": utc_text(resolved) if resolved else None,
        "time_zone": zone,
        "basis": basis,
        "uncertainty_s": uncertainty,
        "camera_clock_fast_s": camera_delta,
        "utc_candidates": [utc_text(t) for t in candidates],
    }


def apply_clock(raw, *, raw_value, zone, skew, uncertainty, fold):
    if raw is None:
        return resolving_record(raw_value, None, zone, "missing", None, skew)
    if raw.utcoffset() is not None:
        resolved = raw.astimezone(timezone.utc) - timedelta(seconds=skew)
        return resolving_record(raw_value, resolved, zone, "camera offset", uncertainty, skew)
    candidates = [stamp - timedelta(seconds=skew) for stamp in zone_choices(raw, zone)]
    if not candidates:
        return resolving_record(raw_value, None, zone, "nonexistent local time", None, skew)
    if len(candidates) > 1 and fold is None:
        return resolving_record(raw_value, None, zone, "ambiguous local time", None, skew, candidates)
    picked = candidates[fold] if len(candidates) > 1 else candidates[0]
    label = "operator fold" if len(candidates) > 1 else "deployment time zone"
    return resolving_record(raw_value, picked, zone, label, uncertainty, skew, candidates)


def interpret_camera(meta, deployment, override=None, fold=None):
    zone = deployment["timezone"]
    skew = deployment["clock_fast_s"]
    error = deployment["clock_uncertainty_s"]
    if override:
        raw = date_time(override, "camera_time")
        return apply_clock(raw, raw_value=override, zone=zone, skew=skew, uncertainty=error, fold=fold)
    raw_text = meta.get("camera_stamp")
    raw = camera_time(raw_text, meta.get("camera_offset"))
    return apply_clock(raw, raw_value=raw_text, zone=zone, skew=skew, uncertainty=error, fold=fold)


def operator_stamp(raw, deployment, previous):
    mode = raw.get("mode", "clock")
    if mode == "utc":
        new = date_time(raw.get("stamp"), "stamp", needs_offset=True)
        uncertainty = integer(raw.get("uncertainty_s"), "uncertainty_s", 0, 86400, default=0)
        return resolving_record(previous["camera_time"], new, deployment["timezone"],
                                "direct operator time", uncertainty, 0)
    if mode == "unresolved":
        return resolving_record(previous["camera_time"], None, deployment["timezone"], "operator held", None,
                                deployment["clock_fast_s"])
    if mode != "clock":
        raise SurveyError("Choose camera clock, direct time or unresolved.", field="mode")
    fold = raw.get("fold")
    if fold is not None:
        fold = integer(fold, "fold", 0, 1)
    return interpret_camera({}, deployment, override=raw.get("stamp"), fold=fold)


def interval(payload, start="start", end="end"):
    left = date_time(payload.get(start), start, needs_offset=True).astimezone(timezone.utc)
    right = date_time(payload.get(end), end, needs_offset=True).astimezone(timezone.utc)
    if right <= left:
        raise SurveyError("End must be later than start.", field=end)
    return left, right


def local_display(stamp, zone):
    if not stamp:
        return None
    local = datetime.fromisoformat(stamp).astimezone(ZoneInfo(zone))
    return {"date": local.date().isoformat(), "wall_time": local.isoformat(timespec="seconds"),
            "clock": local.strftime("%H:%M:%S"), "zone": local.tzname(), "fold": local.fold}


def local_day_range(first, last, zone_name):
    start = day(first, "first_day")
    finish = day(last, "last_day")
    if finish < start:
        raise SurveyError("Last survey day cannot precede the first.", field="last_day")
    zone = ZoneInfo(zone_name)
    local_left = datetime.combine(start, time.min, tzinfo=zone)
    local_right = datetime.combine(finish + timedelta(days=1), time.min, tzinfo=zone)
    return local_left.astimezone(timezone.utc), local_right.astimezone(timezone.utc)


def time_pair(record):
    base = record["time"]
    if not base.get("utc"):
        return None
    stamp = datetime.fromisoformat(base["utc"])
    error = timedelta(seconds=base["uncertainty_s"] or 0)
    return stamp - error, stamp + error


def time_relation(a, b):
    left = time_pair(a)
    right = time_pair(b)
    if left is None or right is None:
        return "unknown"
    if left[1] < right[0]:
        return "before"
    if left[0] > right[1]:
        return "after"
    return "overlapping"
