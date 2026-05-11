from pathlib import Path
from types import SimpleNamespace

from gosync import web
from gosync.progress import ProgressState


class FakeThread:
    instances = []

    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
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
        sidecar_folder="sidecars",
        state_file="gosync_state.json",
        batch_max_bytes="auto",
    )


def reset_web_state(monkeypatch) -> None:
    FakeThread.instances = []
    monkeypatch.setattr(web.threading, "Thread", FakeThread)
    monkeypatch.setattr(web, "PROGRESS", ProgressState())
    monkeypatch.setattr(web, "JOB_THREAD", None)
    monkeypatch.setattr(web, "SIDECAR_THREAD", None)
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
        },
    )

    assert response.status_code == 302
    download_thread = next(
        thread
        for thread in FakeThread.instances
        if thread.target.__name__ == "run_download_job"
    )
    sidecar_thread = next(
        thread
        for thread in FakeThread.instances
        if thread.target.__name__ == "run_sidecar_job"
    )

    assert download_thread.started
    assert download_thread.args[3] == {
        "ABCDEFGHIJKLM_GX010001.MP4",
        "NOPQRSTUVWXYZ_GX010002.JPG",
    }
    assert download_thread.args[4] == 2
    assert [item.key for item in sidecar_thread.args[3]] == [
        "ABCDEFGHIJKLM_GX010001.MP4",
        "NOPQRSTUVWXYZ_GX010002.JPG",
    ]


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
    assert "Select at least one pending media file" in web.PROGRESS.message


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
