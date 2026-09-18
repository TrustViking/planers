"""Пробник эфиров канала: что YouTube держит в upcoming и почему планер часть из этого не видит.

Запуск из корня репо:
  .\\.venv_planers\\Scripts\\python.exe -m app.tools.broadcast_probe --channel "@Osvald.X"
  .\\.venv_planers\\Scripts\\python.exe -m app.tools.broadcast_probe --channel "@Osvald.X" --remove HxTFRJslx2k
  .\\.venv_planers\\Scripts\\python.exe -m app.tools.broadcast_probe --dump-undated [--channel "@Osvald.X"]

Ник — всегда в кавычках: в PowerShell @имя без кавычек — splatting, и в --channel уходит пустое значение
(прогон 18-09-2026: --channel @ПашаЭкскаватощик → «expected one argument»).

Список читается с broadcastType=all: видны и постоянные эфиры («Начать эфир сейчас»), которых planer
не запрашивает. --remove удаляет ровно один эфир по идентификатору и только если у эфира НЕТ времени
старта: эфиры планера этим инструментом не трогаются (инвариант 7). Подтверждение — с клавиатуры.
--dump-undated — по каждому каналу channels.json (или по одному из --channel) тем же запросом, что planer
(liveBroadcasts.list без broadcastType), выгружает сырой элемент ответа каждого эфира без scheduledStartTime
целиком в logs\\{DD-MM-YYYY}_{HHMMSS}_undated_broadcast_{broadcast_id}.json; ключи потоков, если попадутся, —
маской. Ничего не удаляет.
Вспомогательный инструмент разработки, в поставку не входит.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config.loader import ChannelConfig, ConfigError, PlanerConfig, load_planer_config
from app.core.dates import FILE_STAMP_FORMAT
from app.google.auth import AuthError, load_credentials, save_token, token_file_for
from app.observability.logging_setup import get_logger, mask_stream_key
from app.paths import PlanerPaths, build_paths, resolve_root

LOGGER = get_logger("tools.broadcast_probe")

EXIT_OK: Final[int] = 0
EXIT_REFUSED: Final[int] = 2
API_SERVICE_NAME: Final[str] = "youtube"
API_VERSION: Final[str] = "v3"
BROADCAST_PARTS: Final[str] = "snippet,contentDetails,status"
BROADCAST_STATUS: Final[str] = "upcoming"
BROADCAST_TYPE_ALL: Final[str] = "all"          # planer запрашивает умолчание event — постоянных эфиров не видит
MAX_RESULTS: Final[int] = 50
LIFE_CYCLE_LIVE: Final[str] = "live"
CONFIRM_WORD: Final[str] = "удалить"
MISSING: Final[str] = "-"
CHANNELS_KEY: Final[str] = "channels"
PROGRAM_DESCRIPTION: Final[str] = "Эфиры канала в upcoming и удаление эфира без времени старта"
UNDATED_DUMP_TEMPLATE: Final[str] = "{stamp}_undated_broadcast_{broadcast_id}.json"
STREAM_KEY_FIELD: Final[str] = "streamName"      # ключ потока в ответах YouTube: в файле — маской
JSON_INDENT: Final[int] = 2


@dataclass(frozen=True)
class BroadcastRow:
    """Строка списка эфиров: то, по чему видно, увидит ли эфир планер и можно ли его убрать."""

    broadcast_id: str
    start: str
    life_cycle: str
    privacy: str
    is_default: bool
    bound_stream_id: str
    title: str

    @property
    def is_undated(self) -> bool:
        return not self.start

    def text(self) -> str:
        default_mark: str = "постоянный" if self.is_default else "обычный"
        return (
            f"{self.broadcast_id:<11}  {self.start or MISSING:<20}  {self.life_cycle:<9}  "
            f"{self.privacy:<8}  {default_mark:<10}  поток {self.bound_stream_id or MISSING:<20}  {self.title}"
        )


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace = _parse_args(argv)
    paths: PlanerPaths = build_paths(resolve_root())
    if args.dump_undated:
        return _dump_undated(paths, args.channel)
    try:
        channel: ChannelConfig = _channel(paths, args.channel)
        service: Any = _service(paths, channel)
        rows: list[BroadcastRow] = _list_broadcasts(service)
    except (ConfigError, AuthError, HttpError, OSError) as error:
        print(f"ОТКАЗ: {error}")
        return EXIT_REFUSED
    _print_rows(channel, rows)
    if not args.remove:
        return EXIT_OK
    return _remove(service, rows, args.remove)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=PROGRAM_DESCRIPTION)
    parser.add_argument("--channel", default="", help="ник канала (handle) как в secrets\\channels.json")
    parser.add_argument("--remove", default="", help="идентификатор эфира без времени старта, который надо удалить")
    parser.add_argument(
        "--dump-undated",
        action="store_true",
        help="выгрузить сырые элементы эфиров без времени старта по всем каналам (или по --channel) в logs\\",
    )
    args: argparse.Namespace = parser.parse_args(argv)
    if not args.dump_undated and not args.channel:
        parser.error("нужен --channel (или --dump-undated)")
    return args


def _channel(paths: PlanerPaths, handle: str) -> ChannelConfig:
    config: PlanerConfig = load_planer_config(paths.config_file, paths.channels_file)
    channel: ChannelConfig | None = config.channel_by_handle(handle)
    if channel is None:
        names: str = ", ".join(item.handle for item in config.channels)
        raise ConfigError(
            config_path=paths.channels_file,
            key_path=CHANNELS_KEY,
            problem=f"канала с ником {handle} нет в конфиге; есть: {names}",
        )
    return channel


def _service(paths: PlanerPaths, channel: ChannelConfig) -> Any:
    credentials: Any = _credentials(paths, channel)
    return build(API_SERVICE_NAME, API_VERSION, credentials=credentials, cache_discovery=False)


def _credentials(paths: PlanerPaths, channel: ChannelConfig) -> Any:
    """Токен канала; новый вход в браузере файл сам не пишет (задача 5m-A) — пробник сохраняет его здесь.

    Пробник канал не проверяет: выберите в браузере нужный канал.
    """
    token_file: Path = token_file_for(paths.secrets_dir, channel.handle)
    logged_in: list[bool] = []
    credentials: Any = load_credentials(
        paths.client_secret_file,
        token_file,
        login_hint=channel.google_account,
        on_login=lambda: logged_in.append(True),
    )
    if logged_in:
        save_token(credentials, token_file)
        LOGGER.info('probe_token_saved channel="%s" handle=%s file=%s', channel.account_name, channel.handle, token_file.name)
        print(f"токен записан: {token_file}")
    return credentials


def _list_broadcasts(service: Any) -> list[BroadcastRow]:
    rows: list[BroadcastRow] = []
    page_token: str | None = None
    while True:
        response: dict[str, Any] = service.liveBroadcasts().list(
            part=BROADCAST_PARTS,
            broadcastStatus=BROADCAST_STATUS,
            broadcastType=BROADCAST_TYPE_ALL,
            maxResults=MAX_RESULTS,
            pageToken=page_token,
        ).execute()
        rows.extend(_row(item) for item in response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return rows


def _row(item: dict[str, Any]) -> BroadcastRow:
    snippet: dict[str, Any] = item.get("snippet", {})
    return BroadcastRow(
        broadcast_id=str(item.get("id", "")),
        start=str(snippet.get("scheduledStartTime") or ""),
        life_cycle=str(item.get("status", {}).get("lifeCycleStatus") or ""),
        privacy=str(item.get("status", {}).get("privacyStatus") or ""),
        is_default=bool(snippet.get("isDefaultBroadcast")),
        bound_stream_id=str(item.get("contentDetails", {}).get("boundStreamId") or ""),
        title=str(snippet.get("title") or ""),
    )


def _print_rows(channel: ChannelConfig, rows: list[BroadcastRow]) -> None:
    print(f"Канал {channel.account_name} ({channel.google_account}): эфиров в upcoming — {len(rows)}")
    for row in rows:
        print(f"  {row.text()}")
    undated: list[BroadcastRow] = [row for row in rows if row.is_undated]
    print(f"Без времени старта (планер их не видит): {len(undated)}")


def _remove(service: Any, rows: list[BroadcastRow], broadcast_id: str) -> int:
    found: BroadcastRow | None = next((row for row in rows if row.broadcast_id == broadcast_id), None)
    refusal: str | None = _refusal(found, broadcast_id)
    if refusal is not None or found is None:
        print(f"ОТКАЗ: {refusal}")
        return EXIT_REFUSED
    print(f"Будет удалён эфир: {found.text()}")
    if input(f"Напечатайте «{CONFIRM_WORD}» для подтверждения: ").strip().lower() != CONFIRM_WORD:
        print("ОТКАЗ: подтверждения не было, ничего не удалено")
        return EXIT_REFUSED
    try:
        service.liveBroadcasts().delete(id=broadcast_id).execute()
    except (HttpError, OSError) as error:
        print(f"ОТКАЗ: YouTube не удалил эфир — {error}")
        return EXIT_REFUSED
    print(f"Эфир {broadcast_id} удалён")
    return EXIT_OK


def _refusal(found: BroadcastRow | None, broadcast_id: str) -> str | None:
    """Удаляем только брошенный эфир без времени старта: всё остальное — руками в Студии."""
    if found is None:
        return f"эфира {broadcast_id} нет в списке upcoming этого канала"
    if not found.is_undated:
        return f"у эфира {broadcast_id} есть время старта ({found.start}) — такие эфиры пробник не удаляет"
    if found.is_default:
        return f"эфир {broadcast_id} — постоянный эфир канала, YouTube его не удаляет"
    if found.life_cycle == LIFE_CYCLE_LIVE:
        return f"эфир {broadcast_id} сейчас в эфире — сначала завершите его в Студии"
    return None


def _dump_undated(paths: PlanerPaths, handle: str) -> int:
    """По каждому каналу — сырые элементы эфиров без времени старта, как пришли; сбой канала — строка и дальше."""
    try:
        config: PlanerConfig = load_planer_config(paths.config_file, paths.channels_file)
        channels: list[ChannelConfig] = [_channel(paths, handle)] if handle else list(config.channels)
    except ConfigError as error:
        print(f"ОТКАЗ: {error}")
        return EXIT_REFUSED
    stamp: str = datetime.now().strftime(FILE_STAMP_FORMAT)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    failed: bool = False
    for channel in channels:
        try:
            items: list[dict[str, Any]] = _list_raw_items(_service(paths, channel))
        except (AuthError, HttpError, OSError) as error:
            print(f"ОТКАЗ: {channel.account_name} {channel.handle} — {error}")
            failed = True
            continue
        undated: list[dict[str, Any]] = [item for item in items if not _raw_start(item)]
        print(f"Канал {channel.account_name} {channel.handle}: эфиров {len(items)}, без времени старта {len(undated)}")
        for item in undated:
            print(f"  {_write_undated(paths, stamp, item)}")
    return EXIT_REFUSED if failed else EXIT_OK


def _list_raw_items(service: Any) -> list[dict[str, Any]]:
    """Тот же запрос, что у planer (broadcastType по умолчанию), — элементы ответа без разбора."""
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        response: dict[str, Any] = service.liveBroadcasts().list(
            part=BROADCAST_PARTS,
            broadcastStatus=BROADCAST_STATUS,
            maxResults=MAX_RESULTS,
            pageToken=page_token,
        ).execute()
        items.extend(item for item in response.get("items", []) if isinstance(item, dict))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def _raw_start(item: dict[str, Any]) -> Any:
    snippet: Any = item.get("snippet")
    return snippet.get("scheduledStartTime") if isinstance(snippet, dict) else None


def _write_undated(paths: PlanerPaths, stamp: str, item: dict[str, Any]) -> Path:
    path: Path = paths.logs_dir / UNDATED_DUMP_TEMPLATE.format(stamp=stamp, broadcast_id=item.get("id", MISSING))
    text: str = json.dumps(_masked(item), ensure_ascii=False, indent=JSON_INDENT)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _masked(value: Any) -> Any:
    """Копия ответа с ключами потоков под маской; всё остальное — как пришло."""
    if isinstance(value, dict):
        return {
            key: mask_stream_key(item) if key == STREAM_KEY_FIELD and isinstance(item, str) else _masked(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return value


if __name__ == "__main__":
    sys.exit(main())
