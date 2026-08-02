import argparse
from dataclasses import dataclass
from pathlib import Path

from gosync.constants import AUTH_METHOD_API_TOKEN, AUTH_METHOD_HAR, DEFAULT_HEADERS
from gosync.downloader import extract_browser_headers, resolve_har_file
from gosync.progress import ProgressState


@dataclass(frozen=True)
class AuthConfig:
    method: str
    har_path: Path | None = None
    auth_token: str | None = None
    user_id: str | None = None


def resolve_auth_config(
    args: argparse.Namespace,
    data_dir: Path,
    har_file: str | None = None,
) -> AuthConfig:
    method = getattr(args, "auth_method", None) or AUTH_METHOD_HAR

    if method == AUTH_METHOD_API_TOKEN:
        auth_token = (getattr(args, "auth_token", None) or "").strip()
        if not auth_token:
            raise ValueError(
                "AUTH_METHOD is api_token but no AUTH_TOKEN was provided."
            )
        user_id = (getattr(args, "user_id", None) or "").strip() or None
        return AuthConfig(
            method=AUTH_METHOD_API_TOKEN,
            auth_token=auth_token,
            user_id=user_id,
        )

    if method != AUTH_METHOD_HAR:
        raise ValueError(f"Unknown AUTH_METHOD: {method!r}")

    har_path = resolve_har_file(data_dir, har_file or getattr(args, "har_file", None))
    return AuthConfig(method=AUTH_METHOD_HAR, har_path=har_path)


def normalize_bearer_token(token: str) -> str:
    token = token.strip()
    return token if token.startswith("Bearer ") else f"Bearer {token}"


def build_api_token_headers(auth_token: str) -> dict[str, str]:
    """Headers for the zip-export download, built from a directly-provided
    bearer token instead of one copied out of a HAR capture."""
    return {
        **DEFAULT_HEADERS,
        "Authorization": normalize_bearer_token(auth_token),
    }


def build_headers(
    auth: AuthConfig,
    progress: ProgressState | None = None,
    job_id: str | None = None,
) -> dict[str, str]:
    if auth.method == AUTH_METHOD_API_TOKEN:
        return build_api_token_headers(auth.auth_token)
    return extract_browser_headers(auth.har_path, progress, job_id)
