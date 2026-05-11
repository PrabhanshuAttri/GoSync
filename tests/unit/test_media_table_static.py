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
