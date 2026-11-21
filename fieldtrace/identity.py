"""Work with photographed markings and retain uncertain identity claims."""

import math

import numpy as np
from PIL import Image, ImageDraw

from .catalog import revision
from .config import CERTAINTIES, MARKING_REGIONS, POSES, TAXA
from .contracts import Limit, SurveyError, identifier, integer, option, rectangle, word
from .images import fetch_work, image_bytes, intensities, label, mark_union, normalized_crop, quality_edges, show_support


def identity_fields(form):
    return {
        "name": word(form.get("name"), "individual label"),
        "taxon": option(form.get("taxon"), "taxon", TAXA, default="unknown"),
        "status": option(form.get("status"), "status", {"provisional", "established"}, default="provisional"),
        "features": word(form.get("features"), "distinguishing markings", max_length=2000, default="", multiline=True),
    }


def save_identity(store, form, key=None):
    fields = identity_fields(form)
    with store.use(write=True) as db:
        if key is None:
            return db.append("identities", fields, action="identity record created")
        return db.replace("identities", key, revision(form), lambda old: old | fields, action="identity notes reviewed")


def find_identities(store, raw):
    taxon = raw.get("taxon")
    limit = integer(raw.get("limit"), "limit", 1, 200, default=60)
    where = (("taxon", "=", option(taxon, "taxon", TAXA)),) if taxon else ()
    with store.use() as db:
        rows = db.matching("identities", where=where, order=("name", "ASC"), limit=limit)
        enriched = [row | {"portrait_count": db.count("portraits", equal={"identity_id": row["id"]})}
                    for row in rows]
    return {"identities": enriched}


def read_identity(store, key):
    with store.use() as db:
        individual = db.one("identities", key)
        portraits = db.matching("portraits", where=(("identity_id", "=", key),))
    return individual | {"portraits": portraits}


def crop_support(photo, box, marks):
    bounds = normalized_crop(box, photo.size)
    x0, y0, x1, y1 = bounds
    if min(x1 - x0, y1 - y0) < 8:
        raise SurveyError("Choose a marking area at least eight pixels wide and high.", field="box")
    candidate = photo.crop(bounds)
    intensity, visible = intensities(candidate)
    exclusions = mark_union(photo.size, marks, {"occlusion", "ignore"})[y0:y1, x0:x1]
    reliable = visible & ~exclusions
    discernable = reliable & (intensity > 0.035) & (intensity < 0.96)
    gradients, core = quality_edges(intensity, discernable)
    definition = float(np.quantile(gradients[core], 0.85)) if np.count_nonzero(core) >= 8 else None
    return candidate, reliable, {
        "bounds_px": list(bounds), "width_px": x1 - x0, "height_px": y1 - y0,
        "supported_fraction": float(reliable.mean()), "clear_fraction": float(discernable.mean()),
        "edge_detail": definition,
    }


def pose_panel(photo, support, *, description=None):
    result = show_support(photo, support)
    result.thumbnail((480, 480), Image.Resampling.LANCZOS)
    if description:
        title = ImageDraw.Draw(result)
        label(title, (9, 9), description[:50], size=11)
    return result


def save_portrait(store, identity_id, form):
    frame_id = identifier(form.get("frame"), "frame")
    selected = rectangle(form.get("box"))
    pose = option(form.get("pose"), "pose", POSES, default="unknown")
    region = option(form.get("region"), "marking region", MARKING_REGIONS, default="unspecified")
    with store.use() as db:
        individual = db.one("identities", identity_id)
        frame = db.one("frames", frame_id)
        marks = db.matching("marks", where=(("frame_id", "=", frame_id),))
        photo = fetch_work(db, frame_id)
    crop, support, quality = crop_support(photo, selected, marks)
    if quality["supported_fraction"] < 0.2:
        raise SurveyError("Too little of this proposed marking is visible.", field="box")
    if quality["clear_fraction"] < 0.35 and form.get("certainty") == "observed":
        raise SurveyError("Keep a poorly lit marking as provisional evidence.", field="certainty")
    locked = {
        "name": word(form.get("name"), "reference title", default=f"{POSES[pose]} · {frame['name'][:40]}"),
        "identity_id": identity_id, "frame_id": frame["id"], "pose": pose, "box": selected,
        "region": region,
        "certainty": option(form.get("certainty"), "certainty", CERTAINTIES, default="probable"),
        "note": word(form.get("note"), "witness note", max_length=1600, default="", multiline=True),
        "source_digest": frame["digest"], "source_extent": frame["extent"],
        "source_version": frame["version"], "identity_version": individual["version"],
        "illumination": frame["illumination"], "witness_time": frame["stamp_utc"],
        "quality": quality, "mask_versions": {item["id"]: item["version"] for item in marks},
    }
    view = pose_panel(crop, support, description=f"{POSES[pose]} · {MARKING_REGIONS[region]}")
    with store.use(write=True) as db:
        if db.count("portraits", equal={"identity_id": identity_id}) >= 36:
            raise Limit("This individual already has the maximum number of reference crops.")
        record = db.append("portraits", locked, action="identity image attested")
        db.add_asset(record["id"], "guide", image_bytes(view), "image/png")
        return record


def compatibility(left, right):
    if left["id"] == right["id"]:
        return ["Same witness"]
    causes = []
    if left["pose"] != right["pose"] or left["pose"] == "unknown":
        causes.append("Flanks are not directly comparable")
    if left["illumination"] != right["illumination"]:
        causes.append("Capture lighting differs")
    if min(left["quality"]["clear_fraction"], right["quality"]["clear_fraction"]) < 0.45:
        causes.append("A crop has limited visible marking")
    if left["source_digest"] == right["source_digest"]:
        causes.append("Both crops are from the same original")
    return causes


def portrait_image(db, portrait):
    entry = db.asset(portrait["id"], "guide")
    from io import BytesIO
    import PIL.Image
    with PIL.Image.open(BytesIO(db.blobs.read(entry["digest"]))) as image:
        return image.convert("RGB")


def comparison_board(left, right, notices, *, right_label):
    width, height = 950, 520
    b = Image.new("RGB", (width, height), (22, 32, 33))
    painter = ImageDraw.Draw(b)
    label(painter, (23, 16), "Marking comparison", size=24)
    label(painter, (23, 63), "Retained witness", size=14)
    label(painter, (width // 2 + 23, 63), right_label, size=14)
    for index, img in enumerate((left, right)):
        panel = img.copy()
        panel.thumbnail((425, 340), Image.Resampling.LANCZOS)
        x = width * index // 2 + (width // 2 - panel.width) // 2
        y = 90 + (345 - panel.height) // 2
        b.paste(panel, (x, y))
        painter.rectangle((x - 1, y - 1, x + panel.width, y + panel.height), outline=(96, 122, 118))
    for index, notice in enumerate(notices[:3]):
        label(painter, (25, 440 + index * 22), notice, size=13, fill=(248, 216, 157))
    if not notices:
        label(painter, (25, 441), "Comparable evidence · assess the markings in both images", size=13)
    return b


def compare_portraits(store, a, b):
    with store.use() as db:
        left, right = db.documents("portraits", [a, b])
        left_img, right_img = portrait_image(db, left), portrait_image(db, right)
    issues = compatibility(left, right)
    return comparison_board(left_img, right_img, issues, right_label="Other witness")


def query_portraits(store, raw):
    pose = raw.get("pose")
    kind = raw.get("taxon")
    by_pose = (("pose", "=", option(pose, "pose", POSES)),) if pose else ()
    with store.use() as db:
        all_subjects = db.matching("identities", where=(("taxon", "=", kind),) if kind else ())
        subject_ids = {p["id"] for p in all_subjects}
        portraits = db.matching("portraits", where=by_pose, limit=250)
    limit = integer(raw.get("limit"), "limit", 1, 150, default=50)
    selected = [p for p in portraits if p["identity_id"] in subject_ids][:limit]
    return {"portraits": selected, "subjects": all_subjects}


def crop_preview(store, key, raw):
    box = rectangle(raw.get("box"))
    with store.use() as db:
        frame = db.one("frames", key)
        photo = fetch_work(db, key)
        marks = db.matching("marks", where=(("frame_id", "=", key),))
    crop, support, metrics = crop_support(photo, box, marks)
    return pose_panel(crop, support, description=f"Source window · {metrics['supported_fraction']:.0%} visible")
