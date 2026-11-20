"""Reproducible review worklists with explicit source and stratum scope."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib

from .catalog import revision
from .config import ILLUMINATIONS, MAX_QUERY_ROWS, MAX_SEARCH_FRAMES, REVIEW_STATES
from .contracts import Conflict, Limit, SurveyError, date_time, flag, identifier, identity_list, integer, option, utc_text, word
from .events import frame_sort

STRATA = {
    "camera": "Camera deployment",
    "lighting": "Lighting condition",
    "date": "Local date",
    "camera-and-light": "Camera and lighting",
    "all": "Single group",
}
STATES = {"pending", "reviewed", "deferred", "excluded"}


@dataclass(frozen=True)
class SampleSettings:
    stratify: str
    count: int
    seed: int
    deployments: list
    review: str | None
    only_resolved: bool
    after: str | None
    before: str | None

    @classmethod
    def parse(cls, raw):
        choice = raw.get("review")
        after = date_time(raw["after"], "after", needs_offset=True) if raw.get("after") else None
        before = date_time(raw["before"], "before", needs_offset=True) if raw.get("before") else None
        if after and before and before <= after:
            raise SurveyError("The sample end must be later than the start.", field="before")
        selected = raw.get("deployments")
        return cls(
            option(raw.get("stratify"), "stratify", STRATA, default="camera-and-light"),
            integer(raw.get("count"), "count", 1, 18, default=3),
            integer(raw.get("seed"), "seed", 0, 2**31 - 1, default=73),
            identity_list(selected, "deployments", limit=24, required=False) if selected else [],
            option(choice, "review", REVIEW_STATES) if choice else None,
            flag(raw.get("only_resolved"), "only_resolved"),
            utc_text(after) if after else None, utc_text(before) if before else None,
        )

    def json(self):
        return self.__dict__.copy()


def frame_query(db, settings):
    rules = []
    if settings.review:
        rules.append(("review", "=", settings.review))
    if settings.after:
        rules.append(("stamp_utc", ">=", settings.after))
    if settings.before:
        rules.append(("stamp_utc", "<", settings.before))
    if settings.only_resolved:
        rules.append(("stamp_utc", "IS NOT", None))
    if not settings.deployments:
        return db.matching("frames", where=rules, order=("stamp_utc", "ASC"), limit=MAX_SEARCH_FRAMES + 1)
    rows = []
    for key in settings.deployments:
        db.one("deployments", key)
        condition = rules + [("deployment_id", "=", key)]
        rows.extend(db.matching("frames", where=condition, order=("stamp_utc", "ASC"), limit=MAX_SEARCH_FRAMES + 1))
    return sorted(rows, key=frame_sort)


def group_name(frame, classification):
    deployment = frame["deployment_id"]
    lighting = frame["illumination"]
    stamp = frame["local_stamp"]
    local_date = stamp["date"] if stamp else "unresolved date"
    return {
        "camera": (deployment,),
        "lighting": (lighting,),
        "date": (local_date,),
        "camera-and-light": (deployment, lighting),
        "all": ("All matching photos",),
    }[classification]


def group_label(group, mode, names):
    if mode == "camera-and-light":
        return f"{names.get(group[0], 'Unknown camera')} · {ILLUMINATIONS.get(group[1], group[1])}"
    head = group[0]
    if mode == "camera":
        return names.get(head, "Unknown camera")
    return ILLUMINATIONS.get(head, head) if mode == "lighting" else head


def sample_key(frame, seed, group):
    text = f"{seed}\0{' '.join(group)}\0{frame['digest']}\0{frame['id']}"
    return hashlib.sha256(text.encode()).digest()


def source_pin(frame, group, rank):
    return {
        "id": frame["id"], "name": frame["name"], "digest": frame["digest"],
        "frame_version": frame["version"], "time": frame["stamp_utc"], "stratum": list(group),
        "source_review": frame["review"], "illumination": frame["illumination"],
        "rank": rank, "decision": "pending", "review_note": "",
    }


def build_sample(db, settings):
    available = frame_query(db, settings)
    if len(available) > MAX_SEARCH_FRAMES:
        raise Limit("Narrow the sample selection to no more than 1,800 camera frames.")
    if not available:
        raise SurveyError("No camera image matches this worklist.")
    by_group = defaultdict(list)
    for frame in available:
        by_group[group_name(frame, settings.stratify)].append(frame)
    cameras = {item["id"]: item["name"] for item in db.matching("deployments")}
    groups, selected = [], []
    for group, rows in sorted(by_group.items()):
        chosen = sorted(rows, key=lambda frame: sample_key(frame, settings.seed, group))[:settings.count]
        selected.extend(source_pin(frame, group, i) for i, frame in enumerate(chosen, 1))
        groups.append({"key": list(group), "name": group_label(group, settings.stratify, cameras),
                       "available": len(rows), "chosen": len(chosen), "shortfall": max(0, settings.count - len(chosen))})
    if len(selected) > MAX_QUERY_ROWS:
        raise Limit("Reduce the number of strata or photos per stratum so this worklist has at most 250 entries.")
    return {
        "settings": settings.json(), "eligible_count": len(available), "selected_count": len(selected),
        "strata": groups, "members": selected, "counts": dict(Counter(decision["decision"] for decision in selected)),
        "method": "Deterministic content-key order within each selected stratum",
    }


def sample_preview(store, form):
    settings = SampleSettings.parse(form)
    with store.use() as db:
        return build_sample(db, settings)


def save_sample(store, form):
    settings = SampleSettings.parse(form)
    name = word(form.get("name"), "worklist title")
    with store.use(write=True) as db:
        report = build_sample(db, settings)
        return db.append("samples", report | {"name": name}, action="survey sample picked")


def update_member(store, key, form):
    frame_key = identifier(form.get("frame"), "frame")
    decision = option(form.get("decision"), "decision", STATES)
    note = word(form.get("note"), "review note", max_length=1400, default="", multiline=True)
    with store.use(write=True) as db:
        current = db.one("samples", key)
        frame = db.one("frames", frame_key)
        matched = next((row for row in current["members"] if row["id"] == frame_key), None)
        if matched is None or matched["digest"] != frame["digest"]:
            raise Conflict("The selected image does not belong to this frozen worklist.")
        member = matched | {"decision": decision, "review_note": note, "frame_at_decision": frame["version"]}
        members = [member if row["id"] == frame_key else row for row in current["members"]]
        counts = dict(Counter(row["decision"] for row in members))
        return db.replace("samples", key, revision(form), lambda old: old | {"members": members, "counts": counts},
                          action="sample review decision saved")
