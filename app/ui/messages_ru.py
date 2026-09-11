"""Все тексты для владельца: консоль и отчёт (ТЗ §5.6). В коде — только эти константы."""
from __future__ import annotations

from typing import Final

# --- командная строка (ТЗ §7.6)
CLI_DESCRIPTION: Final[str] = "Планер: регистрация трансляций по пакетам броадкастера."
HELP_DRY_RUN: Final[str] = "прочитать пакеты и показать, что было бы сделано; ничего не создавать, не переносить и не удалять"
HELP_CHECK: Final[str] = "проверить авторизацию и права каждого канала"
HELP_AUTH: Final[str] = "заново авторизовать канал (id из planer.yaml)"
HELP_STATUS: Final[str] = "сверка и отчёт без пакетов из inbox"
HELP_DEBUG: Final[str] = "подробный лог в консоли"
MODE_NOT_AVAILABLE_YET: Final[str] = (
    "Режим {mode} пока не реализован (появится на этапе {stage}). "
    "Сейчас доступны запуск без флагов и --dry-run."
)

# --- конфиг (ТЗ §5.2)
CONFIG_CREATED_FROM_EXAMPLE: Final[str] = (
    "Создан {path} из примера. Впишите владельца и свои каналы и запустите планер снова."
)
CONFIG_ERROR: Final[str] = "Ошибка в конфиге {path}: {key} — {problem}"
CONFIG_ROOT_KEY: Final[str] = "(корень файла)"
CONFIG_PROBLEM_EXAMPLE_MISSING: Final[str] = "файла нет, и нет примера {example}, из которого его создать"
CONFIG_PROBLEM_YAML: Final[str] = "файл не читается как YAML: {error}"
CONFIG_PROBLEM_NOT_MAPPING: Final[str] = "нужен набор ключей (словарь)"
CONFIG_PROBLEM_MISSING_KEY: Final[str] = "обязательный ключ отсутствует"
CONFIG_PROBLEM_UNKNOWN_KEY: Final[str] = "неизвестный ключ"
CONFIG_PROBLEM_NON_EMPTY_STRING: Final[str] = "нужна непустая строка"
CONFIG_PROBLEM_INT_MIN: Final[str] = "нужно целое число не меньше {minimum}"
CONFIG_PROBLEM_BOOL: Final[str] = "нужно true или false"
CONFIG_PROBLEM_CHANNELS_EMPTY: Final[str] = "нужен непустой список каналов"
CONFIG_PROBLEM_CHANNEL_ID: Final[str] = "только латиница в нижнем регистре, цифры и _, первой — буква"
CONFIG_PROBLEM_CHANNEL_ID_DUPLICATE: Final[str] = "id «{value}» уже есть у другого канала"
CONFIG_PROBLEM_PLATFORM_FACEBOOK: Final[str] = "Facebook появится на этапе 6; сейчас поддерживается только youtube"
CONFIG_PROBLEM_PLATFORM_UNKNOWN: Final[str] = "неизвестная площадка «{value}»; допустимо: {allowed}"
CONFIG_PROBLEM_LANGUAGES: Final[str] = "нужен непустой список языков строчными буквами, например [uk]"
CONFIG_PROBLEM_LANGUAGE_DUPLICATE: Final[str] = "язык «{value}» указан дважды"
CONFIG_PROBLEM_CHOICE: Final[str] = "допустимо: {allowed}"

# --- inbox и пакеты (ТЗ §7.1)
INBOX_EMPTY: Final[str] = "В {path} нет пакетов *.bcast — сохраните туда пакет от оператора и запустите снова."
REPORT_WRITTEN: Final[str] = "Отчёт сохранён: {path}"
PACKAGE_ACCEPTED: Final[str] = "- {file} — принят, слотов {total}, из них под мои языки {mine}"
PACKAGE_DAMAGED: Final[str] = "- {file} — пакет повреждён: {reason}; файл не тронут"
PACKAGE_UNSUPPORTED_SCHEMA: Final[str] = (
    "- {file} — версия пакета {version} не поддерживается (нужна {supported}); файл не тронут"
)
PACKAGE_ALL_PAST_ARCHIVED: Final[str] = "- {file} — все слоты в прошлом, перенесён в inbox\\archive"
PACKAGE_ALL_PAST_KEPT: Final[str] = "- {file} — все слоты в прошлом (dry-run: не перенесён)"
PACKAGE_ARCHIVE_FAILED: Final[str] = "- {file} — все слоты в прошлом, перенести в inbox\\archive не удалось: {error}"
PACKAGE_REASON_WITH_DETAIL: Final[str] = "{reason} ({detail})"
# ключи — значения PackageErrorReason (app/package/model.py)
PACKAGE_REASON_TEXT: Final[dict[str, str]] = {
    "not_zip": "не ZIP-архив",
    "no_manifest": "нет manifest.json",
    "bad_json": "manifest.json не читается",
    "unsupported_schema": "неизвестная версия пакета",
    "missing_key": "в манифесте нет обязательного поля",
    "bad_value": "неверное значение в манифесте",
    "duplicate_slot": "slot_id повторяется",
    "preview_missing": "в архиве нет файла превью",
}

# --- пропуски (ТЗ §7.1, §7.2) и пары к сверке
SKIP_PAST: Final[str] = "- {date} {time} {language} — уже прошло"
SKIP_TOO_LATE: Final[str] = "- {date} {time} {language} — до старта меньше {minutes} минут"
SKIP_NO_CHANNEL: Final[str] = "- {date} {time} {language} — нет канала для языка {language}"
PAIR_TO_RECONCILE: Final[str] = "- {date} {time} {language} → {account_name} ({channel_id})"

# --- отчёт (ТЗ §5.6)
REPORT_TITLE: Final[str] = "# Планер — отчёт {generated_at}, владелец: {owner}"
REPORT_SECTION_PACKAGES: Final[str] = "## Пакеты"
REPORT_SECTION_CREATED: Final[str] = "## Создано ({count})"
REPORT_SECTION_FIXED: Final[str] = "## Исправлено ({count})"
REPORT_SECTION_MATCHED: Final[str] = "## Уже запланировано, совпадает ({count})"
# временный раздел до этапа 2b: пары, которые пойдут на сверку с YouTube
REPORT_SECTION_PENDING: Final[str] = "## К сверке с YouTube ({count}) — сверка появится на этапе 2b"
REPORT_SECTION_SKIPPED: Final[str] = "## Пропущено"
REPORT_SECTION_ERRORS: Final[str] = "## Ошибки"
REPORT_TOTAL: Final[str] = (
    "Итог: создано {created}, исправлено {fixed}, копий {matched}, пропущено {skipped}, "
    "ошибок {errors}{pending}.{keys_file}"
)
REPORT_TOTAL_PENDING: Final[str] = "; к сверке: {count} пар"
REPORT_TOTAL_KEYS_FILE: Final[str] = " Файл ключей: {path}"
