"""Chain one animal's outlined positions across the frames of a trigger event.

Only existing animal outlines become track points. Frames where the animal was
hidden stay empty: a link may span them, but no position is ever invented.
When the outlines after a crossing fit more than one continuation, the linker
stops and records a decision instead of guessing, and one circle can never be
counted for two animals.
"""

from collections import defaultdict, deque
import hashlib
import json
import math

from .catalog import revision
from .config import TAXA
from .contracts import Conflict, Limit, SurveyError, integer, list_value, need_object, number, word

MAX_TRACKS_PER_EVENT = 24
MAX_TRACK_MARKS = 600
MAX_RESOLUTIONS = 120
DEFAULT_GATE = 0.12
DEFAULT_MAX_GAP = 2


def track_form(form):
    return {
        "gate": number(form.get("gate"), "gate", 0.02, 0.6, default=DEFAULT_GATE),
        "max_gap": integer(form.get("max_gap"), "max_gap", 0, 8, default=DEFAULT_MAX_GAP),
    }


def collect(db, event_id):
    event = db.one("events", event_id)
    frames = db.event_members(event_id)
    sightings = {}
    total = 0
    for frame in frames:
        rows = db.matching(
            "marks",
            where=(("frame_id", "=", frame["id"]), ("kind", "=", "animal")),
            order=("created_at", "ASC"),
        )
        sightings[frame["id"]] = rows
        total += len(rows)
    if total > MAX_TRACK_MARKS:
        raise Limit("This event has too many animal outlines to link at once.")
    return event, frames, sightings


def outline_point(frame, mark, frame_index):
    xs = [vertex[0] for vertex in mark["polygon"]]
    ys = [vertex[1] for vertex in mark["polygon"]]
    return {
        "frame_id": frame["id"], "frame_name": frame["name"], "frame_index": frame_index,
        "mark_id": mark["id"], "mark_name": mark["name"], "mark_version": mark["version"],
        "taxon": mark["taxon"] or "unknown", "certainty": mark["certainty"],
        "centroid": [sum(xs) / len(xs), sum(ys) / len(ys)], "stamp_utc": frame["stamp_utc"],
    }


def predicted(track, frame_index):
    points = track["points"]
    last = points[-1]
    if len(points) == 1:
        return last["centroid"]
    previous = points[-2]
    back = last["frame_index"] - previous["frame_index"]
    ahead = frame_index - last["frame_index"]
    return [
        last["centroid"][axis] + (last["centroid"][axis] - previous["centroid"][axis]) / back * ahead
        for axis in (0, 1)
    ]


def clusters(candidates):
    """Group candidate links into connected track/circle clusters."""
    edges_of = defaultdict(list)
    for index, (track, point, _) in enumerate(candidates):
        edges_of[("t", track["key"])].append(index)
        edges_of[("m", point["mark_id"])].append(index)
    seen = set()
    groups = []
    for start in range(len(candidates)):
        if start in seen:
            continue
        seen.add(start)
        queue = deque([start])
        group = []
        while queue:
            edge = queue.popleft()
            group.append(edge)
            track, point, _ = candidates[edge]
            for node in (("t", track["key"]), ("m", point["mark_id"])):
                for other in edges_of[node]:
                    if other not in seen:
                        seen.add(other)
                        queue.append(other)
        groups.append(group)
    return groups


def link_tracks(frames, sightings, gate, max_gap):
    tracklets, decisions = [], []
    open_tracks = []

    def spawn(point, fresh):
        track = {"key": f"t{len(tracklets) + 1}", "taxon": point["taxon"], "points": [point], "links": []}
        tracklets.append(track)
        fresh.append(track)
        return track

    for index, frame in enumerate(frames):
        open_tracks = [t for t in open_tracks if index - t["points"][-1]["frame_index"] <= max_gap + 1]
        points = [outline_point(frame, mark, index) for mark in sightings.get(frame["id"], [])]
        candidates = []
        for track in open_tracks:
            span = index - track["points"][-1]["frame_index"]
            for point in points:
                if point["taxon"] != track["taxon"]:
                    continue
                distance = math.dist(predicted(track, index), point["centroid"])
                if distance <= gate * span:
                    candidates.append((track, point, distance))
        linked, fresh = set(), []
        for group in clusters(candidates):
            group_tracks = {candidates[edge][0]["key"] for edge in group}
            group_marks = {candidates[edge][1]["mark_id"] for edge in group}
            if len(group) == 1 and len(group_tracks) == 1 and len(group_marks) == 1:
                track, point, distance = candidates[group[0]]
                span = index - track["points"][-1]["frame_index"]
                track["links"].append({"gap_frames": span - 1, "distance": round(distance, 5), "origin": "auto"})
                track["points"].append(point)
                linked.add(point["mark_id"])
                continue
            involved = [track for track in open_tracks if track["key"] in group_tracks]
            open_tracks = [track for track in open_tracks if track["key"] not in group_tracks]
            spawned = {}
            for point in points:
                if point["mark_id"] in group_marks:
                    spawned[point["mark_id"]] = spawn(point, fresh)
                    linked.add(point["mark_id"])
            decisions.append({
                "key": f"d{len(decisions) + 1}", "frame_id": frame["id"], "frame_name": frame["name"],
                "incoming": [track["key"] for track in involved],
                "outgoing": [track["key"] for track in spawned.values()],
                "options": [
                    {
                        "incoming": candidates[edge][0]["key"],
                        "outgoing": spawned[candidates[edge][1]["mark_id"]]["key"],
                        "distance": round(candidates[edge][2], 5),
                        "gap_frames": index - candidates[edge][0]["points"][-1]["frame_index"] - 1,
                    }
                    for edge in group
                ],
            })
        for point in points:
            if point["mark_id"] not in linked:
                spawn(point, fresh)
        open_tracks.extend(fresh)
    return {"tracklets": tracklets, "decisions": decisions}


def proposal_token(event, frames, sightings, settings):
    payload = {
        "event": event["id"], "event_version": event["version"],
        "frames": [frame["id"] for frame in frames],
        "marks": {mark["id"]: mark["version"] for rows in sightings.values() for mark in rows},
        "gate": settings["gate"], "max_gap": settings["max_gap"],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:40]


def preview_tracks(store, event_id, form):
    settings = track_form(form)
    with store.use() as db:
        event, frames, sightings = collect(db, event_id)
        if db.count("tracks", equal={"event_id": event_id}):
            raise Conflict("Tracks are already saved for this event. Remove them before building a new proposal.")
        proposal = link_tracks(frames, sightings, **settings)
        token = proposal_token(event, frames, sightings, settings)
        marked = sum(len(rows) for rows in sightings.values())
    return proposal | {
        "event_id": event["id"], "event_name": event["name"], **settings,
        "proposal": token, "animal_marks": marked,
    }


def parse_resolutions(raw):
    items = list_value(raw if raw is not None else [], "resolutions", max_items=MAX_RESOLUTIONS)
    resolutions = []
    for item in items:
        entry = need_object(item, "resolution")
        links = []
        for join in list_value(entry.get("links") or [], "links", max_items=12):
            pair = need_object(join, "link")
            links.append({
                "incoming": word(pair.get("incoming"), "incoming", max_length=16),
                "outgoing": word(pair.get("outgoing"), "outgoing", max_length=16),
            })
        resolutions.append({"decision": word(entry.get("decision"), "decision", max_length=16), "links": links})
    return resolutions


def merge_chains(proposal, resolutions):
    tracklets = {track["key"]: track for track in proposal["tracklets"]}
    decisions = {decision["key"]: decision for decision in proposal["decisions"]}
    chosen, successor, predecessor, resolved = {}, {}, {}, set()
    for resolution in resolutions:
        key = resolution["decision"]
        if key not in decisions:
            raise SurveyError("This crossing is not part of the current proposal.", field="resolutions")
        if key in resolved:
            raise SurveyError("Resolve each crossing once.", field="resolutions")
        resolved.add(key)
        options = {(option["incoming"], option["outgoing"]): option for option in decisions[key]["options"]}
        for link in resolution["links"]:
            pair = (link["incoming"], link["outgoing"])
            if pair not in options:
                raise SurveyError("This continuation was not offered by the proposal. Refresh it.", field="resolutions")
            if link["incoming"] in successor:
                raise SurveyError("One earlier segment can continue into a single later segment.", field="resolutions")
            if link["outgoing"] in predecessor:
                raise SurveyError("One later segment can continue a single earlier segment.", field="resolutions")
            successor[link["incoming"]] = link["outgoing"]
            predecessor[link["outgoing"]] = link["incoming"]
            chosen[pair] = options[pair]
    chains = []
    for key in tracklets:
        if key in predecessor:
            continue
        order = []
        current = key
        while current is not None:
            order.append(current)
            current = successor.get(current)
            if len(order) > len(tracklets):
                raise Conflict("The requested continuations form a loop. Refresh the proposal.")
        chains.append(order)
    chains.sort(key=lambda order: (tracklets[order[0]]["points"][0]["frame_index"], order[0]))
    documents = []
    for order in chains:
        points, links = [], []
        previous_key = None
        for key in order:
            track = tracklets[key]
            if previous_key is not None:
                option = chosen[(previous_key, key)]
                links.append({"gap_frames": option["gap_frames"], "distance": option["distance"], "origin": "reviewed"})
            points.extend(track["points"])
            links.extend(track["links"])
            previous_key = key
        indexes = [point["frame_index"] for point in points]
        if len(set(indexes)) != len(indexes) or indexes != sorted(indexes):
            raise Conflict("The requested continuations cross in time. Refresh the proposal.")
        documents.append({"taxon": tracklets[order[0]]["taxon"], "points": points, "links": links})
    return documents


def save_tracks(store, event_id, form):
    settings = track_form(form)
    token = word(form.get("proposal"), "proposal", max_length=64)
    base = word(form.get("name"), "track name", default="")
    note = word(form.get("note"), "track note", max_length=1200, default="", multiline=True)
    resolutions = parse_resolutions(form.get("resolutions"))
    with store.use(write=True) as db:
        event, frames, sightings = collect(db, event_id)
        if proposal_token(event, frames, sightings, settings) != token:
            raise Conflict("The event or its outlines changed since the proposal. Build a fresh proposal.")
        if db.count("tracks", equal={"event_id": event_id}):
            raise Conflict("Tracks are already saved for this event. Remove them before saving a new set.")
        proposal = link_tracks(frames, sightings, **settings)
        chains = merge_chains(proposal, resolutions)
        sized = [chain for chain in chains if len(chain["points"]) >= 2]
        unlinked = [point for chain in chains if len(chain["points"]) < 2 for point in chain["points"]]
        if not sized:
            raise SurveyError("No chain of two or more circles can be stored from this proposal.")
        if len(sized) > MAX_TRACKS_PER_EVENT:
            raise Limit(f"An event can hold at most {MAX_TRACKS_PER_EVENT} stored tracks.")
        counters = defaultdict(int)
        stored = []
        for chain in sized:
            label = base or f"{TAXA.get(chain['taxon'], 'Animal')} track"
            counters[label] += 1
            name = label if len(sized) == 1 else f"{label} {counters[label]}"
            stored.append(db.append("tracks", {
                "name": name[:85], "event_id": event["id"], "event_version": event["version"],
                "taxon": chain["taxon"], "points": chain["points"], "links": chain["links"],
                "gap_frames": sum(link["gap_frames"] for link in chain["links"]),
                "reviewed_links": sum(1 for link in chain["links"] if link["origin"] == "reviewed"),
                "parameters": settings,
                "mark_versions": {point["mark_id"]: point["mark_version"] for point in chain["points"]},
                "note": note,
            }, action="individual track saved"))
    return {"tracks": stored, "unlinked": unlinked}


def with_freshness(db, track):
    states = {}
    for mark_id, version in track["mark_versions"].items():
        mark = db.one("marks", mark_id, optional=True)
        states[mark_id] = "removed" if mark is None else ("current" if mark["version"] == version else "edited")
    points = [point | {"mark_state": states[point["mark_id"]]} for point in track["points"]]
    return track | {"points": points, "stale": any(state != "current" for state in states.values())}


def list_tracks(store, event_id):
    with store.use() as db:
        db.one("events", event_id)
        rows = db.matching("tracks", where=(("event_id", "=", event_id),), order=("name", "ASC"))
        return {"tracks": [with_freshness(db, track) for track in rows]}


def remove_track(store, key, form):
    with store.use(write=True) as db:
        db.one("tracks", key)
        db.remove("tracks", key, revision(form))
    return {"removed": key}
