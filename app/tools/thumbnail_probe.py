"""Пробник обложек (задача 5k): чем в ответе YouTube эфир со своей обложкой отличается от эфира без неё.

Запуск из корня репо (все каналы из secrets\\channels.json или один):
  .\\.venv_planers\\Scripts\\python.exe -m app.tools.thumbnail_probe
  .\\.venv_planers\\Scripts\\python.exe -m app.tools.thumbnail_probe --channel "Maria Kamenskay" --video VUeBbpshWVQ

Только чтение: liveBroadcasts.list(upcoming, broadcastType=all — вместе с постоянным эфиром канала) и
videos.list по тем же id (по 1 единице квоты за страницу). Картинки скачиваются с i.ytimg.com без
авторизации (квоту не тратят): адреса из ответа (*_live.jpg) и те же имена без _live. Ничего не пишет на YouTube.

Опыт 1 (16-09-2026 19:31): адреса, размеры и набор обложек у эфиров со своей обложкой и без неё одинаковы;
отличаются только сами картинки — у 8 эфиров Maria Kamenskay без обложки они совпадают байт в байт.
Опыт 2 (этот пробник): отдают ли адреса без _live что-то отличимое и совпадает ли картинка постоянного эфира
канала с заглушкой. Сырые ответы — в logs\\<дата>_thumbnail_probe.json.
Вспомогательный инструмент разработки, в поставку не входит.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config.loader import ChannelConfig, ConfigError, PlanerConfig, load_planer_config
from app.core.dates import FILE_STAMP_FORMAT, format_datetime_text
from app.google.auth import AuthError, load_credentials, save_token, token_file_for
from app.observability.logging_setup import get_logger
from app.paths import PlanerPaths, build_paths, resolve_root

LOGGER = get_logger("tools.thumbnail_probe")

EXIT_OK: Final[int] = 0
EXIT_REFUSED: Final[int] = 2
API_SERVICE_NAME: Final[str] = "youtube"
API_VERSION: Final[str] = "v3"
BROADCAST_PARTS: Final[str] = "snippet,status"
VIDEO_PARTS: Final[str] = "snippet,status,processingDetails"
VIDEO_PARTS_FALLBACK: Final[str] = "snippet,status"
BROADCAST_STATUS: Final[str] = "upcoming"
BROADCAST_TYPE_ALL: Final[str] = "all"
DEFAULT_BROADCAST_FLAG: Final[str] = "isDefaultBroadcast"
LIVE_SUFFIX: Final[str] = "_live.jpg"
STATIC_SUFFIX: Final[str] = ".jpg"
MAX_RESULTS: Final[int] = 50
IMAGE_TIMEOUT_SEC: Final[int] = 15
HASH_CHARS: Final[int] = 12
TITLE_CHARS: Final[int] = 40
DUMP_NAME: Final[str] = "{stamp}_thumbnail_probe.json"
MISSING: Final[str] = "-"
KIND_EVENT: Final[str] = "обычный"
KIND_DEFAULT: Final[str] = "постоянный"
KIND_EXTRA: Final[str] = "доп. --video"


@dataclass
class ProbeRow:
    """Один эфир: обложки глазами списка эфиров и ресурса видео, плюс сами картинки."""

    broadcast_id: str
    kind: str
    start_iso: str               # как прислал YouTube: по нему сортировка
    title: str
    life_cycle: str
    list_thumbnails: dict[str, Any] = field(default_factory=dict)
    video_thumbnails: dict[str, Any] = field(default_factory=dict)
    processing: dict[str, Any] = field(default_factory=dict)
    images: dict[str, dict[str, Any]] = field(default_factory=dict)   # имя файла → url, status, bytes, sha

    @property
    def start_text(self) -> str:
        if not self.start_iso:
            return MISSING
        return format_datetime_text(datetime.fromisoformat(self.start_iso).astimezone())


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace = _parse_args(argv)
    paths: PlanerPaths = build_paths(resolve_root())
    try:
        config: PlanerConfig = load_planer_config(paths.config_file, paths.channels_file)
    except ConfigError as error:
        print(f"ОТКАЗ: {error}")
        return EXIT_REFUSED
    dump: dict[str, Any] = {}
    for channel in _channels(config, args.channel):
        try:
            rows: list[ProbeRow] = _probe_channel(paths, channel, args.video)
        except (AuthError, HttpError, OSError) as error:
            print(f"ОТКАЗ канала {channel.account_name}: {error}")
            continue
        _print_channel(channel, rows)
        dump[channel.account_name] = [row.__dict__ for row in rows]
    print(f"Сырые данные: {_write_dump(paths, dump)}")
    return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Обложки запланированных эфиров")
    parser.add_argument("--channel", default="", help="ник канала (handle) как в secrets\\channels.json; пусто — все")
    parser.add_argument("--video", action="append", default=[], help="id эфира вне upcoming (можно несколько)")
    return parser.parse_args(argv)


def _channels(config: PlanerConfig, handle: str) -> list[ChannelConfig]:
    if not handle:
        return list(config.channels)
    channel: ChannelConfig | None = config.channel_by_handle(handle)
    return [channel] if channel is not None else []


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


def _probe_channel(paths: PlanerPaths, channel: ChannelConfig, extra_ids: list[str]) -> list[ProbeRow]:
    credentials: Any = _credentials(paths, channel)
    service: Any = build(API_SERVICE_NAME, API_VERSION, credentials=credentials, cache_discovery=False)
    rows: list[ProbeRow] = _list_rows(service)
    known: set[str] = {row.broadcast_id for row in rows}
    rows.extend(ProbeRow(video_id, KIND_EXTRA, "", "", MISSING) for video_id in extra_ids if video_id not in known)
    _fill_videos(service, rows)
    session: requests.Session = requests.Session()
    for row in rows:
        for name, url in _image_urls(row).items():
            row.images[name] = {"url": url} | _fetch_image(session, url)
    return rows


def _list_rows(service: Any) -> list[ProbeRow]:
    rows: list[ProbeRow] = []
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
            return sorted(rows, key=lambda row: (row.kind != KIND_DEFAULT, row.start_iso))


def _row(item: dict[str, Any]) -> ProbeRow:
    snippet: dict[str, Any] = item.get("snippet", {})
    return ProbeRow(
        broadcast_id=str(item.get("id", "")),
        kind=KIND_DEFAULT if snippet.get(DEFAULT_BROADCAST_FLAG) else KIND_EVENT,
        start_iso=str(snippet.get("scheduledStartTime") or "").replace("Z", "+00:00"),
        title=str(snippet.get("title") or ""),
        life_cycle=str(item.get("status", {}).get("lifeCycleStatus") or MISSING),
        list_thumbnails=dict(snippet.get("thumbnails") or {}),
    )


def _fill_videos(service: Any, rows: list[ProbeRow]) -> None:
    by_id: dict[str, ProbeRow] = {row.broadcast_id: row for row in rows}
    ids: list[str] = list(by_id)
    for offset in range(0, len(ids), MAX_RESULTS):
        chunk: list[str] = ids[offset:offset + MAX_RESULTS]
        response: dict[str, Any] = _videos_page(service, ",".join(chunk))
        for item in response.get("items", []):
            row: ProbeRow | None = by_id.get(str(item.get("id", "")))
            if row is None:
                continue
            snippet: dict[str, Any] = item.get("snippet", {})
            row.video_thumbnails = dict(snippet.get("thumbnails") or {})
            row.processing = dict(item.get("processingDetails") or {})
            row.title = row.title or str(snippet.get("title") or "")


def _videos_page(service: Any, ids: str) -> dict[str, Any]:
    """processingDetails может не отдаваться для эфиров — тогда без него, опыт не срывается."""
    try:
        return service.videos().list(part=VIDEO_PARTS, id=ids).execute()
    except HttpError as error:
        print(f"  videos.list с {VIDEO_PARTS} отказал ({error}); повторяю с {VIDEO_PARTS_FALLBACK}")
        return service.videos().list(part=VIDEO_PARTS_FALLBACK, id=ids).execute()


def _image_urls(row: ProbeRow) -> dict[str, str]:
    """Адреса из обоих ответов и их пары без _live — имя файла однозначно определяет картинку."""
    urls: dict[str, str] = {}
    for source in (row.list_thumbnails, row.video_thumbnails):
        for value in source.values():
            url: Any = value.get("url") if isinstance(value, dict) else None
            if not isinstance(url, str) or not url:
                continue
            urls.setdefault(_file_name(url), url)
            if url.endswith(LIVE_SUFFIX):
                static: str = url[: -len(LIVE_SUFFIX)] + STATIC_SUFFIX
                urls.setdefault(_file_name(static), static)
    return urls


def _file_name(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


def _fetch_image(session: requests.Session, url: str) -> dict[str, Any]:
    try:
        response: requests.Response = session.get(url, timeout=IMAGE_TIMEOUT_SEC)
    except requests.RequestException as error:
        return {"status": MISSING, "bytes": 0, "sha": MISSING, "error": str(error)}
    body: bytes = response.content
    sha: str = hashlib.sha256(body).hexdigest()[:HASH_CHARS] if body else MISSING
    return {"status": response.status_code, "bytes": len(body), "sha": sha}


def _images_text(row: ProbeRow, is_live: bool) -> str:
    parts: list[str] = []
    for name, image in row.images.items():
        if name.endswith(LIVE_SUFFIX) != is_live:
            continue
        short: str = name.replace(LIVE_SUFFIX, "").replace(STATIC_SUFFIX, "")
        parts.append(f"{short} {image['status']} {image['bytes']}б {str(image['sha'])[:6]}")
    return " | ".join(parts) or MISSING


def _sizes_text(thumbnails: dict[str, Any]) -> str:
    return " ".join(
        f"{name}={value.get('width', MISSING)}x{value.get('height', MISSING)}"
        for name, value in thumbnails.items()
        if isinstance(value, dict)
    ) or MISSING


def _print_channel(channel: ChannelConfig, rows: list[ProbeRow]) -> None:
    print(f"Канал {channel.account_name} ({channel.google_account}): эфиров {len(rows)}")
    for row in rows:
        same: str = "да" if row.list_thumbnails == row.video_thumbnails else "НЕТ"
        print(f"  {row.broadcast_id}  {row.kind}  {row.life_cycle}  {row.start_text}  «{row.title[:TITLE_CHARS]}»")
        print(f"    размеры: {_sizes_text(row.video_thumbnails)}; список=видео: {same}; processing: {row.processing or MISSING}")
        print(f"    _live  : {_images_text(row, is_live=True)}")
        print(f"    без    : {_images_text(row, is_live=False)}")
    _print_duplicates(rows)


def _print_duplicates(rows: list[ProbeRow]) -> None:
    """Одинаковая картинка у разных эфиров канала — кандидат в заглушку без своей обложки."""
    owners: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        for name, image in row.images.items():
            if image["status"] == 200 and image["sha"] != MISSING:
                owners.setdefault((name.split(row.broadcast_id)[-1], str(image["sha"])), set()).add(row.broadcast_id)
    shared: dict[tuple[str, str], set[str]] = {key: ids for key, ids in owners.items() if len(ids) > 1}
    print(f"  Одинаковые картинки у разных эфиров: {len(shared)}")
    for (name, sha), ids in sorted(shared.items(), key=lambda entry: (-len(entry[1]), entry[0])):
        print(f"    {name} sha {sha[:6]} x{len(ids)}: {', '.join(sorted(ids))}")


def _write_dump(paths: PlanerPaths, dump: dict[str, Any]) -> Path:
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    target: Path = paths.logs_dir / DUMP_NAME.format(stamp=datetime.now().strftime(FILE_STAMP_FORMAT))
    target.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    sys.exit(main())
