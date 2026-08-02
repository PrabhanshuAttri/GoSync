from pathlib import Path
from types import SimpleNamespace

import pytest

from gosync.auth import (
    AuthConfig,
    build_api_token_headers,
    build_headers,
    normalize_bearer_token,
    resolve_auth_config,
)
from gosync.constants import AUTH_METHOD_API_TOKEN, AUTH_METHOD_HAR


def test_resolve_auth_config_defaults_to_har(tmp_path: Path) -> None:
    (tmp_path / "gopro.com.har").write_text("{}", encoding="utf-8")
    args = SimpleNamespace(har_file=None)

    auth = resolve_auth_config(args, tmp_path)

    assert auth.method == AUTH_METHOD_HAR
    assert auth.har_path == tmp_path / "gopro.com.har"


def test_resolve_auth_config_api_token_requires_token(tmp_path: Path) -> None:
    args = SimpleNamespace(
        auth_method=AUTH_METHOD_API_TOKEN, auth_token="", user_id=None
    )

    with pytest.raises(ValueError, match="no AUTH_TOKEN"):
        resolve_auth_config(args, tmp_path)


def test_resolve_auth_config_api_token_strips_and_carries_user_id(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        auth_method=AUTH_METHOD_API_TOKEN,
        auth_token="  abc123  ",
        user_id="  user-1  ",
    )

    auth = resolve_auth_config(args, tmp_path)

    assert auth == AuthConfig(
        method=AUTH_METHOD_API_TOKEN,
        auth_token="abc123",
        user_id="user-1",
    )


def test_resolve_auth_config_rejects_unknown_method(tmp_path: Path) -> None:
    args = SimpleNamespace(auth_method="carrier_pigeon", har_file=None)

    with pytest.raises(ValueError, match="Unknown AUTH_METHOD"):
        resolve_auth_config(args, tmp_path)


def test_normalize_bearer_token_adds_prefix_once() -> None:
    assert normalize_bearer_token("abc") == "Bearer abc"
    assert normalize_bearer_token("Bearer abc") == "Bearer abc"
    assert normalize_bearer_token("  abc  ") == "Bearer abc"


def test_build_api_token_headers_merges_onto_default_headers() -> None:
    headers = build_api_token_headers("abc123")

    assert headers["Authorization"] == "Bearer abc123"
    assert headers["Origin"] == "https://gopro.com"
    assert "Accept" in headers


def test_build_headers_dispatches_on_method(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        "gosync.auth.extract_browser_headers",
        lambda *args, **kwargs: calls.append(("har", args, kwargs)) or {"h": "har"},
    )

    har_auth = AuthConfig(method=AUTH_METHOD_HAR, har_path=tmp_path / "x.har")
    assert build_headers(har_auth) == {"h": "har"}
    assert calls[0][0] == "har"

    token_auth = AuthConfig(method=AUTH_METHOD_API_TOKEN, auth_token="abc")
    headers = build_headers(token_auth)
    assert headers["Authorization"] == "Bearer abc"
