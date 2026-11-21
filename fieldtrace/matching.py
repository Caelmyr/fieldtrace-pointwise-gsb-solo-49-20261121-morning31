"""Compare a newly marked area with retained witnesses from the same body side.

The readings here describe photographic resemblance inside the marked areas. A
high value is a prompt to open both crops and look; it is never an
identification and never a decision. An area the new photo does not show, or
that no retained witness covers, stays unresolved: it is not a disagreement.
"""

from dataclasses import dataclass
from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw
from .config import MARKING_REGIONS, MAX_MATCH_REGIONS, MAX_MATCH_WITNESSES, POSES, TAXA
from .contracts import (
    Conflict, SurveyError, identifier, integer, json_argument, list_value, option, rectangle, word,
)
from .images import (
    fetch_work, image_bytes, intensities, label, mark_union, normalized_crop, show_support,
)

GRID = 64
BLOCKS = 6
SIGNATURE_SCALE = 0.6
SPREAD_FLOOR = 0.16
DEFAULT_MINIMUM = 0.30
FAR_DISTANCE = 0.62
WITNESS_USABLE = 0.3
QUERY_USABLE = 0.45
CLEAR_FOR_DIFFERENCE = 0.5

ASSESSED = ("resembles", "differs", "inconclusive")
STANDINGS = ("candidate", "weak", "mixed", "unresolved", "differs")
STANDING_LABELS = {
    "candidate": "Still a candidate",
    "weak": "Weak resemblance, still a candidate",
    "mixed": "Marks disagree between areas",
    "unresolved": "Not enough comparable marks",
    "differs": "Marked area differs in a clear view",
}


@dataclass(frozen=True)
class RegionQuery:
    region: str
    box: tuple

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict):
            raise SurveyError("Each marked area must be an object.", field="regions")
        return cls(option(raw.get("region"), "marking area", MARKING_REGIONS),
                   rectangle(raw.get("box")))

    def json(self):
        return {"region": self.region, "label": MARKING_REGIONS[self.region], "box": self.box}


@dataclass(frozen=True)
class WitnessQuery:
    pose: str
    regions: tuple
    minimum: float
    taxon: str | None

    @classmethod
    def parse(cls, raw):
        pose = option(raw.get("pose"), "body side", POSES)
        if pose not in ("left", "right"):
            raise SurveyError("Choose the side you can see: left or right.", field="pose")
        items = list_value(raw.get("regions"), "marked areas", min_items=1, max_items=MAX_MATCH_REGIONS)
        regions = tuple(RegionQuery.parse(item) for item in items)
        if len({item.region for item in regions}) != len(regions):
            raise SurveyError("Mark each area only once.", field="regions")
        taxon = raw.get("taxon") or None
        return cls(
            pose, regions,
            integer(raw.get("minimum"), "minimum resemblance", 0, 100,
                    default=round(DEFAULT_MINIMUM * 100)) / 100,
            option(taxon, "taxon", TAXA) if taxon else None,
        )


def crop_bounds(extent, box):
    bounds = normalized_crop(box, extent)
    left, top, right, bottom = bounds
    if min(right - left, bottom - top) < 12:
        raise SurveyError("Mark an area at least twelve pixels wide and high.", field="regions")
    return bounds


def region_values(photo, box, marks):
    """Photo levels and usable pixels for one marked area of a source photo."""
    x0, y0, x1, y1 = crop_bounds(photo.size, box)
    values, visible = intensities(photo.crop((x0, y0, x1, y1)))
    omitted = mark_union(photo.size, marks, {"occlusion", "ignore"})[y0:y1, x0:x1]
    usable = visible & ~omitted & (values > 0.035) & (values < 0.96)
    return values, usable


def area_metrics(values, usable):
    count = int(np.count_nonzero(usable))
    clear = int(np.count_nonzero(usable & (values > 0.08) & (values < 0.92)))
    return {"visible_fraction": count / max(1, usable.size),
            "clear_fraction": clear / max(1, count)}


def standardize(values, usable):
    """Fit a marked area into the square grid without stretching it.

    Returns the normalised shading, a coarse-block layout signature, the
    usable mask, and whether the crop carries enough contrast to read at all.
    """
    height, width = values.shape
    ratio = min(GRID / max(1, width), GRID / max(1, height))
    size = (max(1, int(round(width * ratio))), max(1, int(round(height * ratio))))
    canvas = np.zeros((GRID, GRID), dtype=np.float32)
    mask = np.zeros((GRID, GRID), dtype=bool)
    left, top = (GRID - size[0]) // 2, (GRID - size[1]) // 2
    canvas[top:top + size[1], left:left + size[0]] = np.asarray(
        Image.fromarray(values.astype(np.float32), "F").resize(size, Image.Resampling.BOX),
        dtype=np.float32)
    mask[top:top + size[1], left:left + size[0]] = np.asarray(
        Image.fromarray(usable.astype(np.float32), "F").resize(size, Image.Resampling.BOX),
        dtype=np.float32) > 0.5
    if np.count_nonzero(mask) < 48:
        return None, None, mask, False
    selected = canvas[mask]
    centre = float(np.median(selected))
    # A floor keeps a noisy or low-contrast crop from being blown up into a
    # pattern that is not there.
    spread = max(float(np.quantile(selected, 0.9) - np.quantile(selected, 0.1)), SPREAD_FLOOR)
    contrast = float(np.ptp(selected)) > 0.08
    normalized = (canvas - centre) / spread
    return normalized, block_signature(normalized, mask), mask, contrast


def block_signature(normalized, mask):
    """Local shading of the marked area, summarised over a rough block grid.

    Each block keeps the mean level of its usable pixels, so a small shift, a
    little extra blur or ordinary sensor noise leaves the signature close,
    while a truly different distribution of light and dark leaves it far.
    """
    signature = np.zeros((BLOCKS, BLOCKS), dtype=np.float32)
    usable = np.zeros((BLOCKS, BLOCKS), dtype=bool)
    edges = np.linspace(0, GRID, BLOCKS + 1).astype(int)
    for row in range(BLOCKS):
        for column in range(BLOCKS):
            block = normalized[edges[row]:edges[row + 1], edges[column]:edges[column + 1]]
            inside = mask[edges[row]:edges[row + 1], edges[column]:edges[column + 1]]
            if int(np.count_nonzero(inside)) >= 2:
                signature[row, column] = float(np.mean(block[inside]))
                usable[row, column] = True
    return {"levels": signature, "usable": usable}


def describe(values, usable):
    """A description of one marked area, or None if it cannot be read."""
    normalized, signature, mask, contrast = standardize(values, usable)
    if normalized is None or not contrast:
        return None
    return {"signature": signature, "mask": mask}


def resemblance(first, second):
    """How far apart two marked areas are, from 0 (alike) to 1 (unrelated).

    The smallest difference over a one-block shift is used, so a slightly
    different crop or stance does not dominate the reading.
    """
    if first is None or second is None:
        return None
    one, two = first["signature"], second["signature"]
    required = max(3, int(0.6 * np.count_nonzero(one["usable"])))
    best = None
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            outside = one["usable"] & ~np.roll(np.roll(two["usable"], dy, 0), dx, 1)
            shifted = np.roll(np.roll(two["levels"], dy, 0), dx, 1)
            inside = np.roll(np.roll(two["usable"], dy, 0), dx, 1) & one["usable"]
            if int(np.count_nonzero(inside)) < required:
                continue
            # A block present in one reading but absent from the other is a real
            # difference, so missing coverage is penalised rather than ignored.
            difference = np.abs(one["levels"] - shifted)[inside]
            gap = float(np.mean(difference))
            if np.count_nonzero(outside):
                gap += float(np.sum(np.abs(one["levels"][outside]))) / np.count_nonzero(one["usable"])
            best = gap if best is None else min(best, gap)
    return None if best is None else min(1.0, best / SIGNATURE_SCALE)


def reading(score, minimum, clear):
    """Turn a distance into an operator-facing reading.

    ``minimum`` is how alike the operator asked the marked areas to be. A
    difference is only reported when both views are clear, so a shadowed or
    partly hidden crop never turns into a false mismatch.
    """
    if score is None:
        return "not-visible"
    if score <= minimum:
        return "resembles"
    if score >= FAR_DISTANCE and clear >= CLEAR_FOR_DIFFERENCE:
        return "differs"
    return "inconclusive"


def frame_loader(db):
    """Load each source photo at most once during one search."""
    cache = {}

    def load(frame_id):
        if frame_id not in cache:
            marks = db.matching("marks", where=(("frame_id", "=", frame_id),))
            cache[frame_id] = (fetch_work(db, frame_id), marks)
        return cache[frame_id]
    return load


def witness_entry(db, portrait, frame):
    return {
        "id": portrait["id"], "name": portrait["name"], "pose": portrait["pose"],
        "frame_id": portrait["frame_id"], "frame_name": frame["name"] if frame else None,
        "certainty": portrait.get("certainty"), "region": portrait.get("region", "unspecified"),
        "identity_id": portrait["identity_id"], "witness_time": portrait.get("witness_time"),
        "guide": db.asset(portrait["id"], "guide", optional=True) is not None,
    }


def witness_reading(load, portrait):
    photo, marks = load(portrait["frame_id"])
    values, usable = region_values(photo, portrait["box"], marks)
    metrics = area_metrics(values, usable)
    if metrics["visible_fraction"] < WITNESS_USABLE:
        return None, metrics, portrait
    return describe(values, usable), metrics, portrait


def compare_area(query, query_descriptor, query_visible, portraits, load, minimum):
    """Reading for one marked area against the closest retained witness of that area."""
    entry = {"region": query.region, "label": MARKING_REGIONS[query.region], "box": query.box,
             "status": "no-witness", "score": None, "witness": None, "note": ""}
    if query_visible < QUERY_USABLE:
        entry.update(status="not-visible",
                     note="This photo does not show enough of the marked area to compare it.")
        return entry
    if not portraits:
        entry["note"] = "No retained witness describes this area on this side."
        return entry
    if query_descriptor is None:
        entry.update(status="not-visible",
                     note="This marked area is too uniform or too small to compare.")
        return entry
    measured = [row for row in (witness_reading(load, item) for item in portraits) if row[0] is not None]
    if not measured:
        entry.update(status="not-visible",
                     note="The retained witness views of this area cannot be measured.")
        return entry
    # Lower distance means closer, so the first measurable entry is the nearest witness.
    scored = sorted(((resemblance(query_descriptor, descriptor), metrics, row)
                     for descriptor, metrics, row in measured),
                    key=lambda item: (item[0] is None, item[0] or 0.0))
    score, metrics, portrait = scored[0]
    clear = min(metrics["clear_fraction"], 1.0)
    status = reading(score, minimum, clear)
    entry.update(score=None if score is None else round(score, 3), status=status, witness=portrait,
                 witness_clear_fraction=round(metrics["clear_fraction"], 3))
    if score is None:
        entry["note"] = "The marked areas could not be aligned for a reading."
    elif status == "differs":
        entry["note"] = "Both views are clear, yet the marked area does not line up. Check it yourself."
    return entry


def search(store, frame_key, raw):
    query = WitnessQuery.parse(raw)
    previous = []
    with store.use() as db:
        frame = db.one("frames", frame_key)
        load = frame_loader(db)
        photo, marks = load(frame_key)
        regions, descriptors, visible = [], {}, {}
        for item in query.regions:
            values, usable = region_values(photo, item.box, marks)
            metrics = area_metrics(values, usable)
            descriptor = describe(values, usable) if metrics["visible_fraction"] >= QUERY_USABLE else None
            visible[item.region] = metrics["visible_fraction"]
            descriptors[item.region] = descriptor
            regions.append(item.json() | {
                "status": "assessed" if descriptor is not None else "not-visible",
                "visible_fraction": round(metrics["visible_fraction"], 3),
                "clear_fraction": round(metrics["clear_fraction"], 3),
            })
        subjects = db.matching(
            "identities", where=(("taxon", "=", query.taxon),) if query.taxon else (),
            order=("name", "ASC"))
        portraits = db.matching("portraits", order=("created_at", "ASC"), limit=MAX_MATCH_WITNESSES)
        by_subject = {}
        for row in portraits:
            by_subject.setdefault(row["identity_id"], []).append(row)
        frames = {}
        for row in portraits:
            if row["frame_id"] not in frames:
                frames[row["frame_id"]] = db.one("frames", row["frame_id"], optional=True)
        same_side, other_side, without = [], [], 0
        for identity in subjects:
            mine = by_subject.get(identity["id"], [])
            if not mine:
                without += 1
                continue
            sided = [row for row in mine if row["pose"] == query.pose]
            if not sided:
                other_side.append({
                    "identity": {"id": identity["id"], "name": identity["name"], "taxon": identity["taxon"]},
                    "poses": sorted({row["pose"] for row in mine}),
                    "portraits": [witness_entry(db, row, frames[row["frame_id"]])
                                  for row in mine if frames.get(row["frame_id"])],
                })
                continue
            readings = []
            for item in query.regions:
                entry = compare_area(item, descriptors[item.region], visible[item.region],
                                     [row for row in sided if row.get("region") == item.region],
                                     load, query.minimum)
                if entry["witness"] is not None:
                    found = entry["witness"]
                    entry["witness"] = witness_entry(db, found, frames.get(found["frame_id"]))
                readings.append(entry)
            marked = {item.region for item in query.regions}
            uncovered = [{"region": name, "label": MARKING_REGIONS[name], "status": "not-marked",
                          "score": None, "witness": None, "box": None,
                          "note": "This individual has a retained witness here, but this photo was not marked "
                                  "for it. It stays unresolved."}
                         for name in sorted({row.get("region") for row in sided if row.get("region")
                                             and row["region"] not in marked},
                                            key=lambda name: list(MARKING_REGIONS).index(name))]
            assessed = [row for row in readings if row["status"] in ASSESSED]
            statuses = {row["status"] for row in assessed}
            if "resembles" in statuses:
                standing = "mixed" if "differs" in statuses else "candidate"
            elif assessed and statuses == {"differs"}:
                standing = "differs"
            elif statuses == {"inconclusive"}:
                standing = "weak"
            else:
                standing = "unresolved"
            nearest = min((row["score"] for row in assessed if row["score"] is not None), default=None)
            same_side.append({
                "identity": {"id": identity["id"], "name": identity["name"], "taxon": identity["taxon"]},
                "standing": standing, "standing_label": STANDING_LABELS[standing],
                "closest": nearest, "assessed": len(assessed),
                "unresolved": len(readings) - len(assessed), "regions": readings,
                "unmarked_regions": uncovered,
            })
        order = {name: index for index, name in enumerate(STANDINGS)}
        same_side.sort(key=lambda row: (order[row["standing"]], row["closest"] or 1.0,
                                        row["identity"]["name"]))
        previous = [{"id": row["id"], "name": row["name"], "at": row["created_at"]}
                    for row in db.matching("glances", where=(("frame_id", "=", frame_key),),
                                           order=("created_at", "DESC"), limit=5)
                    if row.get("kind") == "witness-search"]
    return {
        "frame": {"id": frame["id"], "name": frame["name"], "extent": frame["extent"]},
        "pose": query.pose, "pose_label": POSES[query.pose],
        "taxon": query.taxon, "minimum": query.minimum,
        "regions": regions,
        "subjects": same_side,
        "other_side": other_side,
        "without_witness": without,
        "previous_search": previous[0] if previous else None,
        "notes": [
            "Resemblance compares only the marked crops on one body side.",
            "An area this photo does not show, or that has no retained witness, stays unresolved.",
            "Only a clear view that does not line up is marked as differing. Check it yourself.",
            "Every candidate below still needs the operator to compare the source photos.",
        ],
    }


def save_search(store, frame_key, raw):
    """Run a search again and retain it with its annotated frame for reopening."""
    result = search(store, frame_key, raw)
    with store.use(write=True) as db:
        frame = db.one("frames", frame_key)
        record = db.append("glances", {
            "name": word(raw.get("name"), "search title", max_length=85,
                         default=f"Witness search · {POSES[result['pose']]}"),
            "frame_id": frame_key, "kind": "witness-search",
            "source_digest": frame["digest"], "frame_version": frame["version"],
            "query": {"pose": result["pose"], "taxon": result["taxon"],
                      "minimum": result["minimum"],
                      "regions": [{key: row[key] for key in ("region", "label", "box")}
                                  for row in result["regions"]]},
            "report": result,
        }, action="marking witness search saved")
        db.add_asset(record["id"], "guide", sketch(store, frame_key, raw), "image/png")
        return record


def sketch(store, frame_key, raw):
    """Annotated copy of the new photo showing the operator's marked areas."""
    query = WitnessQuery.parse(raw)
    with store.use() as db:
        photo = fetch_work(db, frame_key)
    image = show_support(photo)
    image.thumbnail((950, 700), Image.Resampling.LANCZOS)
    graphic = ImageDraw.Draw(image)
    width, height = image.size
    for item in query.regions:
        x, y, w, h = item.box
        points = [(x * width, y * height), ((x + w) * width, y * height),
                  ((x + w) * width, (y + h) * height), (x * width, (y + h) * height)]
        graphic.line(points + [points[0]], fill=(242, 183, 104), width=3, joint="curve")
        label(graphic, (points[0][0] + 4, max(4, points[0][1] + 4)),
              MARKING_REGIONS[item.region], fill=(242, 183, 104), size=12)
    label(graphic, (12, 12), f"{POSES[query.pose]} · marked areas", size=14)
    return image_bytes(image)


def source_crop(db, frame_id, box):
    photo, marks = frame_loader(db)(frame_id)
    x0, y0, x1, y1 = crop_bounds(photo.size, box)
    omitted = mark_union(photo.size, marks, {"occlusion", "ignore"})[y0:y1, x0:x1]
    return show_support(photo.crop((x0, y0, x1, y1)), ~omitted)


def comparison(store, frame_key, raw):
    """Board: the new marked area beside one retained witness crop."""
    box = rectangle(json_argument(raw.get("box"), "box"))
    witness_key = identifier(raw.get("witness"), "witness")
    with store.use() as db:
        db.one("frames", frame_key)
        portrait = db.one("portraits", witness_key)
        if portrait["frame_id"] == frame_key:
            raise SurveyError("Choose a witness retained from another photo.", field="witness")
        query_crop = source_crop(db, frame_key, box)
        entry = db.asset(portrait["id"], "guide", optional=True)
        if entry is not None:
            with Image.open(BytesIO(db.blobs.read(entry["digest"]))) as stored:
                witness_crop = stored.convert("RGB")
        else:
            witness_crop = source_crop(db, portrait["frame_id"], portrait["box"])
    width, height = 960, 570
    board = Image.new("RGB", (width, height), (22, 32, 33))
    painter = ImageDraw.Draw(board)
    label(painter, (24, 16), "Marking comparison", size=24)
    label(painter, (24, 58), "New photo · marked area", size=14)
    label(painter, (width // 2 + 24, 58), f"Retained witness · {portrait['name'][:42]}", size=14)
    for index, panel in enumerate((query_crop, witness_crop)):
        shown = panel.convert("RGB").copy()
        shown.thumbnail((425, 370), Image.Resampling.LANCZOS)
        x = width * index // 2 + (width // 2 - shown.width) // 2
        y = 90 + (380 - shown.height) // 2
        board.paste(shown, (x, y))
        painter.rectangle((x - 1, y - 1, x + shown.width, y + shown.height), outline=(96, 122, 118))
    label(painter, (24, 500),
          f"Compared by the marked area only · witness certainty {portrait.get('certainty') or 'unrecorded'}",
          size=13, fill=(248, 216, 157))
    label(painter, (24, 526), "A resemblance is a prompt to look; it is not an identification.",
          size=13, fill=(200, 216, 202))
    return board


def relabel(store, portrait_key, raw):
    with store.use(write=True) as db:
        portrait = db.one("portraits", portrait_key)
        expected = integer(raw.get("version"), "version", 1, 2**31 - 1)
        if portrait["version"] != expected:
            raise Conflict("This witness changed while it was open. Reload it first.")
        region = option(raw.get("region"), "marking area", MARKING_REGIONS)
        return db.replace("portraits", portrait_key, expected,
                          lambda old: old | {"region": region}, action="witness area labelled")
