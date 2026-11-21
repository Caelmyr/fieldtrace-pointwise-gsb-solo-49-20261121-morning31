"""HTTP boundary for the local FieldTrace workspace."""

from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore
from functools import wraps

from flask import Flask, Response, jsonify, request, send_file
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from . import activity, alignment, baseline, catalog, effort, events, evidence, geometry, identity
from . import inspection, journal, pairs, probe, profiles, reconcile, sampling, sequences, texture
from .config import (
    DATA, DEPLOYMENT_STATES, HABITATS, ILLUMINATIONS, MARK_KINDS, MAX_BATCH_BYTES, OUTAGES,
    POSES, REVIEW_STATES, STATIC, TAXA,
)
from .contracts import Missing, SurveyError, identifier, need_object
from .demo import create_demo
from .images import image_bytes, safe_filename
from .store import Store

NAMES = {
    "application/zip": ".zip", "application/pdf": ".pdf", "application/json": ".json",
    "image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tiff", "image/webp": ".webp",
}


def body():
    return need_object(request.get_json(silent=False))


def png(image):
    payload = image if isinstance(image, bytes) else image_bytes(image)
    return Response(payload, mimetype="image/png", headers={"Cache-Control": "no-store"})


def register_catalog(api, store, work):
    @api.get("/api/health")
    def health():
        with store.use() as db:
            db.conn.execute("SELECT 1").fetchone()
        return {"status": "ready"}

    @api.get("/api/options")
    def options():
        return {
            "habitats": HABITATS, "deployments": DEPLOYMENT_STATES, "lighting": ILLUMINATIONS,
            "review": REVIEW_STATES, "marks": MARK_KINDS, "taxa": TAXA, "poses": POSES,
            "outages": OUTAGES, "strata": sampling.STRATA,
        }

    @api.get("/api/stations")
    def stations():
        return catalog.stations_view(store)

    @api.post("/api/stations")
    def create_station():
        return catalog.save_station(store, body()), 201

    @api.put("/api/stations/<key>")
    def update_station(key):
        return catalog.save_station(store, body(), identifier(key))

    @api.post("/api/deployments")
    def add_deployment():
        return catalog.save_deployment(store, body()), 201

    @api.put("/api/deployments/<key>")
    def update_deployment(key):
        return catalog.save_deployment(store, body(), identifier(key))

    @api.get("/api/deployments/<key>/outages")
    def outages(key):
        return effort.outages_for(store, identifier(key))

    @api.post("/api/outages")
    def record_outage():
        return effort.save_outage(store, body()), 201

    @api.put("/api/outages/<key>")
    def revise_outage(key):
        return effort.save_outage(store, body(), identifier(key))

    @api.post("/api/demo")
    @work
    def load_training():
        return create_demo(store), 201

    @api.get("/api/frames")
    def find_frames():
        return catalog.frame_listing(store, request.args)

    @api.post("/api/deployments/<key>/photos")
    @work
    def photo_intake(key):
        selection = request.files.getlist("images")
        return catalog.receive_frames(store, identifier(key), selection, request.form), 201

    @api.get("/api/frames/<key>")
    def frame_info(key):
        return catalog.inspect_frame(store, identifier(key))

    @api.put("/api/frames/<key>/time")
    def review_time(key):
        return catalog.edit_stamp(store, identifier(key), body())

    @api.post("/api/frames/review")
    def review_frames():
        return catalog.review_frames(store, body())

    @api.post("/api/frames/<key>/marks")
    def draw_mark(key):
        return geometry.save_mark(store, identifier(key), body()), 201

    @api.put("/api/frames/<key>/marks/<mark>")
    def revise_mark(key, mark):
        return geometry.save_mark(store, identifier(key), body(), key=identifier(mark))

    @api.delete("/api/frames/<key>/marks/<mark>")
    def remove_mark(key, mark):
        return geometry.remove_mark(store, identifier(key), identifier(mark), body())

    @api.get("/api/frames/<key>/marked.png")
    @work
    def show_marks(key):
        return png(geometry.marks_preview(store, identifier(key)))

    @api.get("/api/frames/<key>/display.png")
    @work
    def show_tone(key):
        return png(inspection.zoom_image(store, identifier(key), request.args))

    @api.get("/api/frames/compare")
    def compare_frames():
        return catalog.frame_compare(store, identifier(request.args.get("first")), identifier(request.args.get("second")))

    @api.post("/api/camera-log/preview")
    def review_camera_log():
        return reconcile.preview_reconcile(store, body())

    @api.post("/api/camera-log/apply")
    def apply_camera_log():
        return reconcile.apply_reconcile(store, body())


def register_inspections(api, store, work):
    @api.post("/api/frames/<key>/quality")
    @work
    def quality(key):
        return inspection.save_audit(store, identifier(key), body()), 201

    @api.post("/api/frames/<key>/motion")
    @work
    def motion(key):
        return inspection.save_motion(store, identifier(key), body()), 201

    @api.post("/api/frames/<key>/baseline/preview")
    @work
    def baseline_preview(key):
        report, _ = baseline.preview_baseline(store, identifier(key), body())
        return {"report": report}

    @api.post("/api/frames/<key>/baseline/preview.png")
    @work
    def baseline_preview_image(key):
        _, view = baseline.preview_baseline(store, identifier(key), body())
        return png(view)

    @api.post("/api/frames/<key>/baseline")
    @work
    def baseline_save(key):
        return baseline.save_baseline(store, identifier(key), body()), 201

    @api.post("/api/frames/<key>/profile")
    @work
    def line_profile(key):
        return profiles.save_profile(store, identifier(key), body()), 201

    @api.post("/api/frames/<key>/patch")
    @work
    def patch(key):
        return profiles.patch_inspection(store, identifier(key), body())

    @api.post("/api/frames/<key>/placement")
    @work
    def place(key):
        return alignment.create_alignment(store, identifier(key), body()), 201

    @api.post("/api/frames/<key>/window-compare")
    @work
    def compare_window(key):
        return pairs.compare_photos(store, identifier(key), body()), 201

    @api.post("/api/frames/<key>/detail-bands")
    @work
    def inspect_detail_bands(key):
        return pairs.blur_comparison(store, identifier(key), body())

    @api.post("/api/frames/<key>/pattern")
    @work
    def examine_pattern(key):
        return texture.save_pattern(store, identifier(key), body()), 201

    @api.post("/api/frames/<key>/pattern/preview")
    @work
    def local_pattern_probe(key):
        return texture.pattern_probe(store, identifier(key), body())

    @api.post("/api/frames/<key>/probe")
    @work
    def pixel_probe(key):
        return probe.pixel_probe(store, identifier(key), body())

    @api.post("/api/frames/<key>/probe/preview")
    @work
    def pixel_probe_image(key):
        return png(probe.pixel_probe(store, identifier(key), body(), preview=True))

    @api.get("/api/ground-planes")
    def show_ground_planes():
        key = identifier(request.args.get("deployment"), "deployment", optional=True)
        where = (("deployment_id", "=", key),) if key else ()
        with store.use() as db:
            return {"models": db.matching("models", where=where, limit=80)}

    @api.post("/api/ground-planes")
    def save_ground_plane():
        return geometry.save_model(store, body()), 201

    @api.get("/api/ground-planes/<key>/guide.png")
    @work
    def ground_plane_guide(key):
        return png(geometry.guide_image(store, identifier(key)))

    @api.post("/api/ground-measures")
    def measure_ground():
        return geometry.save_measure(store, body()), 201

    @api.get("/api/identities")
    def identities():
        return identity.find_identities(store, request.args)

    @api.post("/api/identities")
    def create_identity():
        return identity.save_identity(store, body()), 201

    @api.get("/api/identities/<key>")
    def saved_identity(key):
        return identity.read_identity(store, identifier(key))

    @api.put("/api/identities/<key>")
    def update_identity(key):
        return identity.save_identity(store, body(), identifier(key))

    @api.post("/api/identities/<key>/portraits")
    @work
    def add_identity_portrait(key):
        return identity.save_portrait(store, identifier(key), body()), 201

    @api.get("/api/portraits/compare.png")
    @work
    def inspect_portraits():
        first = identifier(request.args.get("first"), "first")
        next_ = identifier(request.args.get("second"), "second")
        return png(identity.compare_portraits(store, first, next_))

    @api.get("/api/portraits")
    def portrait_directory():
        return identity.query_portraits(store, request.args)

    @api.post("/api/frames/<key>/crop-preview")
    @work
    def preview_crop(key):
        return png(identity.crop_preview(store, identifier(key), body()))


def register_events(api, store, work):
    @api.get("/api/events")
    def list_events():
        key = identifier(request.args.get("deployment"), "deployment", optional=True)
        return events.list_events(store, key)

    @api.get("/api/events/<key>")
    def get_event(key):
        return events.read_event(store, identifier(key))

    @api.post("/api/events")
    def create_event():
        return events.register_event(store, body()), 201

    @api.put("/api/events/<key>")
    def update_event(key):
        return events.register_event(store, body(), key=identifier(key))

    @api.post("/api/events/preview")
    def preview_events():
        return events.preview_events(store, body())

    @api.post("/api/events/approve")
    def confirm_suggested():
        return events.resolve_preset_groups(store, body()), 201

    @api.post("/api/events/<key>/strip")
    @work
    def save_sequence(key):
        return sequences.make_strip(store, identifier(key), body()), 201

    @api.post("/api/events/<key>/strip/preview")
    @work
    def preview_sequence(key):
        return png(sequences.make_strip(store, identifier(key), body(), save=False))

    @api.post("/api/event-paths")
    def trace_path():
        return events.save_path(store, body()), 201

    @api.get("/api/neighbours")
    def neighbours():
        return sequences.relative_rows(store, request.args)


def register_reports(api, store, work):
    @api.post("/api/effort")
    def effort_preview():
        return effort.survey_effort(store, body())

    @api.post("/api/effort/save")
    def save_effort():
        return effort.survey_effort(store, body(), save=True), 201

    @api.post("/api/activity")
    def activity_preview():
        return activity.activity_chart(store, body())

    @api.post("/api/activity/save")
    @work
    def save_activity():
        return activity.activity_chart(store, body(), save=True), 201

    @api.get("/api/register.csv")
    def frame_register():
        return csv_download(journal.frame_register(store, request.args), "camera-register")

    @api.get("/api/reports/<key>.csv")
    def saved_report_csv(key):
        return csv_download(journal.report_csv(store, identifier(key)), "survey-report")

    @api.get("/api/attachment-check/<key>")
    def attachment_review(key):
        return journal.record_integrity(store, identifier(key), request.args)

    @api.get("/api/reports")
    def reports():
        return sequences.parse_saved_records(store, "reports", request.args)

    @api.get("/api/samples")
    def get_samples():
        return sequences.parse_saved_records(store, "samples", request.args)

    @api.post("/api/samples/preview")
    def sample_preview():
        return sampling.sample_preview(store, body())

    @api.post("/api/samples")
    def save_sample():
        return sampling.save_sample(store, body()), 201

    @api.put("/api/samples/<key>/review")
    def decide_sample(key):
        return sampling.update_member(store, identifier(key), body())

    @api.get("/api/packets")
    def packets():
        return evidence.packet_directory(store)

    @api.get("/api/packets/<key>")
    def read_packet(key):
        return evidence.packet_detail(store, identifier(key))

    @api.post("/api/contact/preview")
    @work
    def preview_contact_sheet():
        return png(evidence.preview_contact(store, body()))

    @api.post("/api/contact")
    @work
    def make_contact():
        return evidence.save_contact(store, body()), 201

    @api.post("/api/packets")
    @work
    def make_packet():
        return evidence.save_contact(store, body(), zip_bundle=True), 201

    @api.post("/api/packets/receive")
    @work
    def accept_packet():
        return evidence.receive_packet(store, request.files.get("archive")), 201


def csv_download(data, title):
    return send_file(BytesIO(data), mimetype="text/csv", as_attachment=True, download_name=title + ".csv")


def home_counts(store):
    with store.use() as db:
        counts = {table: db.count(table) for table in ("stations", "deployments", "frames", "identities", "events", "reports")}
        pending = sum(db.count("frames", equal={"review": state}) for state in ("new", "held"))
        recent = db.matching("frames", order=("created_at", "DESC"), limit=8)
        sample = db.preference("training_survey")
    return {"counts": counts | {"pending": pending}, "recent": [sequences.frame_summary(row) for row in recent],
            "training": sample is not None}


def create_app(root=DATA):
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_BATCH_BYTES + 2 * 1024 ** 2
    store = Store(Path(root))
    app.extensions["fieldtrace"] = store
    heavy = BoundedSemaphore(2)

    def work(function):
        @wraps(function)
        def protected(*args, **kwargs):
            if not heavy.acquire(blocking=False):
                raise SurveyError("Two image jobs are already running. Try again shortly.", status=503, code="busy")
            try:
                return function(*args, **kwargs)
            finally:
                heavy.release()
        return protected

    @app.get("/")
    def home():
        return app.send_static_file("index.html")

    @app.get("/api/home")
    def summary():
        return home_counts(store)

    @app.get("/api/records/<category>/<key>")
    def typed_record(category, key):
        if category not in {"samples", "reports", "paths", "glances", "measures", "portraits", "outages"}:
            raise Missing("record")
        with store.use() as db:
            item = db.one(category, identifier(key))
            return item | {"assets": db.assets(item["id"])}

    @app.get("/api/history/<key>")
    def history(key):
        with store.use() as db:
            return {"history": db.history(identifier(key))}

    @app.get("/api/file/<key>/<label>")
    def file(key, label):
        with store.use() as db:
            asset = db.asset(identifier(key), label)
        mime = asset["media_type"]
        filepath = store.blobs.path(asset["digest"])
        if not filepath.is_file():
            raise Missing("file")
        return send_file(
            filepath, mimetype=mime, etag=asset["digest"], conditional=True,
            as_attachment=label in {"source", "packet", "pdf", "manifest"},
            download_name=safe_filename(label) + NAMES.get(mime, ".bin"),
        )

    @app.errorhandler(SurveyError)
    def survey_error(exc):
        return jsonify(exc.packet()), exc.status

    @app.errorhandler(BadRequest)
    def bad_request(exc):
        return jsonify(SurveyError("The request body is not valid JSON.").packet()), 400

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(exc):
        return jsonify(SurveyError("The upload exceeds the request limit.", status=413).packet()), 413

    register_catalog(app, store, work)
    register_inspections(app, store, work)
    register_events(app, store, work)
    register_reports(app, store, work)
    return app
