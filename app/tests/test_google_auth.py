from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.google import auth as auth_module
from app.google.auth import (
    ACCESS_TYPE,
    PROMPT,
    SCOPES,
    YOUTUBE_SCOPE,
    AuthError,
    AuthErrorReason,
    load_credentials,
    token_file_for,
)

TOKEN_JSON: str = json.dumps({"token": "x", "refresh_token": "y"})
LOGIN_HINT: str = "owner@gmail.com"


class _FakeCredentials:
    def __init__(self, *, valid: bool = True, refresh_token: str | None = "y") -> None:
        self.valid: bool = valid
        self.expired: bool = not valid
        self.refresh_token: str | None = refresh_token
        self.refreshed: bool = False

    def to_json(self) -> str:
        return TOKEN_JSON

    def refresh(self, request: Any) -> None:
        self.refreshed = True
        self.valid = True


class _FakeFlow:
    """Подмена InstalledAppFlow: запоминает, с какими параметрами открывали браузер."""

    last_kwargs: dict[str, Any] = {}
    credentials: _FakeCredentials | None = None

    @classmethod
    def from_client_secrets_file(cls, path: str, scopes: list[str]) -> _FakeFlow:
        cls.last_kwargs = {"path": path, "scopes": scopes}
        return cls()

    def run_local_server(self, **kwargs: Any) -> _FakeCredentials:
        _FakeFlow.last_kwargs.update(kwargs)
        credentials: _FakeCredentials = _FakeFlow.credentials or _FakeCredentials()
        return credentials


@pytest.fixture
def client_secret(tmp_path: Path) -> Path:
    path: Path = tmp_path / "client_secret.json"
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch) -> type[_FakeFlow]:
    _FakeFlow.last_kwargs = {}
    _FakeFlow.credentials = None
    monkeypatch.setattr(auth_module, "InstalledAppFlow", _FakeFlow)
    return _FakeFlow


def test_scope_is_youtube_only() -> None:
    assert SCOPES == (YOUTUBE_SCOPE,)
    assert YOUTUBE_SCOPE == "https://www.googleapis.com/auth/youtube"


def test_token_file_name_is_the_account_name(tmp_path: Path) -> None:
    assert token_file_for(tmp_path, "Osvald.X") == tmp_path / "Osvald.X.token.json"
    assert token_file_for(tmp_path, "Канал UA") == tmp_path / "Канал UA.token.json"


def test_on_login_is_called_right_before_the_browser(
    client_secret: Path,
    tmp_path: Path,
    flow: type[_FakeFlow],
) -> None:
    seen: list[dict[str, Any]] = []
    load_credentials(client_secret, tmp_path / "Osvald.X.token.json", LOGIN_HINT, on_login=lambda: seen.append(dict(flow.last_kwargs)))
    assert seen == [{}]                          # браузер ещё не открывался
    assert flow.last_kwargs["port"] == 0          # а после вызова — открылся


def test_missing_client_secret_raises(tmp_path: Path) -> None:
    with pytest.raises(AuthError) as raised:
        load_credentials(tmp_path / "nope.json", tmp_path / "token.json", LOGIN_HINT)
    assert raised.value.reason is AuthErrorReason.CLIENT_SECRET_MISSING


def test_flow_runs_on_free_port_and_asks_for_refresh_token(
    client_secret: Path,
    tmp_path: Path,
    flow: type[_FakeFlow],
) -> None:
    token_file: Path = tmp_path / "yt_ua.token.json"
    load_credentials(client_secret, token_file, LOGIN_HINT)
    assert flow.last_kwargs["port"] == 0
    assert flow.last_kwargs["access_type"] == ACCESS_TYPE
    assert flow.last_kwargs["prompt"] == PROMPT
    assert flow.last_kwargs["login_hint"] == LOGIN_HINT     # браузер сразу предлагает аккаунт канала
    assert token_file.read_text(encoding="utf-8") == TOKEN_JSON


def test_valid_token_is_reused_without_browser(
    client_secret: Path,
    tmp_path: Path,
    flow: type[_FakeFlow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file: Path = tmp_path / "yt_ua.token.json"
    token_file.write_text(TOKEN_JSON, encoding="utf-8")
    monkeypatch.setattr(
        auth_module.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, path, scopes: _FakeCredentials()),
    )
    logins: list[str] = []
    load_credentials(client_secret, token_file, LOGIN_HINT, on_login=lambda: logins.append("login"))
    assert flow.last_kwargs == {}
    assert logins == []                           # токен живой — входа нет, владельцу печатать нечего


def test_expired_token_is_refreshed_silently(
    client_secret: Path,
    tmp_path: Path,
    flow: type[_FakeFlow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file: Path = tmp_path / "yt_ua.token.json"
    token_file.write_text("stale", encoding="utf-8")
    stale: _FakeCredentials = _FakeCredentials(valid=False)
    monkeypatch.setattr(
        auth_module.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, path, scopes: stale),
    )
    load_credentials(client_secret, token_file, LOGIN_HINT)
    assert stale.refreshed is True
    assert flow.last_kwargs == {}
    assert token_file.read_text(encoding="utf-8") == TOKEN_JSON


def test_force_reauth_drops_token_and_opens_browser(
    client_secret: Path,
    tmp_path: Path,
    flow: type[_FakeFlow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file: Path = tmp_path / "yt_ua.token.json"
    token_file.write_text("old token", encoding="utf-8")
    monkeypatch.setattr(
        auth_module.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, path, scopes: pytest.fail("токен не должен читаться при force_reauth")),
    )
    load_credentials(client_secret, token_file, LOGIN_HINT, force_reauth=True)
    assert flow.last_kwargs["port"] == 0
    assert token_file.read_text(encoding="utf-8") == TOKEN_JSON


def test_unreadable_token_is_reported(
    client_secret: Path,
    tmp_path: Path,
    flow: type[_FakeFlow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file: Path = tmp_path / "yt_ua.token.json"
    token_file.write_text("{broken", encoding="utf-8")

    def _raise(cls: Any, path: str, scopes: list[str]) -> None:
        raise ValueError("broken token")

    monkeypatch.setattr(auth_module.Credentials, "from_authorized_user_file", classmethod(_raise))
    with pytest.raises(AuthError) as raised:
        load_credentials(client_secret, token_file, LOGIN_HINT)
    assert raised.value.reason is AuthErrorReason.TOKEN_UNREADABLE


def test_revoked_token_falls_back_to_browser(
    client_secret: Path,
    tmp_path: Path,
    flow: type[_FakeFlow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file: Path = tmp_path / "yt_ua.token.json"
    token_file.write_text("revoked", encoding="utf-8")
    revoked: _FakeCredentials = _FakeCredentials(valid=False)

    def _raise(request: Any) -> None:
        raise auth_module.RefreshError("invalid_grant")

    revoked.refresh = _raise  # type: ignore[method-assign]
    monkeypatch.setattr(
        auth_module.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, path, scopes: revoked),
    )
    load_credentials(client_secret, token_file, LOGIN_HINT)
    assert flow.last_kwargs["port"] == 0


def test_transport_failure_on_refresh_is_reported(
    client_secret: Path,
    tmp_path: Path,
    flow: type[_FakeFlow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file: Path = tmp_path / "yt_ua.token.json"
    token_file.write_text("stale", encoding="utf-8")
    stale: _FakeCredentials = _FakeCredentials(valid=False)

    def _raise(request: Any) -> None:
        raise auth_module.TransportError("no network")

    stale.refresh = _raise  # type: ignore[method-assign]
    monkeypatch.setattr(
        auth_module.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, path, scopes: stale),
    )
    with pytest.raises(AuthError) as raised:
        load_credentials(client_secret, token_file, LOGIN_HINT)
    assert raised.value.reason is AuthErrorReason.REFRESH_FAILED
