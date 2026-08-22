import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from gosync import web
from gosync.auth import AuthConfig
from gosync.constants import AUTH_METHOD_API_TOKEN
from gosync.events import RECENT_EVENTS, log_event
from gosync.manifest import MediaManifest
from gosync.paths import media_download_path
from gosync.progress import ProgressState
from gosync.runtime import PreparedManifestState, RuntimePaths


def start_button_disabled(page: str) -> bool:
    start = page.index('id="start-button"')
    tag_end = page.index(">", start)
    return "disabled" in page[start:tag_end]


class FakeThread:
    instances = []

    def __init__(self, *, target, args, daemon, kwargs=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.started = False
        FakeThread.instances.append(self)

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return False


def web_args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=str(tmp_path),
        har_file=None,
        output_folder="downloads",
        state_file="gosync_state.json",
        batch_max_bytes="auto",
    )


def reset_web_state(monkeypatch) -> None:
    FakeThread.instances = []
    RECENT_EVENTS.clear()
    monkeypatch.setattr(web.threading, "Thread", FakeThread)
    monkeypatch.setattr(web, "PROGRESS", ProgressState())
    monkeypatch.setattr(web, "JOB_THREAD", None)
    monkeypatch.setattr(web, "SIDECAR_THREAD", None)
    monkeypatch.setattr(web, "TELEMETRY_THREAD", None)
    monkeypatch.setattr(web, "MERGE_THREAD", None)
    monkeypatch.setattr(web, "RESUME_CACHE", {})
    monkeypatch.setattr(web, "MEDIA_ID_CACHE", {})


def test_start_uses_selected_media_keys_and_files_per_batch(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post(
        "/start",
        data={
            "har_file": "gopro.com.har",
            "selected_media_keys": [
                "ABCDEFGHIJKLM_GX010001.MP4",
                "NOPQRSTUVWXYZ_GX010002.JPG",
            ],
            "files_per_batch": "2",
            "create_xmp_sidecars": "on",
        },
    )

    assert response.status_code == 302
    download_thread = next(
        thread
        for thread in FakeThread.instances
        if thread.target.__name__ == "run_download_job"
    )

    # XMP/telemetry generation now happens inside run_download_job itself
    # (sequenced before the media download), not as a separate thread --
    # see test_runtime.py for coverage of that internal ordering.
    assert not any(
        thread.target.__name__ == "run_sidecar_job" for thread in FakeThread.instances
    )
    assert download_thread.started
    assert download_thread.args[3] == {
        "ABCDEFGHIJKLM_GX010001.MP4",
        "NOPQRSTUVWXYZ_GX010002.JPG",
    }
    assert download_thread.args[4] == 2
    assert isinstance(download_thread.args[5], str)
    assert download_thread.args[5]


def test_start_skips_sidecar_thread_when_checkbox_unchecked(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post(
        "/start",
        data={
            "har_file": "gopro.com.har",
            "selected_media_mode": "all_pending",
        },
    )

    assert response.status_code == 302
    assert not any(
        thread.target.__name__ == "run_sidecar_job" for thread in FakeThread.instances
    )
    assert any(
        thread.target.__name__ == "run_download_job" for thread in FakeThread.instances
    )
    assert web.PROGRESS.snapshot()["sidecar_message"] == (
        "XMP sidecar generation disabled."
    )


def test_update_sidecars_starts_sidecar_and_telemetry_threads_forced(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    downloaded_path = tmp_path / "downloads" / "mp4" / "GX010001.MP4"
    downloaded_path.parent.mkdir(parents=True)
    # GX010001.MP4's declared file_size in the sample HAR is 100 bytes; sync
    # only marks a file downloaded when the on-disk size matches.
    downloaded_path.write_text("f" * 100, encoding="utf-8")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post("/update-sidecars", data={})

    assert response.status_code == 302
    # Telemetry (forced refetch) and XMP generation run sequentially inside
    # one thread now, not two racing ones -- see run_metadata_update_job.
    update_thread = next(
        thread
        for thread in FakeThread.instances
        if thread.target.__name__ == "run_metadata_update_job"
    )
    assert update_thread.started
    # (headers, media_items, output_dir, har_path, state_file, progress, job_id)
    # Only the already-downloaded item is included, not all 3 in the HAR.
    assert [item.filename for item in update_thread.args[1]] == ["GX010001.MP4"]


def test_update_sidecars_rejects_when_nothing_downloaded_yet(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post("/update-sidecars", data={})

    assert response.status_code == 302
    assert FakeThread.instances == []
    assert web.PROGRESS.message == "No downloaded media to update sidecars for"


def test_update_sidecars_rejects_when_no_auth_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post("/update-sidecars", data={})

    assert response.status_code == 302
    assert FakeThread.instances == []
    assert web.PROGRESS.message == "No HAR file or API token configured"


def test_update_sidecars_rejects_while_job_running(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    class RunningThread:
        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(web, "JOB_THREAD", RunningThread())

    response = app.test_client().post("/update-sidecars", data={})

    assert response.status_code == 302
    assert FakeThread.instances == []
    assert web.PROGRESS.message == "A job is already running"


def test_start_rejects_empty_media_selection(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post(
        "/start",
        data={"har_file": "gopro.com.har", "files_per_batch": "2"},
    )

    assert response.status_code == 302
    assert FakeThread.instances == []
    assert web.PROGRESS.message == "No pending media selected"
    assert web.PROGRESS.snapshot()["events"][-1]["event"] == "media.selection.empty"


def test_start_accepts_compact_all_pending_selection(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post(
        "/start",
        data={
            "har_file": "gopro.com.har",
            "selected_media_mode": "all_pending",
        },
        headers={"X-Requested-With": "fetch"},
    )

    assert response.status_code == 204
    download_thread = next(
        thread
        for thread in FakeThread.instances
        if thread.target.__name__ == "run_download_job"
    )
    assert download_thread.args[3] == {
        "ABCDEFGHIJKLM_GX010001.MP4",
        "NOPQRSTUVWXYZ_GX010002.JPG",
        "UNNAMEDMEDIA1_unnamed_1.MP4",
    }


def test_start_fetch_request_updates_without_redirect(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post(
        "/start",
        data={
            "har_file": "gopro.com.har",
            "selected_media_keys": ["ABCDEFGHIJKLM_GX010001.MP4"],
        },
        headers={"X-Requested-With": "fetch"},
    )

    assert response.status_code == 204
    assert response.location is None
    assert any(
        thread.target.__name__ == "run_download_job"
        for thread in FakeThread.instances
    )
    snapshot = web.PROGRESS.snapshot()
    assert snapshot["status"] == "running"
    assert snapshot["state_label"] == "Starting"
    assert snapshot["message"] == "Preparing the selected download job."
    assert snapshot["total_ids"] == 3


def test_stop_fetch_request_updates_without_redirect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post(
        "/stop",
        headers={"X-Requested-With": "fetch"},
    )

    assert response.status_code == 204
    assert response.location is None


def test_sidecars_endpoint_includes_media_file_size(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().get("/sidecars")

    assert response.status_code == 200
    items = response.get_json()["items"]
    assert items[0]["filename"] == "GX010001.MP4"
    assert items[0]["file_size"] == 100


def test_status_reconciles_durable_media_counts(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))
    (tmp_path / "gosync_state.json").write_text(
        json.dumps(
            {
                "media": {
                    "one": {"key": "one", "download_status": "downloaded"},
                    "two": {"key": "two", "download_status": "pending"},
                }
            }
        ),
        encoding="utf-8",
    )
    response = app.test_client().get("/status")

    assert response.status_code == 200
    snapshot = response.get_json()
    assert snapshot["completed_ids"] == 1
    assert snapshot["total_ids"] == 2
    assert snapshot["overall_percent"] == 50.0


def test_status_reconciles_counts_for_env_supplied_api_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    args = web_args(tmp_path)
    args.auth_token = "tok123"
    args.user_id = "user123"
    app = web.create_app(args)
    (tmp_path / "gosync_state.json").write_text(
        json.dumps(
            {
                "media": {
                    "one": {"key": "one", "download_status": "downloaded"},
                    "two": {"key": "two", "download_status": "pending"},
                }
            }
        ),
        encoding="utf-8",
    )

    response = app.test_client().get("/status")

    assert response.status_code == 200
    snapshot = response.get_json()
    assert snapshot["completed_ids"] == 1
    assert snapshot["total_ids"] == 2
    assert snapshot["capabilities"]["can_start"] is True


def test_status_does_not_reconcile_counts_while_running(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))
    (tmp_path / "gosync_state.json").write_text(
        json.dumps(
            {
                "media": {
                    "one": {"key": "one", "download_status": "downloaded"},
                    "two": {"key": "two", "download_status": "pending"},
                }
            }
        ),
        encoding="utf-8",
    )
    web.PROGRESS.update(
        status="running",
        total_ids=10,
        completed_ids=5,
        pending_ids=5,
    )

    response = app.test_client().get("/status")

    assert response.status_code == 200
    snapshot = response.get_json()
    assert snapshot["total_ids"] == 10
    assert snapshot["completed_ids"] == 5


def test_favicon_endpoint_serves_icon(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().get("/favicon.ico")

    assert response.status_code == 200
    assert response.mimetype == "image/vnd.microsoft.icon"
    assert response.data.startswith(b"\x00\x00\x01\x00")


def test_polling_endpoints_do_not_reprepare_manifest_state(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    def fail_prepare(*_args, **_kwargs):
        raise AssertionError("poll endpoint should use cached state")

    monkeypatch.setattr(web, "prepare_paths_manifest_state", fail_prepare)

    client = app.test_client()
    assert client.get("/status").status_code == 200

    sidecars_response = client.get("/sidecars")
    assert sidecars_response.status_code == 200
    assert sidecars_response.get_json()["items"][0]["filename"] == "GX010001.MP4"


def test_upload_does_not_mutate_shared_args_har_file(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    args = web_args(tmp_path)
    app = web.create_app(args)
    har_path = tmp_path / "source.har"
    write_sample_har(har_path)

    response = app.test_client().post(
        "/upload",
        data={"har_file": (BytesIO(har_path.read_bytes()), "uploaded.har")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert args.har_file is None


def test_settings_infers_api_token_method_when_token_and_user_id_filled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    app = web.create_app(web_args(tmp_path))
    client = app.test_client()

    response = client.post(
        "/settings",
        data={
            "auth_token": "abc123",
            "user_id": "user-1",
            "download_telemetry": "on",
        },
    )

    assert response.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert not start_button_disabled(page)
    assert "Saved (blank keeps it)" in page


def test_settings_falls_back_to_har_when_user_id_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    app = web.create_app(web_args(tmp_path))
    client = app.test_client()

    response = client.post("/settings", data={"auth_token": "abc123"})

    assert response.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert start_button_disabled(page)


def test_settings_blank_token_keeps_previously_saved_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    app = web.create_app(web_args(tmp_path))
    client = app.test_client()

    client.post("/settings", data={"auth_token": "abc123", "user_id": "user-1"})
    response = client.post(
        "/settings",
        data={"user_id": "user-1", "download_telemetry": "on"},
    )

    assert response.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert not start_button_disabled(page)


def test_start_without_har_or_token_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    app = web.create_app(web_args(tmp_path))
    client = app.test_client()

    response = client.post("/start", data={})

    assert response.status_code == 302
    assert FakeThread.instances == []
    assert web.PROGRESS.message == "No HAR file selected"


def test_start_with_token_and_user_id_uses_api_token_mode(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    app = web.create_app(web_args(tmp_path))
    client = app.test_client()

    item = make_media_item("A", "clip.mp4", 10)
    manifest = MediaManifest(
        media=[item], duplicates=[], matching_entries=1, media_responses=[]
    )

    def fake_prepare(args, har_file=None, progress=None):
        assert args.auth_method == AUTH_METHOD_API_TOKEN
        paths = RuntimePaths(
            data_dir=tmp_path,
            har_path=None,
            output_dir=tmp_path / "downloads",
            state_file=tmp_path / "state.json",
            manifest_file=tmp_path / "manifest.json",
            media_dump_file=tmp_path / "media_search.json",
            auth=AuthConfig(
                method=AUTH_METHOD_API_TOKEN, auth_token="abc123", user_id="user-1"
            ),
        )
        return PreparedManifestState(
            paths=paths, manifest=manifest, state={"media": {}}, sync_changes=[]
        )

    monkeypatch.setattr(web, "prepare_runtime_manifest_state", fake_prepare)

    response = client.post(
        "/start",
        data={
            "auth_token": "abc123",
            "user_id": "user-1",
            "selected_media_keys": [item.key],
        },
    )

    assert response.status_code == 302
    assert any(
        thread.target.__name__ == "run_download_job" for thread in FakeThread.instances
    )


def test_start_handles_manifest_load_failure_gracefully(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    def fake_prepare(args, har_file=None, progress=None):
        raise ValueError("boom")

    monkeypatch.setattr(web, "prepare_runtime_manifest_state", fake_prepare)

    response = app.test_client().post(
        "/start",
        data={"har_file": "gopro.com.har", "files_per_batch": "2"},
    )

    assert response.status_code == 302
    assert FakeThread.instances == []
    assert web.PROGRESS.message == "Could not load media for download"
    event = web.PROGRESS.snapshot()["events"][-1]
    assert event["event"] == "error.validation"
    assert event["error_message"] == "boom"


def test_current_events_endpoint_filters_to_current_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    web.PROGRESS.update(job_id="current")
    app = web.create_app(web_args(tmp_path))

    log_event("download.phase.started", "Old run", run_id="old")
    log_event("download.phase.started", "Current run", run_id="current")
    log_event("app.ready", "Startup event")

    response = app.test_client().get("/api/runs/current/events")

    assert response.status_code == 200
    assert [event["message"] for event in response.get_json()["items"]] == [
        "Current run"
    ]


def chaptered_media_records() -> list[dict]:
    return [
        {
            "id": "MERGEDOK00001",
            "filename": "GX010301.MP4",
            "file_extension": "MP4",
            "file_size": 18,
            "content_type": "video/mp4",
            "item_count": 2,
        },
        {
            "id": "SIZEMISMATCH1",
            "filename": "GX010302.MP4",
            "file_extension": "MP4",
            "file_size": 18,
            "content_type": "video/mp4",
            "item_count": 2,
        },
        {
            "id": "CHAPTSREADY01",
            "filename": "GX010303.MP4",
            "file_extension": "MP4",
            "file_size": 18,
            "content_type": "video/mp4",
            "item_count": 2,
        },
        {
            "id": "CHAPTSPARTIAL",
            "filename": "GX010304.MP4",
            "file_extension": "MP4",
            "file_size": 27,
            "content_type": "video/mp4",
            "item_count": 3,
        },
        {
            "id": "CHAPTSMISSING",
            "filename": "GX010305.MP4",
            "file_extension": "MP4",
            "file_size": 18,
            "content_type": "video/mp4",
            "item_count": 2,
        },
    ]


def test_sidecars_endpoint_computes_merge_status_across_all_states(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    monkeypatch.setattr("gosync.chapters.PER_CHAPTER_OVERHEAD_BYTES", 1)
    monkeypatch.setattr("gosync.chapters.MIN_TOLERANCE_BYTES", 1)
    monkeypatch.setattr("gosync.chapters.MAX_TOLERANCE_BYTES", 5)
    write_sample_har(tmp_path / "gopro.com.har", media=chaptered_media_records())
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()

    # merged: target size matches the local chapter sum exactly.
    (output_dir / "GX010301.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020301.MP4").write_text("chapter-2", encoding="utf-8")
    media_download_path(output_dir, "GX010301.MP4").parent.mkdir(parents=True)
    media_download_path(output_dir, "GX010301.MP4").write_text(
        "chapter-1chapter-2", encoding="utf-8"
    )

    # size_mismatch: target exists but is far off the local chapter sum.
    (output_dir / "GX010302.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020302.MP4").write_text("chapter-2", encoding="utf-8")
    media_download_path(output_dir, "GX010302.MP4").parent.mkdir(
        parents=True, exist_ok=True
    )
    media_download_path(output_dir, "GX010302.MP4").write_text("X", encoding="utf-8")

    # chapters_ready: exact item_count chapters present, no merged target.
    (output_dir / "GX010303.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020303.MP4").write_text("chapter-2", encoding="utf-8")

    # chapters_partial: item_count=3 but only 2 chapters present.
    (output_dir / "GX010304.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020304.MP4").write_text("chapter-2", encoding="utf-8")

    # chapters_missing: nothing on disk for GX010305.MP4's group at all.

    app = web.create_app(web_args(tmp_path))
    response = app.test_client().get("/sidecars")

    assert response.status_code == 200
    rows_by_filename = {
        item["filename"]: item for item in response.get_json()["items"]
    }
    assert rows_by_filename["GX010301.MP4"]["merge_status"] == "merged"
    assert rows_by_filename["GX010301.MP4"]["remerge_eligible"] is True
    assert rows_by_filename["GX010301.MP4"]["merge_target_path"] == str(
        media_download_path(output_dir, "GX010301.MP4")
    )
    assert rows_by_filename["GX010302.MP4"]["merge_status"] == "size_mismatch"
    assert rows_by_filename["GX010302.MP4"]["remerge_eligible"] is True
    assert rows_by_filename["GX010303.MP4"]["merge_status"] == "chapters_ready"
    assert rows_by_filename["GX010303.MP4"]["remerge_eligible"] is True
    assert rows_by_filename["GX010304.MP4"]["merge_status"] == "chapters_partial"
    assert rows_by_filename["GX010304.MP4"]["remerge_eligible"] is False
    assert rows_by_filename["GX010305.MP4"]["merge_status"] == "chapters_missing"
    assert rows_by_filename["GX010305.MP4"]["remerge_eligible"] is False


def test_sidecars_endpoint_reports_actual_size_for_merged_chapters(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    """A chaptered item's state record can carry a stale file_size (e.g. the
    manifest's pre-merge chapter-sum estimate) even once it's genuinely
    merged on disk. When merge_status confirms the on-disk file is a valid
    merge, the row should report that file's real size, not whatever is
    still recorded in gosync_state.json -- and should also surface the
    manifest's originally reported size alongside it for comparison."""
    reset_web_state(monkeypatch)
    monkeypatch.setattr("gosync.chapters.PER_CHAPTER_OVERHEAD_BYTES", 1)
    monkeypatch.setattr("gosync.chapters.MIN_TOLERANCE_BYTES", 1)
    monkeypatch.setattr("gosync.chapters.MAX_TOLERANCE_BYTES", 5)
    stale_records = [
        {
            "id": "MERGEDOK00001",
            "filename": "GX010301.MP4",
            "file_extension": "MP4",
            "file_size": 999999,
            "content_type": "video/mp4",
            "item_count": 2,
        }
    ]
    write_sample_har(tmp_path / "gopro.com.har", media=stale_records)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "GX010301.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020301.MP4").write_text("chapter-2", encoding="utf-8")
    merged_path = media_download_path(output_dir, "GX010301.MP4")
    merged_path.parent.mkdir(parents=True)
    merged_path.write_text("chapter-1chapter-2", encoding="utf-8")

    app = web.create_app(web_args(tmp_path))
    response = app.test_client().get("/sidecars")

    assert response.status_code == 200
    row = response.get_json()["items"][0]
    assert row["merge_status"] == "merged"
    assert row["file_size"] == merged_path.stat().st_size
    assert row["manifest_file_size"] == 999999


def test_sidecars_endpoint_omits_merge_status_for_non_chaptered_items(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har")
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().get("/sidecars")

    assert response.status_code == 200
    items = response.get_json()["items"]
    assert items[0]["merge_status"] is None
    assert items[0]["remerge_eligible"] is False


def test_rerun_merge_starts_thread_with_eligible_items_only(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    monkeypatch.setattr("gosync.chapters.PER_CHAPTER_OVERHEAD_BYTES", 1)
    monkeypatch.setattr("gosync.chapters.MIN_TOLERANCE_BYTES", 1)
    monkeypatch.setattr("gosync.chapters.MAX_TOLERANCE_BYTES", 5)
    write_sample_har(tmp_path / "gopro.com.har", media=chaptered_media_records())
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()

    # GX010303.MP4: chapters_ready -- eligible.
    (output_dir / "GX010303.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020303.MP4").write_text("chapter-2", encoding="utf-8")
    # GX010304.MP4 (item_count=3): only 2 of 3 chapters -- not eligible.
    (output_dir / "GX010304.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020304.MP4").write_text("chapter-2", encoding="utf-8")
    # GX010305.MP4: nothing on disk -- not eligible.

    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post(
        "/rerun-merge",
        data={
            "selected_merge_keys": [
                "CHAPTSREADY01_GX010303.MP4",
                "CHAPTSPARTIAL_GX010304.MP4",
                "CHAPTSMISSING_GX010305.MP4",
            ]
        },
    )

    assert response.status_code == 302
    merge_thread = next(
        thread
        for thread in FakeThread.instances
        if thread.target.__name__ == "run_remerge_job"
    )
    assert merge_thread.started
    # (media_items, output_dir, state_file, progress, job_id)
    assert [item.filename for item in merge_thread.args[0]] == ["GX010303.MP4"]

    events = [event["event"] for event in RECENT_EVENTS]
    assert "media.scan.started" in events


def test_rerun_merge_rejects_when_nothing_eligible_selected(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har", media=chaptered_media_records())
    app = web.create_app(web_args(tmp_path))

    response = app.test_client().post("/rerun-merge", data={})

    assert response.status_code == 302
    assert FakeThread.instances == []
    assert web.PROGRESS.message == "No eligible chaptered media selected for re-merge"


def test_rerun_merge_rejects_while_job_running(
    tmp_path: Path,
    write_sample_har,
    monkeypatch,
) -> None:
    reset_web_state(monkeypatch)
    write_sample_har(tmp_path / "gopro.com.har", media=chaptered_media_records())
    app = web.create_app(web_args(tmp_path))

    class RunningThread:
        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(web, "MERGE_THREAD", RunningThread())

    response = app.test_client().post(
        "/rerun-merge",
        data={"selected_merge_keys": ["CHAPTSREADY01_GX010303.MP4"]},
    )

    assert response.status_code == 302
    assert FakeThread.instances == []
    assert web.PROGRESS.message == "A job is already running"
