"""OAuth для YouTube (ТЗ §5.3): один токен на канал, единственный скоуп — youtube.

Перенос ветки `_create_oauth_credentials` из broadcaster/app/google/auth.py:
только OAuth, без service account, Docs, Drive и Sheets. Порт локального сервера — 0
(свободный), иначе занятый порт кладёт авторизацию.
"""
from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Final

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.observability.logging_setup import get_logger

LOGGER = get_logger("auth")

# Единственный источник скоупа в проекте (ТЗ §5.3, инвариант 5 CLAUDE.md).
YOUTUBE_SCOPE: Final[str] = "https://www.googleapis.com/auth/youtube"
SCOPES: Final[tuple[str, ...]] = (YOUTUBE_SCOPE,)
LOCAL_SERVER_PORT: Final[int] = 0          # 0 — любой свободный порт
ACCESS_TYPE: Final[str] = "offline"        # без него Google не выдаст refresh-токен
PROMPT: Final[str] = "consent"             # при повторной авторизации refresh-токен выдаётся заново
TOKEN_ENCODING: Final[str] = "utf-8"
TOKEN_FILE_TEMPLATE: Final[str] = "{account_name}.token.json"


class AuthErrorReason(str, Enum):
    CLIENT_SECRET_MISSING = "client_secret_missing"
    TOKEN_UNREADABLE = "token_unreadable"
    FLOW_FAILED = "flow_failed"
    REFRESH_FAILED = "refresh_failed"


class AuthError(Exception):
    """Единственное исключение, которое выпускает этот модуль наружу."""

    def __init__(self, reason: AuthErrorReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason: AuthErrorReason = reason
        self.detail: str = detail


def token_file_for(secrets_dir: Path, account_name: str) -> Path:
    """secrets\\<account_name>.token.json — единственный источник имени файла токена."""
    return secrets_dir / TOKEN_FILE_TEMPLATE.format(account_name=account_name)


def load_credentials(
    client_secret_file: Path,
    token_file: Path,
    login_hint: str,
    force_reauth: bool = False,
    on_login: Callable[[], None] | None = None,
) -> Credentials:
    """Готовые к работе учётные данные: из токена, обновлением или через браузер.

    login_hint — почта аккаунта Google канала (google_account): браузер сразу предлагает этот аккаунт.
    on_login вызывается ровно перед открытием браузера: владелец должен знать, какой канал выбирать.
    """
    if not client_secret_file.is_file():
        raise AuthError(AuthErrorReason.CLIENT_SECRET_MISSING, str(client_secret_file))
    if force_reauth:
        _drop_token(token_file)
        return _login(client_secret_file, token_file, login_hint, on_login)
    credentials: Credentials | None = _load_token(token_file)
    if credentials is None:
        return _login(client_secret_file, token_file, login_hint, on_login)
    if credentials.valid:
        return credentials
    refreshed: Credentials | None = _refresh(credentials, token_file)
    if refreshed is not None:
        return refreshed
    return _login(client_secret_file, token_file, login_hint, on_login)


def _login(
    client_secret_file: Path,
    token_file: Path,
    login_hint: str,
    on_login: Callable[[], None] | None,
) -> Credentials:
    if on_login is not None:
        on_login()
    return _run_flow(client_secret_file, token_file, login_hint)


def _drop_token(token_file: Path) -> None:
    try:
        token_file.unlink(missing_ok=True)
    except OSError as error:
        raise AuthError(AuthErrorReason.TOKEN_UNREADABLE, f"{token_file}: {error}") from error
    LOGGER.info("token_dropped file=%s", token_file.name)


def _load_token(token_file: Path) -> Credentials | None:
    """Нет файла — None (пойдём в браузер); файл есть, но не читается — это ошибка."""
    if not token_file.is_file():
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_file), scopes=list(SCOPES))
    except (OSError, ValueError, KeyError) as error:
        raise AuthError(AuthErrorReason.TOKEN_UNREADABLE, f"{token_file}: {error}") from error


def _refresh(credentials: Credentials, token_file: Path) -> Credentials | None:
    """None — токен отозван, нужен браузер; сетевой сбой — AuthError, браузер тут не поможет."""
    if not credentials.refresh_token:
        return None
    try:
        credentials.refresh(Request())
    except RefreshError as error:
        LOGGER.warning("token_refresh_rejected file=%s reason=%s", token_file.name, error)
        return None
    except TransportError as error:
        raise AuthError(AuthErrorReason.REFRESH_FAILED, str(error)) from error
    _save_token(credentials, token_file)
    LOGGER.info("token_refreshed file=%s", token_file.name)
    return credentials


def _run_flow(client_secret_file: Path, token_file: Path, login_hint: str) -> Credentials:
    try:
        flow: InstalledAppFlow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_file),
            scopes=list(SCOPES),
        )
        credentials: Credentials = flow.run_local_server(
            port=LOCAL_SERVER_PORT,
            access_type=ACCESS_TYPE,
            prompt=PROMPT,
            login_hint=login_hint,
        )
    except (OSError, ValueError, RefreshError, TransportError) as error:
        raise AuthError(AuthErrorReason.FLOW_FAILED, str(error)) from error
    if credentials is None:
        raise AuthError(AuthErrorReason.FLOW_FAILED, "flow returned no credentials")
    _save_token(credentials, token_file)
    LOGGER.info("token_created file=%s", token_file.name)
    return credentials


def _save_token(credentials: Credentials, token_file: Path) -> None:
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding=TOKEN_ENCODING)
    except OSError as error:
        raise AuthError(AuthErrorReason.TOKEN_UNREADABLE, f"{token_file}: {error}") from error
