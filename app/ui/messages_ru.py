"""Все тексты для владельца: консоль, отчёт, файл ключей (ТЗ §5.5, §5.6). В коде — только эти константы."""
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
    "Сейчас доступны --dry-run и --status."
)
FULL_RUN_NOT_AVAILABLE_YET: Final[str] = (
    "Полный запуск (создание эфиров) появится на этапе 3; сейчас доступны --dry-run и --status"
)
NOTICE_FAKE_PLATFORM: Final[str] = (
    "Площадка — заглушка до этапа 3: сверка с YouTube не выполнялась, все слоты считаются незапланированными"
)
REGISTRY_UNREADABLE: Final[str] = "Журнал {path} не читается: {error}. Ничего не делалось."

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
PACKAGE_FINISHED: Final[str] = (
    "- {file} — принят, слотов {total}, из них под мои языки {mine}; всё обработано, перенесён в inbox\\done"
)
PACKAGE_FINISH_FAILED: Final[str] = "- {file} — всё обработано, перенести в inbox\\done не удалось: {error}"
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

# --- пропуски (ТЗ §7.1, §7.2)
SKIP_PAST: Final[str] = "- {date} {time} {language} — уже прошло"
SKIP_TOO_LATE: Final[str] = "- {date} {time} {language} — до старта меньше {minutes} минут"
SKIP_NO_CHANNEL: Final[str] = "- {date} {time} {language} — нет канала для языка {language}"

# --- исходы пар (ТЗ §5.6, §7.3)
OUTCOME_SLOT_PREFIX: Final[str] = "- {date} {time} {language} → {account_name}"
OUTCOME_CHANNEL_PREFIX: Final[str] = "- {account_name}"
OUTCOME_CREATED: Final[str] = "{prefix} — эфир создан, ключ получен, форма {form}"
OUTCOME_RECREATED: Final[str] = "{prefix} — эфира на YouTube не было (удалён?), создан заново, ключ получен, форма {form}"
OUTCOME_CREATE_PLANNED: Final[str] = "{prefix} — эфира нет, будет создан"
OUTCOME_RECREATE_PLANNED: Final[str] = "{prefix} — эфира на YouTube нет (удалён?), будет создан заново"
OUTCOME_FIXED: Final[str] = "{prefix} — на YouTube было другое {what}; обновлено. {form_part}"
OUTCOME_FIX_PLANNED: Final[str] = "{prefix} — на YouTube другое {what}; будет обновлено"
FIXED_FORM_NOT_RESENT: Final[str] = "Ключ и ссылка прежние, форма не переотправлялась"
FIXED_FORM_RESENT: Final[str] = "Ключ и ссылка прежние, форма {form} (повторная отправка)"
FIXED_FORM_REBIND: Final[str] = "Эфира не было в журнале — ключ взят с площадки, форма {form}"
OUTCOME_MATCHED: Final[str] = "{prefix} — {url}{suffix}"
MATCHED_SUFFIX_RESENT: Final[str] = "; форма {form} (повторная отправка)"
MATCHED_SUFFIX_REBIND: Final[str] = "; эфира не было в журнале — ключ взят с площадки, форма {form}"
MATCHED_SUFFIX_REBIND_PLANNED: Final[str] = "; эфира нет в журнале — ключ будет взят с площадки"
OUTCOME_AMBIGUOUS: Final[str] = (
    "{prefix} — на канале несколько эфиров на эту минуту без маркера планера, "
    "не могу различить — разберитесь вручную"
)
OUTCOME_ERROR: Final[str] = "{prefix} — {origin}: {code} ({message})"
OUTCOME_PLANER_ERROR: Final[str] = "{prefix} — {text}"
OUTCOME_DRY_RUN_SUFFIX: Final[str] = " — не выполнено (dry-run)"
FORM_MARK_SENT: Final[str] = "✅"
FORM_MARK_FAILED: Final[str] = "❌ (повторю в следующий запуск)"
FORM_MARK_WAITING: Final[str] = "⏳ отправка появится на этапе 4"
# ключи — значения ChangedField (app/pipeline/reconciler.py)
CHANGED_FIELD_TEXT: Final[dict[str, str]] = {"title": "название", "description": "описание"}
CHANGED_FIELDS_JOINER: Final[str] = " и "
# ключи — OutcomeError.origin: значения Platform (app/config/loader.py) и источники планера
ERROR_ORIGIN_TEXT: Final[dict[str, str]] = {
    "youtube": "YouTube",
    "registry": "журнал",
    "package": "пакет",
}
PLANER_ERROR_TEXT_ORIGINS: Final[frozenset[str]] = frozenset({"planer"})
PLANER_ERROR_TEXT: Final[dict[str, str]] = {
    "noBoundStream": "у найденного эфира нет привязанного потока — ключ получить нельзя; привяжите поток или удалите эфир",
    "registrySaveFailed": "журнал не записан: {detail}",
    "keysWriteFailed": "файл ключей не записан: {detail}",
}
ORPHAN_LINE: Final[str] = "- {date} {time} {language} → {account_name} — {url} — эфир не удалён"
SCHEDULED_LINE: Final[str] = "{prefix} — {url}"

# --- отчёт (ТЗ §5.6)
REPORT_TITLE: Final[str] = "# Планер — отчёт {generated_at}, владелец: {owner}"
REPORT_TITLE_DRY_RUN: Final[str] = " (dry-run)"
REPORT_NOTICE: Final[str] = "⚠ {notice}"
REPORT_SECTION_PACKAGES: Final[str] = "## Пакеты"
REPORT_SECTION_CREATED: Final[str] = "## Создано ({count})"
REPORT_SECTION_FIXED: Final[str] = "## Исправлено ({count})"
REPORT_SECTION_MATCHED: Final[str] = "## Уже запланировано, совпадает ({count})"
REPORT_SECTION_ORPHANS: Final[str] = "## Перенесён или отменён? ({count})"
REPORT_SECTION_SCHEDULED: Final[str] = "## Запланировано на каналах ({count})"
REPORT_SECTION_SKIPPED: Final[str] = "## Пропущено"
REPORT_SECTION_ERRORS: Final[str] = "## Ошибки"
REPORT_TOTAL: Final[str] = (
    "Итог: создано {created}, исправлено {fixed}, копий {matched}, пропущено {skipped}, "
    "ошибок {errors}.{keys_file}"
)
REPORT_STATUS_TOTAL: Final[str] = "Итог: запланировано {scheduled}, ошибок {errors}.{keys_file}"
REPORT_TOTAL_KEYS_FILE: Final[str] = " Файл ключей: {path}"

# --- файл ключей (ТЗ §5.5)
KEYS_FILE_HEADER: Final[str] = (
    "# Ключи трансляций. Сгенерировано планером {generated_at}. Файл производный — не править."
)
KEYS_FILE_COLUMNS: Final[str] = (
    "# язык | дата | время (Киев) | аккаунт | статус формы | stream_url | stream_key | ссылка на эфир"
)
KEY_FORM_SENT: Final[str] = "форма ✅ {sent_at}"
KEY_FORM_FAILED: Final[str] = "форма ❌ {error}"
KEY_FORM_WAITING: Final[str] = "форма ⏳ отправка появится на этапе 4"
KEY_FORM_NEVER_SENT: Final[str] = "форма — не отправлялась"
