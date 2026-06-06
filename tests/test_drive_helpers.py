"""No-network unit tests for the pure helpers in drive.py and config.py."""

from __future__ import annotations

import pytest

from drive_session_mcp import config, drive, errors


# --------------------------------------------------------------------------- #
# error hierarchy (CLI relies on these being DriveError subclasses)
# --------------------------------------------------------------------------- #
def test_fetch_errors_are_drive_errors():
    assert issubclass(errors.FileNotFoundError, errors.DriveError)
    assert issubclass(errors.AccessDeniedError, errors.DriveError)
    assert issubclass(errors.DriveError, RuntimeError)


# --------------------------------------------------------------------------- #
# build_query / search_url
# --------------------------------------------------------------------------- #
def test_build_query_text_only():
    assert drive.build_query("quarterly report") == "quarterly report"


def test_build_query_with_filters():
    q = drive.build_query("report", {"type": "document", "owner": "me"})
    assert q == "report type:document owner:me"


def test_build_query_skips_empty_filters():
    q = drive.build_query("report", {"type": "pdf", "owner": None, "x": ""})
    assert q == "report type:pdf"


def test_search_url_encodes():
    url = drive.search_url("q1 report", {"type": "document"})
    assert url.startswith("https://drive.google.com/drive/search?q=")
    assert " " not in url
    assert "type%3Adocument" in url


# --------------------------------------------------------------------------- #
# SearchItems protobuf-JSON parsing + normalization
# --------------------------------------------------------------------------- #
# A row is positional: [id, parents, name, mime, ..., modified_ms@10, ...].
def _row(id_, name, mime, *, parents=None, modified_ms=None):
    r = [None] * 11
    r[0] = id_
    r[1] = parents
    r[2] = name
    r[3] = mime
    r[10] = modified_ms
    return r


VALID_ID = "1e8k-XPS3Pl7ry-YReOdjvyjj-mlAAUwUMT3Bq4pWgvA"


def test_parse_protojson_strips_xssi_prefix():
    assert drive.parse_protojson(b")]}'\n[[1,2]]") == [[1, 2]]
    assert drive.parse_protojson(b")]}'[3]") == [3]
    assert drive.parse_protojson(b"[4]") == [4]


def test_parse_protojson_bad_returns_none():
    assert drive.parse_protojson(b"not json") is None


def test_find_rows_extracts_nested_items():
    data = [[[_row(VALID_ID, "Doc", "application/vnd.google-apps.document")]], 0]
    out = []
    drive.find_rows(data, out)
    assert len(out) == 1
    assert out[0][0] == VALID_ID


def test_find_rows_ignores_non_rows():
    # Nested arrays with no id-like [0]/name/mime triple must not be matched.
    data = [[None, "x", 5], [[[None] * 12]], "short"]
    out = []
    drive.find_rows(data, out)
    assert out == []


def test_normalize_document_gets_export_hint_and_iso_date():
    row = _row(
        VALID_ID,
        "Plan",
        "application/vnd.google-apps.document",
        modified_ms=1780671474000,
    )
    out = drive.normalize_row(row)
    assert out["id"] == VALID_ID
    assert out["name"] == "Plan"
    assert out["export_format"] == "pdf"
    assert out["owner"] is None
    assert out["modified"].startswith("2026-")  # epoch ms -> ISO


def test_normalize_binary_has_no_export_hint():
    row = _row(VALID_ID, "data.csv", "text/csv")
    out = drive.normalize_row(row)
    assert out["export_format"] is None
    assert out["modified"] is None


def test_normalize_folder_parent():
    row = _row(VALID_ID, "Sub", "application/vnd.google-apps.folder", parents=["PARENT_ID_123"])
    assert drive.normalize_row(row)["folder"] == "PARENT_ID_123"


# --------------------------------------------------------------------------- #
# _resolve_export
# --------------------------------------------------------------------------- #
def test_resolve_export_native_default_format():
    is_export, fmt, tmpl = drive._resolve_export(
        "application/vnd.google-apps.spreadsheet", None
    )
    assert is_export is True
    assert fmt == "xlsx"
    assert "spreadsheets" in tmpl


def test_resolve_export_native_override_format():
    is_export, fmt, _ = drive._resolve_export(
        "application/vnd.google-apps.document", "docx"
    )
    assert (is_export, fmt) == (True, "docx")


def test_resolve_export_binary():
    is_export, fmt, tmpl = drive._resolve_export("application/pdf", None)
    assert is_export is False
    assert fmt is None
    assert tmpl is None


def test_resolve_export_unknown_mime_but_format_requested():
    is_export, fmt, tmpl = drive._resolve_export(None, "pdf")
    assert (is_export, fmt) == (True, "pdf")
    assert "document" in tmpl  # falls back to the document export template


# --------------------------------------------------------------------------- #
# looks_like_login_page
# --------------------------------------------------------------------------- #
def test_login_page_detected():
    body = b"<html><head><title>Sign in - Google Accounts</title>"
    assert drive.looks_like_login_page(body, {"content-type": "text/html"}) is True


def test_login_page_not_for_binary():
    assert drive.looks_like_login_page(b"%PDF-1.7...", {"content-type": "application/pdf"}) is False


# --------------------------------------------------------------------------- #
# _filename_from_headers
# --------------------------------------------------------------------------- #
def test_filename_from_content_disposition():
    name = drive._filename_from_headers(
        {"content-disposition": 'attachment; filename="Report Q1.pdf"'}, "fallback"
    )
    assert name == "Report Q1.pdf"


def test_filename_fallback_when_absent():
    assert drive._filename_from_headers({}, "abc.pdf") == "abc.pdf"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_PROFILE, str(tmp_path / "prof"))
    monkeypatch.setenv(config.ENV_DOWNLOAD_DIR, str(tmp_path / "dl"))
    assert config.profile_dir() == tmp_path / "prof"
    assert config.download_dir() == tmp_path / "dl"


def test_config_defaults_exist(monkeypatch):
    monkeypatch.delenv(config.ENV_PROFILE, raising=False)
    monkeypatch.delenv(config.ENV_DOWNLOAD_DIR, raising=False)
    assert config.profile_dir().name == "profile"
    assert config.download_dir().name == "drive-session-mcp"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
