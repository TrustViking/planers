"""OAuth для YouTube (ТЗ §5.3): один токен на канал, единственный скоуп — youtube.

Перенос ветки `_create_oauth_credentials` из broadcaster/app/google/auth.py:
только OAuth, без service account, Docs, Drive и Sheets. Порт локального сервера — 0
(свободный), иначе занятый порт кладёт авторизацию.

Вход в браузере файл токена не пишет: токен записывает вызывающий (save_token) — только после того,
как канал за этим входом подтверждён. Обновление действующего токена пишет файл сразу.
Ожидание браузера ограничено LOGIN_TIMEOUT_SEC; текст про ссылку в консоли и страница «вход выполнен»
в браузере — из messages_ru, а не английские тексты библиотеки.
"""
from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Final

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

from app.core.text import token_file_stem
from app.observability.logging_setup import get_logger
from app.ui import messages_ru as msg

LOGGER = get_logger("auth")

# Единственный источник скоупа в проекте (ТЗ §5.3, инвариант 5 CLAUDE.md).
YOUTUBE_SCOPE: Final[str] = "https://www.googleapis.com/auth/youtube"
SCOPES: Final[tuple[str, ...]] = (YOUTUBE_SCOPE,)
LOCAL_SERVER_PORT: Final[int] = 0          # 0 — любой свободный порт
ACCESS_TYPE: Final[str] = "offline"        # без него Google не выдаст refresh-токен
# select_account — экран выбора аккаунта и канала (личный и дополнительные) даже при login_hint;
# consent — при повторной авторизации refresh-токен выдаётся заново.
PROMPT: Final[str] = "select_account consent"
# Ожидание входа в браузере: без предела запуск висит, пока окно не закроют (прогон 17-09-2026 16:38).
# 10 минут — с запасом: живой первый вход 17-09-2026 (выбор аккаунта, канала и экран «не проверено») занял 8,5 минуты.
LOGIN_TIMEOUT_SEC: Final[int] = 600
SECONDS_PER_MINUTE: Final[int] = 60
LOGIN_TIMEOUT_MINUTES: Final[int] = LOGIN_TIMEOUT_SEC // SECONDS_PER_MINUTE
TOKEN_ENCODING: Final[str] = "utf-8"
TOKEN_FILE_TEMPLATE: Final[str] = "{stem}.token.json"


class AuthErrorReason(str, Enum):
    CLIENT_SECRET_MISSING = "client_secret_missing"
    TOKEN_UNREADABLE = "token_unreadable"
    FLOW_FAILED = "flow_failed"
    REFRESH_FAILED = "refresh_failed"
    LOGIN_REQUIRED = "login_required"   # нужен браузер, а вызывающий вход запретил (allow_login=False)
    LOGIN_TIMEOUT = "login_timeout"     # вход в браузере не завершён за LOGIN_TIMEOUT_SEC


class AuthError(Exception):
    """Единственное исключение, которое выпускает этот модуль наружу."""

    def __init__(self, reason: AuthErrorReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason: AuthErrorReason = reason
        self.detail: str = detail


def token_file_for(secrets_dir: Path, handle: str) -> Path:
    """secrets\\<ник>.token.json — единственный источник пути к токену; имя — core.text.token_file_stem."""
    return secrets_dir / TOKEN_FILE_TEMPLATE.format(stem=token_file_stem(handle))


def load_credentials(
    client_secret_file: Path,
    token_file: Path,
    login_hint: str,
    force_reauth: bool = False,
    on_login: Callable[[], None] | None = None,
    allow_login: bool = True,
) -> Credentials:
    """Готовые к работе учётные данные: из токена, обновлением или через браузер.

    login_hint — почта аккаунта Google канала (google_account): браузер сразу предлагает этот аккаунт.
    on_login вызывается ровно перед открытием браузера: по нему вызывающий знает, что вход новый.
    force_reauth — вход в браузере без чтения токена; файл токена не удаляется и не перезаписывается.
    allow_login=False — браузер не открывается: нужен вход — AuthError(LOGIN_REQUIRED), токен не трогается.
    Учётные данные нового входа в файл не пишутся — это делает save_token после подтверждения канала.
    """
    if not client_secret_file.is_file():
        raise AuthError(AuthErrorReason.CLIENT_SECRET_MISSING, str(client_secret_file))
    login: _Login = _Login(client_secret_file, token_file, login_hint, on_login, allow_login)
    if force_reauth:
        return login.run()
    credentials: Credentials | None = _load_token(token_file)
    if credentials is None:
        return login.run()
    if credentials.valid:
        return credentials
    refreshed: Credentials | None = _refresh(credentials, token_file)
    if refreshed is not None:
        return refreshed
    return login.run()


def save_token(credentials: Credentials, token_file: Path) -> None:
    """Записать токен канала; единственная запись файла токена, кроме обновления действующего."""
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding=TOKEN_ENCODING)
    except OSError as error:
        raise AuthError(AuthErrorReason.TOKEN_UNREADABLE, f"{token_file}: {error}") from error


def drop_token(token_file: Path) -> None:
    """Удалить файл токена: единственное место удаления (токен ведёт не на тот канал)."""
    try:
        token_file.unlink(missing_ok=True)
    except OSError as error:
        raise AuthError(AuthErrorReason.TOKEN_UNREADABLE, f"{token_file}: {error}") from error
    LOGGER.info("token_dropped file=%s", token_file.name)


class _Login:
    """Вход через браузер — единственный путь к нему; запрет входа проверяется здесь же."""

    def __init__(
        self,
        client_secret_file: Path,
        token_file: Path,
        login_hint: str,
        on_login: Callable[[], None] | None,
        allow_login: bool,
    ) -> None:
        self._client_secret_file: Path = client_secret_file
        self._token_file: Path = token_file
        self._login_hint: str = login_hint
        self._on_login: Callable[[], None] | None = on_login
        self._allow_login: bool = allow_login

    def run(self) -> Credentials:
        if not self._allow_login:
            LOGGER.info("login_not_allowed file=%s", self._token_file.name)
            raise AuthError(AuthErrorReason.LOGIN_REQUIRED, self._token_file.name)
        if self._on_login is not None:
            self._on_login()
        credentials: Credentials = _run_flow(self._client_secret_file, self._login_hint)
        LOGGER.info("login_completed file=%s", self._token_file.name)
        return credentials


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
    save_token(credentials, token_file)
    LOGGER.info("token_refreshed file=%s", token_file.name)
    return credentials


def _run_flow(client_secret_file: Path, login_hint: str) -> Credentials:
    """Браузер не дольше LOGIN_TIMEOUT_SEC; файл токена не пишется.

    WSGITimeoutError наследует AttributeError — ловится отдельно, до общих сбоев.
    """
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
            timeout_seconds=LOGIN_TIMEOUT_SEC,
            authorization_prompt_message=msg.AUTH_OPEN_LINK,
            success_message=msg.AUTH_BROWSER_DONE,
        )
    except WSGITimeoutError as error:
        raise AuthError(AuthErrorReason.LOGIN_TIMEOUT, f"no answer in {LOGIN_TIMEOUT_SEC} s") from error
    except (OSError, ValueError, RefreshError, TransportError) as error:
        raise AuthError(AuthErrorReason.FLOW_FAILED, str(error)) from error
    if credentials is None:
        raise AuthError(AuthErrorReason.FLOW_FAILED, "flow returned no credentials")
    return credentials
