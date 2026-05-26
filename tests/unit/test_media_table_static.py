from pathlib import Path

MEDIA_TABLE_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gosync"
    / "static"
    / "media_table.js"
)


def test_media_table_skips_redraw_for_unchanged_payloads() -> None:
    script = MEDIA_TABLE_JS.read_text(encoding="utf-8")

    assert "let lastItemsSignature" in script
    assert "const nextSignature = itemsSignature(items);" in script
    assert "if (nextSignature === lastItemsSignature)" in script
    assert "return;" in script


def test_media_table_persists_view_state_across_reloads() -> None:
    script = MEDIA_TABLE_JS.read_text(encoding="utf-8")

    assert "extensionFilter: extensionFilter.value" in script
    assert "tableScrollTop: tableWrap?.scrollTop || 0" in script
    assert "tableScrollLeft: tableWrap?.scrollLeft || 0" in script
    assert (
        "let pendingScrollTop = Number(restoredSettings.tableScrollTop) || 0"
        in script
    )
    assert (
        "let pendingScrollLeft = Number(restoredSettings.tableScrollLeft) || 0"
        in script
    )


def test_media_table_reads_restored_settings_before_using_them() -> None:
    script = MEDIA_TABLE_JS.read_text(encoding="utf-8")

    assert script.index("const restoredSettings = readSettings();") < script.index(
        "let requestedExtensionFilter = restoredSettings.extensionFilter || \"\";"
    )
    assert script.index("const restoredSettings = readSettings();") < script.index(
        "let pendingScrollTop = Number(restoredSettings.tableScrollTop) || 0;"
    )


def test_media_table_checks_and_disables_downloaded_rows() -> None:
    script = MEDIA_TABLE_JS.read_text(encoding="utf-8")

    assert "const isDownloaded = (item) => item.status === \"downloaded\";" in script
    assert (
        "checkbox.checked = downloaded || selectedMediaKeys?.has(item.key) || false;"
        in script
    )
    assert "checkbox.disabled = !selectable || !item.key;" in script
    assert ".filter((item) => !isDownloaded(item) && item.key)" in script


def test_media_table_status_sort_order_prioritizes_active_rows() -> None:
    script = MEDIA_TABLE_JS.read_text(encoding="utf-8")

    downloading_rank = script.index('if (status === "downloading") return 0;')
    pending_rank = script.index('if (status === "pending") return 1;')
    downloaded_rank = script.index('if (status === "downloaded") return 2;')

    assert downloading_rank < pending_rank < downloaded_rank


def test_media_table_compacts_all_pending_start_payloads() -> None:
    script = MEDIA_TABLE_JS.read_text(encoding="utf-8")

    assert 'formData.set("selected_media_mode", "all_pending");' in script
    assert 'formData.delete("selected_media_keys");' in script
    assert 'window.gosyncMediaTable.startFormData(form)' in (
        Path(__file__).resolve().parents[2]
        / "src"
        / "gosync"
        / "templates"
        / "index.html"
    ).read_text(encoding="utf-8")


def test_event_log_uses_grouped_timeline_renderer() -> None:
    template = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "gosync"
        / "templates"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert 'class="event-timeline"' in template
    assert "const eventGroups = [" in template
    assert "event.detail_lines" in template
    assert "event.meta" in template


def test_event_log_compacts_progress_updates_in_ui() -> None:
    template = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "gosync"
        / "templates"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert "const compactUiEvents = (events) => {" in template
    assert 'event.event !== "download.file.progress"' in template
    assert "latestProgressIndexes.get(key) === index" in template
