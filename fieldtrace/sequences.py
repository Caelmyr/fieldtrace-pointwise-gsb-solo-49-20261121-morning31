"""Time-ordered browsing and inspection strips for camera trigger bursts."""

from bisect import bisect_left
from datetime import datetime
from math import ceil

from PIL import Image, ImageDraw

from .clock import time_pair
from .config import MAX_EVENT_FRAMES
from .contracts import Conflict, Limit, SurveyError, identifier, integer, option, rectangle, word
from .events import aggregate_reviews, frame_sort
from .images import fetch_work, image_bytes, label, marked_preview, normalized_crop, show_support

SEQUENCE_STATES = {"all", "remaining", "visible-only"}
DISPLAY_STATES = {"new", "held", "seen", "empty", "unusable"}
STEP_PALETTE = {
    "new": (115, 151, 165),
    "held": (214, 171, 94),
    "seen": (113, 219, 158),
    "empty": (136, 172, 211),
    "unusable": (171, 116, 124),
}


def sequence_filter(frame, mode):
    if mode == "all":
        return True
    if mode == "remaining":
        return frame["review"] in {"held", "new"}
    return frame["review"] != "unusable" and frame["supported_fraction"] >= 0.25


def visible_frames(db, focus, mode, event=None):
    if event:
        rows = db.event_members(event)
        if not any(frame["id"] == focus["id"] for frame in rows):
            raise Conflict("The active frame is outside this event.")
    else:
        rows = db.matching("frames", where=(("deployment_id", "=", focus["deployment_id"]),),
                           order=("stamp_utc", "ASC"), limit=2500)
    selected = [frame for frame in rows if sequence_filter(frame, mode)]
    return sorted(selected, key=frame_sort)


def frame_summary(frame):
    return {
        "id": frame["id"], "name": frame["name"], "review": frame["review"],
        "extent": frame["extent"], "time": frame["time"], "illumination": frame["illumination"],
        "origin": frame["origin"], "version": frame["version"], "local_stamp": frame["local_stamp"],
    }


def relative_rows(db, form):
    frame_id = identifier(form.get("frame"), "frame")
    event = identifier(form.get("event"), "event", optional=True)
    mode = option(form.get("filter"), "filter", SEQUENCE_STATES, default="all")
    count = integer(form.get("neighbours"), "neighbours", 1, 20, default=4)
    with db.use() as q:
        focus = q.one("frames", frame_id)
        rows = visible_frames(q, focus, mode, event)
    keys = [frame_sort(frame) for frame in rows]
    anchor = bisect_left(keys, frame_sort(focus))
    current_in_list = anchor < len(rows) and rows[anchor]["id"] == frame_id
    end = anchor + int(current_in_list)
    return {
        "before": [frame_summary(row) for row in rows[max(0, anchor - count):anchor]],
        "focus": frame_summary(focus),
        "after": [frame_summary(row) for row in rows[end:end + count]],
        "index": anchor + 1 if current_in_list else None,
        "eligible": len(rows),
        "event": event,
        "filter": mode,
    }


def cursor_info(frames, index):
    current = frames[index]
    first, last = frames[0], frames[-1]
    active = time_pair(current)
    start = time_pair(first)
    finish = time_pair(last)
    lower = max(0.0, (active[0] - start[1]).total_seconds()) if active and start else None
    upper = max(0.0, (active[1] - start[0]).total_seconds()) if active and start else None
    length = max(0.0, (finish[1] - start[0]).total_seconds()) if finish and start else None
    return {"fraction": 0 if len(frames) <= 1 else index / (len(frames) - 1),
            "elapsed_from_first_s": [lower, upper] if lower is not None else None,
            "maximum_burst_s": length}


def capture_strip(db, frame, box, mark):
    image = fetch_work(db, frame["id"])
    marks = db.matching("marks", where=(("frame_id", "=", frame["id"]),))
    if mark:
        image = marked_preview(image, marks)
    else:
        image = show_support(image)
    bounds = normalized_crop(box, image.size) if box else (0, 0, *image.size)
    focused = image.crop(bounds)
    return focused, {
        "frame_id": frame["id"], "source_digest": frame["digest"], "frame_version": frame["version"],
        "name": frame["name"], "origin": frame["origin"], "time": frame["time"],
        "review": frame["review"], "frozen_extent": frame["extent"], "bounds": list(bounds),
        "marks": {item["id"]: item["version"] for item in marks} if mark else {},
    }


def strip_caption(draw, origin, item, ordinal):
    x, y = origin
    state = item["review"]
    time_info = item["time"]
    stamp = time_info.get("utc")
    suffix = "time unresolved" if not stamp else datetime.fromisoformat(stamp).strftime("%H:%M:%S UTC")
    label(draw, (x, y), f"{ordinal:02d}  {item['name'][:29]}", size=12)
    label(draw, (x, y + 18), f"{suffix} · {state}", size=11, fill=STEP_PALETTE[state])


def strip_image(title, parts, columns):
    width = 1060
    inset = 24
    gap = 13
    rows = ceil(len(parts) / columns)
    card_w = (width - inset * 2 - gap * (columns - 1)) // columns
    inner_height = int(card_w * 0.66)
    card_h = inner_height + 44
    image = Image.new("RGB", (width, 80 + rows * (card_h + gap)), (26, 38, 39))
    painter = ImageDraw.Draw(image)
    label(painter, (inset, 19), title, size=22)
    label(painter, (inset, 53), "Ordered inspection windows from the same source burst", size=13)
    for i, (picture, meta) in enumerate(parts):
        row, col = divmod(i, columns)
        x = inset + col * (card_w + gap)
        y = 80 + row * (card_h + gap)
        pic = picture.copy()
        pic.thumbnail((card_w, inner_height), Image.Resampling.LANCZOS)
        shade = STEP_PALETTE[meta["review"]]
        painter.rectangle((x, y, x + card_w, y + inner_height), outline=shade, width=1)
        image.paste(pic, (x + (card_w - pic.width) // 2, y + (inner_height - pic.height) // 2))
        strip_caption(painter, (x + 3, y + inner_height + 5), meta, i + 1)
    return image


def spaced_selection(count, width, start):
    if start >= count:
        raise SurveyError("The requested start is outside this event.", field="first")
    if count - start <= width:
        return list(range(start, count))
    if width == 1:
        return [start]
    interval = (count - start - 1) / (width - 1)
    return [start + round(i * interval) for i in range(width)]


def make_strip(store, event_id, form, *, save=True):
    variant = option(form.get("mode"), "mode", {"spaced", "contiguous"}, default="spaced")
    layout = integer(form.get("columns"), "columns", 2, 5, default=4)
    width = integer(form.get("count"), "count", 2, 20, default=8)
    start = integer(form.get("first"), "first", 1, MAX_EVENT_FRAMES, default=1) - 1
    polygon_box = rectangle(form["box"]) if form.get("box") else None
    mark = form.get("show_marks", True) is not False
    with store.use() as db:
        event = db.one("events", event_id)
        frames = db.event_members(event_id)
        if len(frames) < 2 or start >= len(frames):
            raise SurveyError("The selected event has too few frames for this strip.")
        if variant == "spaced":
            indexes = spaced_selection(len(frames), width, start)
        else:
            indexes = list(range(start, min(start + width, len(frames))))
        if len(indexes) < 2:
            raise SurveyError("Choose a start with two remaining camera frames.", field="first")
        if polygon_box and len({tuple(frames[i]["extent"]) for i in indexes}) != 1:
            raise SurveyError("Fixed crop strips require frames with equivalent framing.", field="box")
        plates = [capture_strip(db, frames[i], polygon_box, mark) for i in indexes]
        cursors = [cursor_info(frames, i) for i in indexes]
    title = word(form.get("name"), "strip title", default=f"{event['name'][:54]} · sequence")
    poster = strip_image(title, plates, layout)
    report = {
        "name": title, "event_id": event_id, "event_version": event["version"],
        "metadata": [meta | {"sequence_index": i + 1, "cursor": elapsed}
                     for (_, meta), i, elapsed in zip(plates, indexes, cursors)],
        "mode": variant, "kind": "sequence", "source_review": aggregate_reviews(frames),
        "box": polygon_box, "show_marks": mark,
    }
    if not save:
        return poster
    with store.use(write=True) as db:
        entry = db.append("reports", {"kind": "sequence", "name": title, "report": report},
                          action="inspection sequence frozen")
        db.add_asset(entry["id"], "guide", image_bytes(poster), "image/png")
    return entry


def parse_saved_records(store, table, query):
    group = query.get("kind")
    criterion = (("kind", "=", group),) if group and table == "reports" else ()
    if table not in {"samples", "reports"}:
        raise ValueError("Unsupported stored view")
    limit = integer(query.get("limit"), "limit", 1, 100, default=40)
    with store.use() as db:
        rows = db.matching(table, where=criterion, limit=limit)
        return {table: [
            {key: row[key] for key in ("id", "name", "version", "created_at")}
            | ({"kind": row["kind"]} if table == "reports" else {})
            | {"assets": db.assets(row["id"])} for row in rows
        ]}
