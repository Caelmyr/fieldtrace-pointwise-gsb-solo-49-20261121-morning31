"""Marking-witness search: readings, unresolved areas and saved searches."""

import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

from fieldtrace import matching
from fieldtrace.catalog import register_photo
from fieldtrace.demo import create_demo
from fieldtrace.images import decode, image_bytes
from fieldtrace.web import create_app


@pytest.fixture()
def survey(tmp_path):
    app = create_app(str(tmp_path / "survey"))
    store = app.extensions["fieldtrace"]
    created = create_demo(store)
    return app.test_client(), store, created


def frame_photo(store, deployment_id, seed, *, marking="stripe", light="day-color", name="extra.jpg"):
    """A plain scene with one of two clearly different flank markings."""
    generator = np.random.default_rng(seed)
    image = Image.new("RGB", (600, 400), (120, 112, 96))
    painter = ImageDraw.Draw(image)
    painter.ellipse((120, 120, 260, 260), fill=(64, 58, 48))
    painter.polygon([(300, 100), (340, 160), (310, 250), (285, 170)], fill=(158, 134, 100))
    if marking == "stripe":
        painter.rectangle((190, 290, 300, 318), fill=(244, 236, 214))
    elif marking == "patch":
        painter.ellipse((200, 286, 246, 322), fill=(30, 28, 24))
        painter.rectangle((250, 292, 296, 310), fill=(238, 226, 200))
    noise = generator.normal(0, 3, (400, 600, 3))
    values = np.clip(np.asarray(image, dtype=np.float32) + noise, 0, 255).astype(np.uint8)
    photo = decode(image_bytes(Image.fromarray(values), "JPEG", quality=92))
    with store.use(write=True) as db:
        deployment = db.one("deployments", deployment_id)
        frame, _ = register_photo(db, deployment, name, photo, {
            "illumination": light, "override": "2026-09-17T08:00:00+00:00",
            "fold": None, "note": "",
        })
    return frame


def add_witness(store, identity, frame, box, region, pose="left", name="Witness"):
    from fieldtrace.identity import crop_support, pose_panel
    from fieldtrace.images import fetch_work
    with store.use(write=True) as db:
        photo = fetch_work(db, frame["id"])
        crop, support, quality = crop_support(photo, box, [])
        record = db.append("portraits", {
            "identity_id": identity["id"], "frame_id": frame["id"], "name": name,
            "pose": pose, "region": region, "box": box, "certainty": "probable", "note": "",
            "source_digest": frame["digest"], "source_extent": frame["extent"],
            "source_version": frame["version"], "identity_version": identity["version"],
            "illumination": frame["illumination"], "witness_time": frame["stamp_utc"],
            "quality": quality, "mask_versions": {},
        })
        db.add_asset(record["id"], "guide", image_bytes(pose_panel(crop, support)), "image/png")
        return record


def test_query_requires_a_side_and_one_area():
    with pytest.raises(matching.SurveyError, match="left or right"):
        matching.WitnessQuery.parse({"pose": "back", "regions": [{"region": "ear", "box": [0, 0, .2, .2]}]})
    with pytest.raises(matching.SurveyError, match="only once"):
        matching.WitnessQuery.parse({"pose": "left", "regions": [
            {"region": "ear", "box": [0, 0, .2, .2]}, {"region": "ear", "box": [0, .3, .2, .2]}]})
    with pytest.raises(matching.SurveyError, match="At least 1"):
        matching.WitnessQuery.parse({"pose": "left", "regions": []})
    with pytest.raises(matching.SurveyError, match="rectangle"):
        matching.WitnessQuery.parse({"pose": "left",
                                     "regions": [{"region": "ear", "box": [.5, .5, .6, .001]}]})


def test_candidate_kept_and_absent_area_left_unresolved(survey):
    client, store, created = survey
    with store.use() as db:
        ear = next(row for row in db.matching("portraits") if row["region"] == "ear")
        target = next(row for row in db.matching("frames", where=(("review", "=", "seen"),))
                      if row["id"] != ear["frame_id"])
    answer = client.post(f"/api/frames/{target['id']}/witness-search", json={
        "pose": "left", "taxon": "fox", "minimum": 30,
        "regions": [{"region": "ear", "box": ear["box"]}],
    }).json
    subject = next(row for row in answer["subjects"] if row["identity"]["name"] == "Willow")
    assert subject["standing"] in {"candidate", "weak"}
    assert subject["regions"][0]["status"] != "no-witness"
    # The flank witness exists, but this photo was not marked for it: unresolved, never a mismatch.
    assert [row["region"] for row in subject["unmarked_regions"]] == ["flank"]


def test_other_side_witness_is_listed_not_compared(survey):
    client, store, created = survey
    with store.use() as db:
        bonnie = db.append("identities", {"name": "Bonnie", "taxon": "fox", "status": "provisional",
                                          "features": ""})
        right = db.matching("frames", where=(("review", "=", "seen"),), limit=1)[0]
    add_witness(store, bonnie, right, [0.36, 0.58, 0.2, 0.16], "flank", pose="right", name="Right flank")
    with store.use() as db:
        left = next(row for row in db.matching("frames", where=(("review", "=", "seen"),))
                    if row["id"] != right["id"])
    answer = client.post(f"/api/frames/{left['id']}/witness-search", json={
        "pose": "left", "taxon": "fox", "minimum": 30,
        "regions": [{"region": "flank", "box": [0.36, 0.58, 0.2, 0.16]}],
    }).json
    assert all(row["identity"]["name"] != "Bonnie" for row in answer["subjects"])
    listed = next(row for row in answer["other_side"] if row["identity"]["name"] == "Bonnie")
    assert listed["poses"] == ["right"]
    assert listed["portraits"][0]["region"] == "flank"


def test_occluded_marked_area_is_not_a_mismatch(survey):
    client, store, created = survey
    with store.use() as db:
        ear = next(row for row in db.matching("portraits") if row["region"] == "ear")
        target = next(row for row in db.matching("frames", where=(("review", "=", "seen"),))
                      if row["id"] != ear["frame_id"])
        box = ear["box"]
        x, y, width, height = box
        db.append("marks", {"name": "Branch", "kind": "occlusion", "frame_id": target["id"],
                            "polygon": [(x, y), (x + width, y), (x + width, y + height), (x, y + height)],
                            "note": ""})
    answer = client.post(f"/api/frames/{target['id']}/witness-search", json={
        "pose": "left", "taxon": "fox", "minimum": 30,
        "regions": [{"region": "ear", "box": box}],
    }).json
    assert answer["regions"][0]["status"] == "not-visible"
    subject = answer["subjects"][0]
    assert subject["standing"] == "unresolved"
    assert subject["regions"][0]["status"] == "not-visible"
    assert subject["regions"][0]["status"] != "differs"


def test_different_clear_marking_reads_as_differs(survey):
    client, store, created = survey
    with store.use() as db:
        deployment = db.one("deployments", created["deployments"][0])
        identity = db.append("identities", {"name": "Ash", "taxon": "fox", "status": "provisional",
                                            "features": ""})
    reference = frame_photo(store, deployment["id"], 41, marking="stripe", name="ash-a.jpg")
    query = frame_photo(store, deployment["id"], 43, marking="patch", name="ash-c.jpg")
    box = [0.30, 0.70, 0.22, 0.10]
    add_witness(store, identity, reference, box, "flank", name="Flank stripe")
    answer = client.post(f"/api/frames/{query['id']}/witness-search", json={
        "pose": "left", "taxon": "fox", "minimum": 30,
        "regions": [{"region": "flank", "box": box}],
    }).json
    subject = next(row for row in answer["subjects"] if row["identity"]["name"] == "Ash")
    assert subject["regions"][0]["status"] == "differs"
    assert subject["standing"] == "differs"


def test_identical_marking_stays_a_candidate(survey):
    client, store, created = survey
    with store.use() as db:
        deployment = db.one("deployments", created["deployments"][0])
        identity = db.append("identities", {"name": "Rook", "taxon": "fox", "status": "provisional",
                                            "features": ""})
    first = frame_photo(store, deployment["id"], 51, marking="stripe", name="rook-a.jpg")
    again = frame_photo(store, deployment["id"], 51, marking="stripe", name="rook-b.jpg")
    box = [0.30, 0.70, 0.22, 0.10]
    add_witness(store, identity, first, box, "flank", name="Flank stripe")
    answer = client.post(f"/api/frames/{again['id']}/witness-search", json={
        "pose": "left", "taxon": "fox", "minimum": 30,
        "regions": [{"region": "flank", "box": box}],
    }).json
    subject = next(row for row in answer["subjects"] if row["identity"]["name"] == "Rook")
    assert subject["regions"][0]["status"] == "resembles"
    assert subject["standing"] == "candidate"


def test_saved_search_reopens_with_its_areas(survey):
    client, store, created = survey
    with store.use() as db:
        ear = next(row for row in db.matching("portraits") if row["region"] == "ear")
        target = next(row for row in db.matching("frames", where=(("review", "=", "seen"),))
                      if row["id"] != ear["frame_id"])
    saved = client.post(f"/api/frames/{target['id']}/witness-search/save", json={
        "pose": "left", "taxon": "fox", "minimum": 30, "name": "Ear check",
        "regions": [{"region": "ear", "box": ear["box"]}],
    })
    assert saved.status_code == 201
    item = client.get(f"/api/records/glances/{saved.json['id']}").json
    assert item["query"]["pose"] == "left"
    assert item["query"]["regions"][0]["region"] == "ear"
    assert item["report"]["subjects"]
    assert client.get(f"/api/glances/{saved.json['id']}/marked.png").status_code == 200
    frame = client.get(f"/api/frames/{target['id']}").json
    assert any(row["kind"] == "witness-search" for row in frame["glances"])


def test_witness_without_a_labelled_area_is_not_matched(survey):
    client, store, created = survey
    with store.use() as db:
        ear = next(row for row in db.matching("portraits") if row["region"] == "ear")
        db.replace("portraits", ear["id"], ear["version"],
                   lambda old: {key: value for key, value in old.items() if key != "region"})
        target = next(row for row in db.matching("frames", where=(("review", "=", "seen"),))
                      if row["id"] != ear["frame_id"])
    answer = client.post(f"/api/frames/{target['id']}/witness-search", json={
        "pose": "left", "taxon": "fox", "minimum": 30,
        "regions": [{"region": "ear", "box": ear["box"]}],
    }).json
    assert answer["subjects"][0]["regions"][0]["status"] == "no-witness"


def test_relabel_witness_keeps_history(survey):
    client, store, created = survey
    with store.use() as db:
        portrait = db.matching("portraits")[0]
    updated = client.put(f"/api/portraits/{portrait['id']}",
                         json={"region": "tail", "version": portrait["version"]})
    assert updated.status_code == 200
    assert updated.json["region"] == "tail"
    history = client.get(f"/api/history/{portrait['id']}").json["history"][0]
    assert history["action"] == "witness area labelled"


def test_comparison_refuses_a_same_photo_witness(survey):
    client, store, created = survey
    with store.use() as db:
        portrait = db.matching("portraits")[0]
    response = client.get("/api/witness-compare.png", query_string={
        "frame": portrait["frame_id"], "witness": portrait["id"],
        "box": json.dumps(portrait["box"]),
    })
    assert response.status_code == 400
    assert "another photo" in response.json["error"]["message"]
