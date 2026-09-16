"""Пробник эфиров канала: что YouTube держит в upcoming и почему планер часть из этого не видит.

Запуск из корня репо:
  .\\.venv_planers\\Scripts\\python.exe -m app.tools.broadcast_probe --channel Osvald.X
  .\\.venv_planers\\Scripts\\python.exe -m app.tools.broadcast_probe --channel Osvald.X --remove HxTFRJslx2k

Список читается с broadcastType=all: видны и постоянные эфиры («Начать эфир сейчас»), которых planer
не запрашивает. --remove удаляет ровно один эфир по идентификатору и только если у эфира НЕТ времени
старта: эфиры планера этим инструментом не трогаются (инвариант 7). Подтверждение — с клавиатуры.
Вспомогательный инструмент разработки, в поставку не входит.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config.loader import ChannelConfig, ConfigError, PlanerConfig, load_planer_config
from app.google.auth import AuthError, load_credentials, token_file_for
from app.paths import PlanerPaths, build_paths, resolve_root

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
    parser.add_argument("--channel", required=True, help="ник канала (handle) как в secrets\\channels.json")
    parser.add_argument("--remove", default="", help="идентификатор эфира без времени старта, который надо удалить")
    return parser.parse_args(argv)


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
    credentials: Any = load_credentials(
        paths.client_secret_file,
        token_file_for(paths.secrets_dir, channel.handle),
        login_hint=channel.google_account,
    )
    return build(API_SERVICE_NAME, API_VERSION, credentials=credentials, cache_discovery=False)


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


if __name__ == "__main__":
    sys.exit(main())
