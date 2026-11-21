"""Deployment limits and public vocabularies for a local survey workbench."""

import os
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
HOME_DATA = Path.home() / ".local" / "share" / "fieldtrace"
DATA = Path(os.environ.get("FIELDTRACE_DATA", HOME_DATA))

MIB = 1024 * 1024
MAX_IMAGE_BYTES = 16 * MIB
MAX_BATCH_BYTES = 64 * MIB
MAX_ARCHIVE_BYTES = 48 * MIB
MAX_PARSED_PIXELS = 32_000_000
MAX_BATCH_PIXELS = 70_000_000
MAX_FRAME_EDGE = 1800
MAX_THUMB_EDGE = 400
MAX_FRAME_COUNT = 4800
MAX_BATCH_FILES = 20
MAX_EVENT_FRAMES = 90
MAX_WITNESSES = 80
MAX_MARKS_PER_FRAME = 36
MAX_POLYGON_POINTS = 60
MAX_SEARCH_FRAMES = 1800
MAX_CONTACT_PAGES = 12
MAX_PACKET_FRAMES = 32
MAX_WALK_STEPS = 120
MAX_PLANAR_PAIRS = 16
MAX_MEASURES = 80
MAX_SITE_COUNT = 96
MAX_VISIT_DAYS = 730
MAX_QUERY_ROWS = 250
MAX_SPAN_DAYS = 365
DEFAULT_EVENT_GAP = 45

MEDIA_TYPES = {
    "source": "image/jpeg",
    "work": "image/png",
    "thumbnail": "image/jpeg",
    "quality": "image/png",
    "motion": "image/png",
    "assist": "image/png",
    "provenance": "image/png",
    "contact": "image/png",
    "guide": "image/png",
    "packet": "application/zip",
}
IMAGE_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
}
HABITATS = {
    "woodland": "Woodland",
    "scrub": "Scrub",
    "grassland": "Grassland",
    "wetland-edge": "Wetland edge",
    "farm-edge": "Farm edge",
    "path": "Path corridor",
    "other": "Other habitat",
}
DEPLOYMENT_STATES = {
    "active": "Active",
    "service": "Service hold",
    "retired": "Retired",
}
ILLUMINATIONS = {
    "day-color": "Day colour",
    "night-ir": "Night infrared",
    "dusk": "Dusk or dawn",
    "other": "Unknown lighting",
}
ORIGINS = {"upload", "synthetic"}
REVIEW_STATES = {
    "new": "Unreviewed",
    "held": "Needs review",
    "seen": "Animal observed",
    "empty": "No animal observed",
    "unusable": "Unusable image",
}
MARK_KINDS = {
    "animal": "Animal region",
    "identity": "Identifying marking",
    "occlusion": "Obstruction",
    "ignore": "Exclude from image analysis",
    "restricted": "Restricted from sharing",
    "fixture": "Fixed reference",
}
TAXA = {
    "badger": "Badger",
    "deer": "Deer",
    "fox": "Fox",
    "hare": "Hare",
    "small-mammal": "Small mammal",
    "bird": "Bird",
    "unknown": "Uncertain animal",
}
POSES = {
    "left": "Left flank",
    "right": "Right flank",
    "front": "Front",
    "back": "Back",
    "unknown": "Unknown view",
}
CERTAINTIES = {"observed", "probable", "uncertain"}
OUTAGES = {
    "power": "Power loss",
    "obstructed": "Camera obstructed",
    "service": "Scheduled service",
    "position": "Moved or rotated",
    "unknown": "Unknown state",
}
EFFORT_MODES = {"active", "day-window", "night-window"}
MOTION_DIRECTIONS = {"approach", "depart", "crossing", "unresolved"}
SORT_KEYS = {
    "stamp": ("stamp_utc", "ASC"),
    "newest": ("stamp_utc", "DESC"),
    "name": ("name", "ASC"),
    "recent-upload": ("created_at", "DESC"),
}
