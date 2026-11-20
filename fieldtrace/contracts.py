"""Input validation for stations, evidence, images and survey filters."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import MAX_POLYGON_POINTS

_KEY = re.compile(r"[a-f0-9]{24}\Z")
_SYSTEM_TZ_NAMES = {"UTC", "Etc/UTC", "GMT"}


class SurveyError(ValueError):
    def __init__(self, message, *, field=None, status=400, code="invalid", facts=None):
        super().__init__(message)
        self.field = field
        self.status = status
        self.code = code
        self.facts = facts

    def packet(self):
        result = {"message": str(self), "code": self.code}
        if self.field:
            result["field"] = self.field
        if self.facts is not None:
            result["facts"] = self.facts
        return {"error": result}


class Missing(SurveyError):
    def __init__(self, noun="record"):
        super().__init__(f"The {noun} is unavailable in this survey.", status=404, code="not_found")


class Conflict(SurveyError):
    def __init__(self, what, *, facts=None):
        super().__init__(what, status=409, code="conflict", facts=facts)


class Limit(SurveyError):
    def __init__(self, message, *, facts=None):
        super().__init__(message, status=413, code="limit", facts=facts)


def need_object(value, name="input"):
    if not isinstance(value, dict):
        raise SurveyError(f"{name.replace('_', ' ').capitalize()} must be an object.", field=name)
    return value


def list_value(value, name, *, min_items=0, max_items=None):
    if not isinstance(value, list):
        raise SurveyError(f"Enter a list for {name.replace('_', ' ')}.", field=name)
    if len(value) < min_items:
        raise SurveyError(f"At least {min_items} entries are required.", field=name)
    if max_items is not None and len(value) > max_items:
        raise Limit(f"At most {max_items} entries can be processed at once.")
    return value


def word(value, name, *, max_length=85, default=None, multiline=False):
    if value is None or value == "":
        if default is not None:
            return default
        raise SurveyError(f"Enter {name.replace('_', ' ')}.", field=name)
    if not isinstance(value, str):
        raise SurveyError(f"{name.replace('_', ' ').capitalize()} must be text.", field=name)
    value = value.strip()
    if not value and default is None:
        raise SurveyError(f"Enter {name.replace('_', ' ')}.", field=name)
    allowed_layout = "\r\n\t" if multiline else ""
    if any(ord(c) < 32 and c not in allowed_layout for c in value):
        raise SurveyError("Remove control characters from the text.", field=name)
    if len(value) > max_length:
        raise SurveyError(f"Limit this field to {max_length} characters.", field=name)
    return value


def option(value, name, options, *, default=None):
    if value is None or value == "":
        if default is None:
            raise SurveyError(f"Choose {name.replace('_', ' ')}.", field=name)
        value = default
    if not isinstance(value, str) or value not in options:
        raise SurveyError(f"Choose a supported {name.replace('_', ' ')}.", field=name)
    return value


def flag(value, name, *, default=False):
    if value is None:
        return default
    if type(value) is bool:
        return value
    if value in ("true", "1", "on"):
        return True
    if value in ("false", "0", "off", ""):
        return False
    raise SurveyError(f"Invalid switch value for {name.replace('_', ' ')}.", field=name)


def number(value, name, minimum, maximum, *, default=None):
    if value is None or value == "":
        if default is not None:
            return default
        raise SurveyError(f"Enter {name.replace('_', ' ')}.", field=name)
    if isinstance(value, bool):
        raise SurveyError("Enter a number.", field=name)
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise SurveyError("Enter a number.", field=name) from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise SurveyError(f"Use a value from {minimum:g} to {maximum:g}.", field=name)
    return result


def integer(value, name, minimum, maximum, *, default=None):
    result = number(value, name, minimum, maximum, default=default)
    if int(result) != result:
        raise SurveyError("Use a whole number.", field=name)
    return int(result)


def identifier(value, name="id", *, optional=False):
    if optional and not value:
        return None
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise SurveyError(f"Choose a saved {name.replace('_', ' ')}.", field=name)
    return value


def identity_list(value, name, *, limit, required=True):
    seq = list_value(value, name, min_items=int(required), max_items=limit)
    ids = [identifier(entry, name) for entry in seq]
    if len(ids) != len(set(ids)):
        raise SurveyError("A record can be selected only once.", field=name)
    return ids


def parse_float_pair(value, name, low, high):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SurveyError("Enter coordinates as a pair.", field=name)
    return tuple(number(c, name, low[i], high[i]) for i, c in enumerate(value))


def unit_point(value, name="point"):
    return parse_float_pair(value, name, (0, 0), (1, 1))


def rectangle(value, name="box"):
    item = list_value(value, name, min_items=4, max_items=4)
    x, y, width, height = [number(i, name, 0, 1) for i in item]
    if min(width, height) < 0.001 or x + width > 1.000001 or y + height > 1.000001:
        raise SurveyError("The rectangle must stay inside the image.", field=name)
    return x, y, width, height


def polygon(value, name="polygon"):
    points = [unit_point(p, name) for p in list_value(value, name, min_items=3, max_items=MAX_POLYGON_POINTS)]
    if points[0] == points[-1]:
        points.pop()
    if len(set(points)) != len(points) or len(points) < 3:
        raise SurveyError("A polygon needs three or more distinct points.", field=name)
    twice_area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))
    if abs(twice_area) < 0.00001:
        raise SurveyError("The outlined area is too small.", field=name)
    return points


def day(value, name="date"):
    if not isinstance(value, str):
        raise SurveyError("Enter a date as YYYY-MM-DD.", field=name)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SurveyError("Enter a date as YYYY-MM-DD.", field=name) from exc


def date_time(value, name="timestamp", *, needs_offset=False):
    if not isinstance(value, str):
        raise SurveyError("Enter a camera date and time.", field=name)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SurveyError("Enter a camera date and time.", field=name) from exc
    if result.year < 1970 or result.year > 2090:
        raise SurveyError("Use a timestamp from 1970 to 2090.", field=name)
    if needs_offset and result.utcoffset() is None:
        raise SurveyError("Include an explicit UTC offset.", field=name)
    return result


def timezone_id(value, name="timezone"):
    if not isinstance(value, str) or ("/" not in value and value not in _SYSTEM_TZ_NAMES):
        raise SurveyError("Use an IANA time zone, for example Europe/London.", field=name)
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise SurveyError("This IANA time zone could not be opened.", field=name) from exc
    return value


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_text():
    return utc_text(datetime.now(timezone.utc))


def json_argument(raw, name):
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SurveyError("Enter valid JSON for this field.", field=name) from exc


@dataclass(frozen=True)
class QueryPage:
    limit: int
    offset: int
    sort: str

    @classmethod
    def parse(cls, raw, options):
        return cls(
            integer(raw.get("limit"), "limit", 1, 250, default=48),
            integer(raw.get("offset"), "offset", 0, 10000, default=0),
            option(raw.get("sort"), "sort", options, default="stamp"),
        )
