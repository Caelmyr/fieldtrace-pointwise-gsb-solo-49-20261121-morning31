"""Printable camera evidence with pinned source, disclosure masks and ZIP verification."""

from collections import Counter
import csv
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

import numpy as np
from PIL import Image, ImageDraw, UnidentifiedImageError

from .config import MAX_ARCHIVE_BYTES, MAX_CONTACT_PAGES, MAX_PACKET_FRAMES
from .contracts import Conflict, Limit, SurveyError, flag, identity_list, integer, option, word
from .images import (
    fetch_work, font, image_bytes, label, mark_union, marked_preview, readonly_array,
)
from .store import encoded

PAPER = {"a4": (210, 297), "letter": (215.9, 279.4)}
CONTACT_LAYOUTS = {"two": (2, 2), "wide": (1, 3), "grid": (2, 3)}
PACKET_SCHEMA = "fieldtrace_inspection_v1"
MAX_ENTRY_COUNT = MAX_PACKET_FRAMES + 10
MAX_EXTRACT_TOTAL = 82 * 1024 ** 2
CONTACT_BACKGROUND = (240, 241, 231)
INSPECTION_GREEN = (29, 73, 62)


def packet_selection(form):
    key = form.get("name")
    sorting = option(form.get("order"), "order", {"selection", "time"}, default="selection")
    return (
        word(key, "evidence title", max_length=100),
        identity_list(form.get("frames"), "frames", limit=MAX_PACKET_FRAMES),
        sorting,
    )


def public_station(station, share):
    if share:
        return {"site": station["code"] or "Site withheld" if station["private"] else station["code"] or station["name"]}
    return {"site": station["name"], "code": station["code"], "time_zone": station["timezone"]}


def asset_scope(frame):
    return {
        "name": frame["name"], "source_extent": frame["source_extent"],
        "working_extent": frame["extent"], "orientation": frame["camera"].get("orientation", 1),
        "supported_fraction": frame["supported_fraction"],
    }


def pin_frame(db, frame, *, share, marks):
    station = db.one("stations", frame["station_id"])
    common = public_station(station, share)
    values = {
        "frame_ref": frame["id"], "source_digest": frame["digest"],
        "frame_version": frame["version"], "illumination": frame["illumination"],
        "time_utc": frame["stamp_utc"], "origin": frame["origin"],
        "review": frame["review"], "taxa": frame["taxa"],
        "masked": marks,
    }
    if share:
        return values | common | {
            "name": "Shared camera frame",
            "working_extent": frame["extent"],
            "time_basis": frame["time"]["basis"],
        }
    return values | common | asset_scope(frame) | {
        "note": frame["review_note"],
        "time_evidence": frame["time"],
        "metadata": frame["camera"],
    }


def redacted_photo(photo, marks):
    hidden = mark_union(photo.size, marks, {"restricted"})
    values = readonly_array(photo).copy()
    values[hidden] = (31, 42, 41, 255)
    output = Image.fromarray(values)
    visible_marks = [mark for mark in marks if mark["kind"] not in {"restricted", "ignore"}]
    result = marked_preview(output, visible_marks, edge=1300)
    return result, int(hidden.sum())


def collect(db, keys, sorting, share, show_marks):
    frames = db.documents("frames", keys)
    if sorting == "time":
        frames.sort(key=lambda row: (row["stamp_utc"] is None, row["stamp_utc"] or "", row["name"]))
    rows = []
    for frame in frames:
        key = frame["id"]
        marks = db.matching("marks", where=(("frame_id", "=", key),), order=("created_at", "ASC"))
        image = fetch_work(db, key)
        if share:
            image, blocked = redacted_photo(image, marks if show_marks else [
                item for item in marks if item["kind"] == "restricted"
            ])
            summary = {"redacted_pixels": blocked, "annotated": show_marks}
        else:
            image = marked_preview(image, marks if show_marks else [], edge=1350)
            summary = {"redacted_pixels": 0, "annotated": show_marks}
        record = pin_frame(db, frame, share=share, marks=summary)
        rows.append((record, image))
    return rows


def split_pages(rows, layout):
    cols, row_count = CONTACT_LAYOUTS[layout]
    amount = cols * row_count
    return [rows[start:start + amount] for start in range(0, len(rows), amount)]


def physical_pixels(mm, dpi):
    return max(1, round(mm * dpi / 25.4))


def paper_pixels(paper, dpi, landscape):
    mm = PAPER[paper]
    values = mm[::-1] if landscape else mm
    return tuple(physical_pixels(v, dpi) for v in values)


def frame_canvas(image, size):
    result = image.copy()
    result.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, (214, 223, 212))
    pos = ((size[0] - result.width) // 2, (size[1] - result.height) // 2)
    canvas.paste(result, pos)
    return canvas


def short_text(text, font_obj, max_width):
    text = str(text)
    if font_obj.getlength(text) <= max_width:
        return text
    while text and font_obj.getlength(text + "…") > max_width:
        text = text[:-1]
    return text + "…"


def caption_lines(record, number):
    label_time = record.get("time_utc") or "Unresolved time"
    light = record["illumination"]
    status = record["review"]
    tax = ", ".join(record["taxa"]) if record["taxa"] else "No animal label"
    mask = record["masked"]
    badges = []
    if record["origin"] == "synthetic":
        badges.append("synthetic")
    if mask["redacted_pixels"]:
        badges.append("privacy mask")
    parts = [f"{number}. {record['name']}", f"{record['site']} · {light}",
             f"{label_time} · {status}", tax + (f" · {', '.join(badges)}" if badges else "")]
    return parts


def draw_caption(draw, parts, origin, width, dpi):
    x, y = origin
    sizes = [18, 14, 13, 13]
    step = physical_pixels(4, dpi)
    for i, (sample, size) in enumerate(zip(parts, sizes)):
        typeface = font(round(size * dpi / 144))
        line = short_text(sample, typeface, width)
        draw.text((x, y + i * step), line, font=typeface, fill=(36, 42, 39))


def sheet_header(draw, title, dims, dpi, index, total, private):
    px, py = dims
    margin = physical_pixels(11, dpi)
    first = font(round(29 * dpi / 144))
    second = font(round(16 * dpi / 144))
    draw.text((margin, margin), short_text(title, first, px - margin * 2), font=first, fill=INSPECTION_GREEN)
    subtitle = "Camera evidence · " + ("masked sharing copy" if private else "internal review copy")
    draw.text((margin, margin + physical_pixels(8, dpi)), subtitle, font=second, fill=(67, 80, 72))
    draw.line((margin, py - margin, px - margin, py - margin), fill=(120, 138, 116), width=1)
    draw.text((margin, py - margin + physical_pixels(2, dpi)), f"FieldTrace    {index} / {total}",
              font=second, fill=(64, 81, 69))


def contacts(rows, title, *, settings):
    layout = settings["layout"]
    dpi = settings["dpi"]
    dimensions = paper_pixels(settings["paper"], dpi, settings["landscape"])
    margin = physical_pixels(11, dpi)
    header = physical_pixels(34, dpi)
    foot = physical_pixels(16, dpi)
    gap = physical_pixels(7, dpi)
    caption = physical_pixels(18, dpi)
    columns, row_count = CONTACT_LAYOUTS[layout]
    width = (dimensions[0] - 2 * margin - gap * (columns - 1)) // columns
    height = (dimensions[1] - header - foot - gap * (row_count - 1)) // row_count - caption
    if min(width, height) < 35:
        raise SurveyError("The page layout leaves too little space for each photo.")
    groups = split_pages(rows, layout)
    if len(groups) > MAX_CONTACT_PAGES:
        raise Limit("The selected frames need too many contact sheets.")
    page_images = []
    for i, chunk in enumerate(groups, 1):
        page = Image.new("RGB", dimensions, CONTACT_BACKGROUND)
        d = ImageDraw.Draw(page)
        sheet_header(d, title, dimensions, dpi, i, len(groups), settings["share"])
        for j, (record, image) in enumerate(chunk):
            row, column = divmod(j, columns)
            x = margin + column * (width + gap)
            y = header + row * (height + gap + caption)
            page.paste(frame_canvas(image, (width, height)), (x, y))
            d.rectangle((x, y, x + width, y + height), outline=(159, 181, 164))
            seq = (i - 1) * columns * row_count + j + 1
            draw_caption(d, caption_lines(record, seq), (x, y + height + 2), width, dpi)
        page_images.append(page)
    return page_images


def contact_settings(form, *, share_default=False):
    return {
        "paper": option(form.get("paper"), "paper", PAPER, default="a4"),
        "layout": option(form.get("layout"), "layout", CONTACT_LAYOUTS, default="two"),
        "dpi": integer(form.get("dpi"), "dpi", 110, 200, default=144),
        "landscape": flag(form.get("landscape"), "landscape"),
        "share": flag(form.get("share"), "share", default=share_default),
        "show_marks": flag(form.get("show_marks"), "show_marks", default=True),
    }


def print_pdf(pages, dpi):
    f = BytesIO()
    if not pages:
        raise SurveyError("No contact sheet pages were produced.")
    pages[0].save(f, "PDF", resolution=dpi, save_all=True, append_images=pages[1:])
    return f.getvalue()


def receipt_rows(rows):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["frame", "site", "lighting", "time_utc", "review", "source_sha256", "synthetic"])
    for record in rows:
        cells = [record["name"], record["site"], record["illumination"], record["time_utc"], record["review"],
                 record["source_digest"], record["origin"] == "synthetic"]
        writer.writerow([safe_cell(c) for c in cells])
    return output.getvalue().encode("utf-8")


def safe_cell(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def readout(sheets, *, max_edge=1000):
    cover = sheets[0].copy()
    cover.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    return image_bytes(cover)


def bundle_files(title, frames, contact_pdf):
    parts = {"contact.pdf": contact_pdf, "frames.csv": receipt_rows([meta for meta, _ in frames])}
    metadata = []
    for i, (frame, image) in enumerate(frames, 1):
        key = f"frames/frame-{i:03}.jpg"
        binary = image_bytes(image, "JPEG", quality=89, optimize=True)
        parts[key] = binary
        metadata.append(frame | {"file": key, "preview_sha256": hashlib.sha256(binary).hexdigest(),
                                 "preview_dimensions": list(image.size)})
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in parts.items()}
    manifest = {"format": PACKET_SCHEMA, "title": title, "frames": metadata, "assets": hashes}
    parts["fieldtrace.json"] = (encoded(manifest) + "\n").encode()
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=5) as archive:
        for key, data in parts.items():
            archive.writestr(key, data)
    result = buffer.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise Limit("The evidence bundle is larger than 48 MiB.")
    return result, manifest


def preview_contact(store, form):
    title, selected, order = packet_selection(form)
    settings = contact_settings(form)
    with store.use() as db:
        inputs = collect(db, selected, order, settings["share"], settings["show_marks"])
    images = contacts(inputs, title, settings=settings)
    return readout(images)


def save_contact(store, form, *, zip_bundle=False):
    title, selected, order = packet_selection(form)
    settings = contact_settings(form, share_default=zip_bundle)
    with store.use() as db:
        files = collect(db, selected, order, settings["share"], settings["show_marks"])
    pages = contacts(files, title, settings=settings)
    pdf = print_pdf(pages, settings["dpi"])
    if len(pdf) > MAX_ARCHIVE_BYTES:
        raise Limit("The generated contact PDF is larger than 48 MiB.")
    bundle, manifest = bundle_files(title, files, pdf) if zip_bundle else (None, None)
    document = {
        "kind": "packet" if zip_bundle else "contact",
        "name": title, "settings": settings,
        "frames": [row for row, _ in files], "page_count": len(pages),
        "origins": dict(Counter(row["origin"] for row, _ in files)),
        "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "manifest": manifest, "status": "local",
    }
    with store.use(write=True) as db:
        record = db.append("packets", document, action="inspection evidence frozen")
        key = record["id"]
        db.add_asset(key, "contact", readout(pages), "image/png")
        db.add_asset(key, "pdf", pdf, "application/pdf")
        db.add_asset(key, "manifest", (encoded(manifest or document) + "\n").encode(), "application/json")
        if bundle:
            db.add_asset(key, "packet", bundle, "application/zip")
        return record | {"assets": db.assets(key)}


def safe_member(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise SurveyError("The bundle contains an unsafe file entry.")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name or any(ord(c) < 32 for c in name):
        raise SurveyError("The bundle contains an unsafe file entry.")
    return name


def archive_index(archive):
    entries = archive.infolist()
    if not 2 <= len(entries) <= MAX_ENTRY_COUNT:
        raise SurveyError("The archive does not contain a supported number of records.")
    sizes = {}
    total = 0
    for entry in entries:
        key = safe_member(entry.filename)
        if entry.is_dir() or key in sizes:
            raise SurveyError("The bundle contains duplicate or unexpected entries.")
        total += entry.file_size
        if total > MAX_EXTRACT_TOTAL or entry.file_size > MAX_ARCHIVE_BYTES:
            raise Limit("The expanded bundle exceeds its local resource limit.")
        sizes[key] = entry.file_size
    return sizes


def read_member(archive, name):
    try:
        return archive.read(name)
    except (KeyError, RuntimeError, BadZipFile, OSError) as exc:
        raise SurveyError("The bundle contains an unreadable file.") from exc


def matches_image(raw, expected):
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format != "JPEG" or list(source.size) != expected:
                raise SurveyError("The declared inspection image dimensions do not match its JPEG.")
            if source.width * source.height > 4_000_000:
                raise Limit("An inspection panel exceeds four million decoded pixels.")
            # Verify compressed structure before previewing the inspection.
            source.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, SurveyError):
            raise
        raise SurveyError("The bundle contains an invalid JPEG.") from exc


def verify_packet(binary):
    if not 0 < len(binary) <= MAX_ARCHIVE_BYTES:
        raise Limit("Choose a FieldTrace evidence ZIP no larger than 48 MiB.")
    try:
        with ZipFile(BytesIO(binary)) as archive:
            sizes = archive_index(archive)
            manifest = packet_document(archive, sizes)
            images = {}
            for info in manifest["frames"]:
                entry = info["file"]
                data = read_member(archive, entry)
                matches_image(data, info["preview_dimensions"])
                images[entry] = data
            pdf = read_member(archive, "contact.pdf")
            if not pdf.startswith(b"%PDF-"):
                raise SurveyError("The bundle contact file is not a PDF.")
    except BadZipFile as exc:
        raise SurveyError("Choose a readable FieldTrace ZIP, rather than an individual file.") from exc
    return manifest, pdf, images


def packet_document(archive, sizes):
    if "fieldtrace.json" not in sizes or sizes["fieldtrace.json"] > 3 * 1024**2:
        raise SurveyError("The archive has no supported FieldTrace inspection manifest.")
    try:
        manifest = json.loads(read_member(archive, "fieldtrace.json"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SurveyError("The inspection manifest is not readable JSON.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != PACKET_SCHEMA:
        raise SurveyError("The inspection manifest is from an unsupported format.")
    title = manifest.get("title")
    frames = manifest.get("frames")
    hashes = manifest.get("assets")
    if not isinstance(title, str) or not isinstance(hashes, dict) or not isinstance(frames, list):
        raise SurveyError("The manifest is missing its title, photo index or digest list.")
    if not 1 <= len(frames) <= MAX_PACKET_FRAMES:
        raise SurveyError("The manifest must describe between 1 and 32 inspection photos.")
    if set(hashes) != set(sizes) - {"fieldtrace.json"}:
        raise SurveyError("The digest list differs from the files in the ZIP.")
    required = {"contact.pdf", "frames.csv"}
    for frame in frames:
        need_frame_manifest(frame)
        key = safe_member(frame["file"])
        if key in required or not key.startswith("frames/") or not key.endswith(".jpg"):
            raise SurveyError("The manifest photo files must have unique JPEG entries.")
        required.add(key)
        if hashes.get(key) != frame["preview_sha256"]:
            raise SurveyError("A photo digest is inconsistent with its manifest entry.")
    if set(hashes) != required:
        raise SurveyError("The bundle has undeclared or missing payload files.")
    for name, digest in hashes.items():
        if not isinstance(digest, str) or hashlib.sha256(read_member(archive, name)).hexdigest() != digest:
            raise SurveyError("A file digest does not match the inspection manifest.")
    return manifest


def need_frame_manifest(frame):
    required = {"name", "site", "source_digest", "preview_sha256", "preview_dimensions", "file", "origin"}
    if not isinstance(frame, dict) or not required.issubset(frame):
        raise SurveyError("The manifest lacks provenance for an inspection photo.")
    width_height = frame["preview_dimensions"]
    if not isinstance(width_height, list) or len(width_height) != 2 or any(
        type(value) != int or not 1 <= value <= 2000 for value in width_height
    ):
        raise SurveyError("The manifest declares an invalid inspection image size.")
    for field in ("source_digest", "preview_sha256"):
        value = frame[field]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise SurveyError("The manifest declares an invalid content digest.")
    if frame["origin"] not in {"synthetic", "upload"} or not isinstance(frame["site"], str):
        raise SurveyError("The manifest declares an invalid source context.")


def receive_packet(store, file):
    if file is None or not file.filename:
        raise SurveyError("Choose a FieldTrace evidence bundle.", field="archive")
    binary = file.stream.read(MAX_ARCHIVE_BYTES + 1)
    manifest, pdf, pictures = verify_packet(binary)
    title = word(manifest["title"], "package title", max_length=100)
    digest = hashlib.sha256(binary).hexdigest()
    report = {
        "name": title, "kind": "received", "frames": manifest["frames"],
        "settings": None, "manifest": manifest, "status": "verified",
        "bundle_digest": digest, "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
    }
    with store.use(write=True) as db:
        existing = db.matching("packets", where=(("kind", "=", "received"),))
        for row in existing:
            if row["bundle_digest"] == digest:
                return row | {"assets": db.assets(row["id"]), "duplicate": True}
        if len(existing) >= 250:
            raise Limit("This survey has reached its received packet limit.")
        result = db.append("packets", report, action="inspection bundle verified")
        key = result["id"]
        for i, row in enumerate(manifest["frames"], 1):
            db.add_asset(key, f"image-{i:03}", pictures[row["file"]], "image/jpeg")
        db.add_asset(key, "pdf", pdf, "application/pdf")
        db.add_asset(key, "packet", binary, "application/zip")
        db.add_asset(key, "manifest", (encoded(manifest) + "\n").encode(), "application/json")
        return result | {"assets": db.assets(key), "duplicate": False}


def packet_directory(store):
    with store.use() as db:
        rows = db.matching("packets", limit=150)
        return {"packets": [
            {k: entry[k] for k in ("id", "name", "kind", "status", "created_at")}
            | {"frame_count": len(entry["frames"]), "page_count": entry.get("page_count"),
               "assets": db.assets(entry["id"])}
            for entry in rows
        ]}


def packet_detail(store, key):
    with store.use() as db:
        return db.one("packets", key) | {"assets": db.assets(key)}
