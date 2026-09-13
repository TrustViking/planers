"""Все тексты для владельца: консоль, отчёт, файл ключей (ТЗ §5.5, §5.6). В коде — только эти константы."""
from __future__ import annotations

from typing import Final

# --- командная строка (ТЗ §7.6)
CLI_DESCRIPTION: Final[str] = "Планер: регистрация трансляций по пакетам броадкастера."
HELP_DRY_RUN: Final[str] = "прочитать пакеты и показать, что было бы сделано; ничего не создавать, не переносить и не удалять"
HELP_CHECK: Final[str] = "проверить авторизацию и права каждого канала"
HELP_AUTH: Final[str] = "заново авторизовать канал (ключ из channels.yaml) или all — все каналы"
HELP_STATUS: Final[str] = "сверка и отчёт без пакетов из promo"
HELP_DEBUG: Final[str] = "подробный лог в консоли"
FIRST_RUN_HEADER: Final[str] = (
    "Запуск без ключей: проверяю каналы и планирую эфиры по пакетам. "
    "Канал без авторизации будет авторизован сейчас."
)

# --- конфиг (ТЗ §5.2)
CONFIG_CREATED_FROM_EXAMPLE: Final[str] = (
    "Создан {path} из примера. Проверьте значения в нём и запустите планер снова."
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
CONFIG_PROBLEM_CHANNEL_ID: Final[str] = "только латиница в нижнем регистре, цифры и _"
CONFIG_PROBLEM_CHANNEL_ID_DUPLICATE: Final[str] = "id «{value}» уже есть у другого канала"
CONFIG_PROBLEM_PLATFORM_FACEBOOK: Final[str] = "Facebook появится на этапе 6; сейчас поддерживается только youtube"
CONFIG_PROBLEM_PLATFORM_UNKNOWN: Final[str] = "неизвестная площадка «{value}»; допустимо: {allowed}"
CONFIG_PROBLEM_LANGUAGES: Final[str] = "нужен непустой список языков строчными буквами, например [uk]"
CONFIG_PROBLEM_LANGUAGE_DUPLICATE: Final[str] = "язык «{value}» указан дважды"
CONFIG_PROBLEM_CHOICE: Final[str] = "допустимо: {allowed}"

# --- авторизация и каналы (ТЗ §5.3)
CLIENT_SECRET_MISSING: Final[str] = (
    "Нет файла {path} — без него планер не может обратиться к YouTube. "
    "Возьмите его у оператора и положите рядом с программой, в папку secrets."
)
CHANNELS_STATE_UNREADABLE: Final[str] = "Файл привязок каналов {path} не читается: {error}. Ничего не делалось."
AUTH_UNKNOWN_CHANNEL: Final[str] = "В {path} нет канала с ключом «{key}». Известные ключи: {known}."
AUTH_STARTING: Final[str] = "Канал «{key}» ({account_name}): сейчас откроется браузер."
AUTH_UNVERIFIED_APP_WARNING: Final[str] = (
    "Google покажет предупреждение «Google hasn't verified this app» — это ожидаемо, "
    "приложение ещё не проходило проверку Google. Нажмите Advanced, затем ссылку "
    "Go to ... (unsafe), затем Continue. На экране согласия должен быть пункт про управление "
    "вашим аккаунтом YouTube — отметьте его и подтвердите."
)
AUTH_CHOOSE_RIGHT_CHANNEL: Final[str] = (
    "Выбирайте тот аккаунт и тот канал, который в channels.yaml записан как «{account_name}»."
)
AUTH_OK: Final[str] = "Канал «{key}» авторизован: {title} (id {youtube_channel_id})."
AUTH_BINDING_SAVED: Final[str] = "Привязка записана в {path}."
AUTH_BINDING_UPDATED: Final[str] = "Привязка подтверждена: это тот же канал, что и раньше."
AUTH_BINDING_MISMATCH: Final[str] = (
    "Ключ «{key}» уже привязан к каналу {expected_title} (id {expected_id}), "
    "а токен ведёт на {actual_title} (id {actual_id}). Ничего не переписано. "
    "Либо авторизуйтесь заново и выберите правильный канал, либо исправьте channels.yaml."
)
AUTH_FAILED: Final[str] = "Канал «{key}»: авторизация не удалась — {reason}."
AUTH_SCOPE_HINT: Final[str] = (
    "Если на экране согласия не было пункта про управление YouTube-аккаунтом — "
    "значит скоуп youtube не добавлен в настройках доступа приложения в Google Cloud."
)
# ключи — значения AuthErrorReason (app/google/auth.py)
AUTH_REASON_TEXT: Final[dict[str, str]] = {
    "client_secret_missing": "нет файла client_secret.json",
    "token_unreadable": "файл токена не читается; удалите его и повторите --auth",
    "flow_failed": "браузер не вернул разрешение",
    "refresh_failed": "не удалось обновить токен (нет связи с Google)",
}

# --- проверка каналов (--check)
CHECK_HEADER: Final[str] = "Проверка каналов по {path}:"
CHECK_CHANNEL_OK: Final[str] = (
    "- {key}: {title} (id {youtube_channel_id}), язык канала на YouTube: {channel_language}; "
    "языки стримов из channels.yaml: {languages}; запланированных эфиров: {upcoming}"
)
CHECK_CHANNEL_LANGUAGE_UNSET: Final[str] = "не указан"
CHECK_CHANNEL_LANGUAGE_NOTE: Final[str] = (
    "Язык канала на YouTube — справочный, на решения планера он не влияет: "
    "язык стрима задаёт оператор в channels.yaml."
)
CHECK_NEEDS_AUTH: Final[str] = "- {key}: нужна авторизация — запустите: planer.bat --auth {key}"
CHECK_AUTHORIZING: Final[str] = "- {key}: токена нет, авторизую канал."
CHECK_CHANNEL_FAILED: Final[str] = "- {key}: {code} ({message})"
CHECK_ALL_OK: Final[str] = "Все каналы на месте, трансляции включены."
CHECK_HAS_PROBLEMS: Final[str] = "Часть каналов не прошла проверку — см. строки выше."
BINDINGS_NOT_VERIFIED: Final[str] = "Каналы не проверены, ничего не читалось и не записывалось."

# --- promo и пакеты (ТЗ §7.1)
PROMO_EMPTY: Final[str] = "В {path} нет пакетов *.bcast — сохраните туда пакет от оператора и запустите снова."
REPORT_WRITTEN: Final[str] = "Отчёт сохранён: {path}"
PACKAGE_ACCEPTED: Final[str] = "- {file} — принят, слотов {total}, из них под мои языки {mine}"
PACKAGE_DAMAGED: Final[str] = "- {file} — пакет повреждён: {reason}; файл не тронут"
PACKAGE_UNSUPPORTED_SCHEMA: Final[str] = (
    "- {file} — версия пакета {version} не поддерживается (нужна {supported}); файл не тронут"
)
PACKAGE_ALL_PAST: Final[str] = "- {file} — все слоты в прошлом, ничего из него не планируется"
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
OUTCOME_CREATE_PLANNED: Final[str] = "{prefix} — эфира нет, будет создан"
OUTCOME_FIXED: Final[str] = "{prefix} — на YouTube было другое {what}; обновлено. Ключ и ссылка прежние"
OUTCOME_FIX_PLANNED: Final[str] = "{prefix} — на YouTube другое {what}; будет обновлено"
OUTCOME_MATCHED: Final[str] = "{prefix} — {url}"
OUTCOME_NO_STREAM: Final[str] = (
    "{prefix} — эфир на канале есть ({url}), но к нему не привязан поток: ключ взять неоткуда. "
    "Привяжите поток в YouTube Studio или удалите эфир — планер создаст его заново"
)
OUTCOME_STREAM_ATTACHED: Final[str] = (
    "{prefix} — эфир был без потока, поток привязан, ключ получен, форма {form}"
)
WARNING_LINE: Final[str] = "- {prefix}: {step} — {code} ({message})"
# ключи — WARNING_STEP_* (app/pipeline/plan.py)
MISMATCH_LINE: Final[str] = "{prefix}: {field} — хотели: {wanted}; на платформе: {actual}"
MISMATCH_FIELD_TITLE: Final[str] = "название"
MISMATCH_FIELD_DESCRIPTION: Final[str] = "описание"
MISMATCH_FIELD_START: Final[str] = "время старта"
MISMATCH_FIELD_MARKER: Final[str] = "маркер потока"
MISMATCH_FIELD_LANGUAGE: Final[str] = "язык"
MISMATCH_FIELD_AUDIENCE: Final[str] = "аудитория"
MISMATCH_FIELD_CATEGORY: Final[str] = "категория"
WARNING_FORM_DIAGNOSTIC: Final[str] = (
    "- ответ формы сохранён для разбора: {path}"
)
WARNING_KEPT_KEY: Final[str] = (
    "- у совпавших и исправленных эфиров ключ прежний: планер не отправляет его в форму повторно, "
    "чтобы не задвоить ключ у стримера. Если стример ключа не получил — передайте его из keys.txt "
    "вручную или удалите эфир на YouTube: планер создаст его заново с новым ключом и отправит."
)
WARNING_LIVE_CHAT: Final[str] = (
    "- у эфиров включён живой чат. Через API он не отключается: если чат не нужен, "
    "выключите его один раз в Студии на весь канал (Settings → Community)."
)
MISMATCH_DESCRIPTION: Final[str] = "{length} символов, начало «{head}»"
AUDIENCE_NOT_FOR_KIDS: Final[str] = "не для детей"
AUDIENCE_FOR_KIDS: Final[str] = "для детей"
WARNING_STEP_TEXT: Final[dict[str, str]] = {
    "thumbnail": "обложка не поставлена (нужен подтверждённый канал); эфир и ключ в силе",
    "language": "язык эфира не записан; эфир и ключ в силе",
    "audience": "аудитория эфира была «для детей» (настройка канала) — планер снял её; проверьте настройки канала",
    "category": "категория эфира была другой — планер поставил ту, что в channels.yaml",
    "settings": "не удалось применить настройки эфира (язык, категория, аудитория); эфир и ключ в силе",
    "age_restricted": "на эфире стоит возрастное ограничение 18+; через API оно не снимается — снимите вручную в Студии",
    "facts": "не удалось перечитать эфир после планирования; на сам эфир это не влияет",
}
OUTCOME_AMBIGUOUS: Final[str] = (

    "{prefix} — на канале несколько эфиров на эту минуту без маркера планера, "
    "не могу различить — разберитесь вручную"
)
OUTCOME_ERROR: Final[str] = "{prefix} — {origin}: {code} ({message})"
OUTCOME_PLANER_ERROR: Final[str] = "{prefix} — {text}"
OUTCOME_DRY_RUN_SUFFIX: Final[str] = " — не выполнено (dry-run)"
FORM_MARK_SENT: Final[str] = "✅"
FORM_MARK_FAILED: Final[str] = (
    "❌ {reason}; повторно планер ключ не отправит — передайте его стримеру из keys.txt вручную"
)
# ключи — коды из app/form/base.py
FORM_REASON_TEXT: Final[dict[str, str]] = {
    "structureUnreadable": "не удалось прочитать форму ({detail})",
    "missingOption": "в форме нет нужного варианта ответа ({detail}) — попросите владельца формы добавить его",
    "requiredMissing": "в форме остались незаполненные обязательные вопросы ({detail})",
    "transportFailed": "форма недоступна ({detail})",
    "notConfirmed": "форма не подтвердила запись ответа ({detail})",
}
FORM_REASON_UNKNOWN: Final[str] = "отправка не удалась ({detail})"
# ключи — значения ChangedField (app/pipeline/reconciler.py)
CHANGED_FIELD_TEXT: Final[dict[str, str]] = {"title": "название", "description": "описание"}
CHANGED_FIELDS_JOINER: Final[str] = " и "
# ключи — OutcomeError.origin: значения Platform (app/config/loader.py) и источники планера
ERROR_ORIGIN_TEXT: Final[dict[str, str]] = {
    "youtube": "YouTube",
    "package": "пакет",
}
PLANER_ERROR_TEXT_ORIGINS: Final[frozenset[str]] = frozenset({"planer"})
PLANER_ERROR_TEXT: Final[dict[str, str]] = {
    "noBoundStream": "у найденного эфира нет привязанного потока — ключ получить нельзя; привяжите поток или удалите эфир",
    "keysWriteFailed": "файл ключей не записан: {detail}",
}
ORPHAN_LINE: Final[str] = "- {date} {time} {language} → {account_name} — {url} — эфир не удалён"
SCHEDULED_LINE: Final[str] = "{prefix} — {url}"

# --- отчёт (ТЗ §5.6)
REPORT_TITLE: Final[str] = "# Планер — отчёт {generated_at}"
REPORT_TITLE_DRY_RUN: Final[str] = " (dry-run)"
REPORT_NOTICE: Final[str] = "⚠ {notice}"
REPORT_SECTION_PACKAGES: Final[str] = "## Пакеты"
REPORT_SECTION_CREATED: Final[str] = "## Создано ({count})"
REPORT_SECTION_FIXED: Final[str] = "## Исправлено ({count})"
REPORT_SECTION_MATCHED: Final[str] = "## Уже запланировано, совпадает ({count})"
REPORT_SECTION_ORPHANS: Final[str] = "## Перенесён или отменён? ({count})"
REPORT_SECTION_SCHEDULED: Final[str] = "## Запланировано на каналах ({count})"
REPORT_SECTION_SKIPPED: Final[str] = "## Пропущено"
REPORT_SECTION_WARNINGS: Final[str] = "## Предупреждения"
REPORT_SECTION_MISMATCHES: Final[str] = "## Расхождения с платформой"
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
KEY_FORM_SENT: Final[str] = "ключ передан в форму {sent_at}"
KEY_FORM_KEPT: Final[str] = "ключ прежний, планер его не передавал"
KEY_FORM_FAILED: Final[str] = "не удалось передать: {reason}"
