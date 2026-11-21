# FieldTrace

FieldTrace is a local image workbench for reviewing wildlife camera surveys. Camera bodies and time records sit alongside original photos, oriented working copies and source-space observations. The operator can inspect changes in a static-camera pair, retain measured landmarks and prepare printable views without giving up the source positions.

## Start the workspace

```sh
docker compose up --build -d
```

Open http://127.0.0.1:19952. The container is healthy when `GET /api/health` reports `ready`. Stop the service with `docker compose stop`. The `survey-library` named volume keeps the SQLite index, originals and output files. Set `FIELDTRACE_PORT` if 19952 is occupied. The app binds its published port to localhost. To run with your own Python 3.11 environment, install `requirements.txt` and use `./start.sh`. It defaults to localhost port 8000 when run outside Docker. Set `FIELDTRACE_DATA` to choose a writable data directory.

## Explore a survey

Use **Load training survey** on Overview. It generates 16 original procedural training images, with two fictional stations, an infrared sequence, glare and one visible-support gap. The UI marks these records as synthetic. No field photographs or wildlife occurrences are claimed.

- **Stations** establishes named camera deployments, time zones, clock uncertainty and camera outages. Preview a camera CSV log before applying unambiguous timestamps. Resolved frames and source timestamps remain distinguishable.
- **Photos** imports bounded camera images into a deployment. Intake keeps the original digest and oriented working copy, skips exact content already in that deployment, and supports unresolved EXIF time. Select one photo to mark its image, step to nearby frames, probe original positions or record a sighting.
- **Image inspections** maps support, shadows, highlights and local edge detail. A same-frame-size comparison can show common display ranges, static-camera changes or fixed landmark displacement. Profiles and the marking direction map retain their sample positions. These views leave originals and source review intact.
- **Audit camera placement** keeps the original manual landmark pairs, and adds an assisted landing-point step: circle small fixed marks on the earlier photo, give a plausible shift direction and search window, and the workbench proposes candidate landings on the current photo with local-correlation scores. Repeated texture, weak matches, blocked patches and photo-edge marks stay flagged for operator review, and a shared-shift consensus shows whether the preferred landings tell one story. Only points you confirm feed the displacement estimate; suggestions never confirm themselves.
- **Ground geometry** fits a plane supplied by existing field anchors. The supported region and fit residual constrain distances. Enter known ground coordinates and source image points, then measure a trail segment or trace visible ground contact through a trigger burst.
- **Events** previews fixed-gap camera groups, holds unresolved order, supports reviewed events and freezes an inspection strip with links back to camera frames. Manual event edits remain available.
- **Individuals** stores cropped witness markings with body side, illumination and source visibility. Compare retained witnesses for manual interpretation.
- **Survey reports** records active station hours with outages and overlapping cameras, counts reviewed confirmed triggers, and lays out activity in local clock time. Frozen sample worklists have a reproducible seed and per-entry decisions.
- **Evidence** previews contact sheets, freezes PDFs and optionally creates a portable ZIP. Sharing copies hide restricted regions and strip source metadata from the JPEG panels. Local verification checks the provided manifest, image geometry and file digests and retains indexed inspection copies. Review camera/library records and download their CSV or report attachments.

## Limits and readings

JPEG, PNG, single-image TIFF and WebP are supported. One file may use up to 16 MiB or 32 million decoded pixels. Batches may contain up to 20 photos, 64 MiB and 70 million source pixels. The working view is oriented and capped at 1,800 pixels on its longest edge. Alpha below the visibility threshold stays unavailable. Working-image measurements use 8-bit display levels and the manual exclusion marks.

Camera-clock ambiguity and unsupported pixels are reported separately from any observation. Movement candidates require matching framing and lighting and can contain wind, lighting changes, animals or other motion. Placement landing suggestions are an unconfirmed review aid: brightness-robust local correlation can still be fooled by repeated bark texture or moving foliage, so occluded, edge and near-tied matches are held for the operator and never enter the displacement calculation on their own. Planar distances depend on operator-provided positions on the same ground plane. Direction/lag evidence assists inspection of markings. Station rates describe reviewed detections and recorded active hours. Source thumbnails, printed plates and ZIP verification run entirely on the local service.

Published limits include 4,800 frames, 90 frames per frozen event and a 32-frame evidence packet. Survey reports allow up to 365 calendar days. Rendering and image intake have bounded concurrency. The software does not publish packets or contact a remote data service.
