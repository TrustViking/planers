"""Все тексты для владельца: консоль, отчёт, файл ключей (ТЗ §5.5, §5.6). В коде — только эти константы."""
from __future__ import annotations

from typing import Final

# --- командная строка (ТЗ §7.6)
CLI_DESCRIPTION: Final[str] = "Планер: регистрация трансляций по пакетам броадкастера."
HELP_DRY_RUN: Final[str] = "прочитать пакеты и показать, что было бы сделано; ничего не создавать, не переносить и не удалять"
HELP_CHECK: Final[str] = "проверить авторизацию и права каждого канала"
HELP_AUTH: Final[str] = "заново авторизовать канал (account_name из channels.json) или all — все каналы"
HELP_STATUS: Final[str] = "сверка и отчёт без пакетов из bcast"
HELP_DEBUG: Final[str] = "подробный лог в консоли"
HELP_VERSION: Final[str] = "показать номер версии и выйти"
VERSION_TEXT: Final[str] = "Планер {version}"

# --- конфиг (ТЗ §5.2): два JSON, все поля обязательные, примеров и копирования нет
CONFIG_ERROR: Final[str] = "Ошибка в конфиге {path}: {key} — {problem}"
CONFIG_ROOT_KEY: Final[str] = "(корень файла)"
CONFIG_CHANNELS_HINT: Final[str] = "Создайте файл {path} с таким содержимым и впишите свои значения:"
CONFIG_PLANER_HINT: Final[str] = "Восстановите файл {path} с таким содержимым и впишите свои значения:"
# Точные шаблоны файлов для консоли: печатаются, когда файла или поля нет. В код как умолчания не идут.
CONFIG_CHANNELS_TEMPLATE: Final[str] = """{
  "channels": [
    {"platform": "youtube", "account_name": "Osvald.X", "google_account": "you@gmail.com",
     "languages": ["ru"], "privacy": "unlisted"}
  ]
}"""
CONFIG_PLANER_TEMPLATE: Final[str] = """{"min_lead_minutes": 60, "keep_days": 30,
 "auto_start": true, "set_thumbnail": true, "category_id": "22"}"""
CONFIG_PROBLEM_FILE_MISSING: Final[str] = "файла нет"
CONFIG_PROBLEM_JSON: Final[str] = "файл не читается как JSON: {error}"
CONFIG_PROBLEM_NOT_MAPPING: Final[str] = "нужен объект JSON в фигурных скобках"
CONFIG_PROBLEM_MISSING_KEY: Final[str] = "обязательное поле отсутствует"
CONFIG_PROBLEM_UNKNOWN_KEY: Final[str] = "неизвестное поле"
CONFIG_PROBLEM_DUPLICATE_KEY: Final[str] = "поле указано дважды"
CONFIG_PROBLEM_NON_EMPTY_STRING: Final[str] = "нужна непустая строка в кавычках"
CONFIG_PROBLEM_INT_MIN: Final[str] = "нужно целое число не меньше {minimum}"
CONFIG_PROBLEM_BOOL: Final[str] = "нужно true или false"
CONFIG_PROBLEM_CHANNELS_EMPTY: Final[str] = "нужен непустой список каналов"
CONFIG_PROBLEM_ACCOUNT_NAME_CHAR: Final[str] = (
    "символ «{char}» в имени канала недопустим: имя канала — это имя файла токена, "
    r'в нём нельзя \ / : * ? " < > |'
)
CONFIG_PROBLEM_ACCOUNT_NAME_TOO_LONG: Final[str] = (
    "имя канала длиннее {maximum} символов (сейчас {length}): оно становится именем файла токена"
)
CONFIG_PROBLEM_ACCOUNT_NAME_CONTROL: Final[str] = "в имени канала есть управляющий символ (перевод строки, табуляция)"
CONFIG_PROBLEM_ACCOUNT_NAME_EDGE: Final[str] = "имя канала начинается или заканчивается пробелом или точкой: «{value}»"
CONFIG_PROBLEM_ACCOUNT_NAME_RESERVED: Final[str] = "«{value}» — зарезервированное имя Windows, файлом его назвать нельзя"
CONFIG_PROBLEM_ACCOUNT_NAME_AUTH_ALL: Final[str] = (
    "имя «{value}» занято режимом --auth {auth_all} (вход во все каналы сразу); назовите канал иначе"
)
CONFIG_PROBLEM_GOOGLE_ACCOUNT: Final[str] = (
    "«{value}» не похоже на почту аккаунта Google: нужен вид имя@домен, ровно один @ и без пробелов"
)
CONFIG_PROBLEM_ACCOUNT_NAME_DUPLICATE: Final[str] = (
    "имя «{value}» уже есть у другого канала (большие и маленькие буквы не различаются: это имя файла)"
)
CONFIG_PROBLEM_PLATFORM_FACEBOOK: Final[str] = "Facebook появится на этапе 6; сейчас поддерживается только youtube"
CONFIG_PROBLEM_PLATFORM_UNKNOWN: Final[str] = "неизвестная площадка «{value}»; допустимо: {allowed}"
CONFIG_PROBLEM_LANGUAGES: Final[str] = "нужен непустой список кодов языков строчными буквами, например [\"uk\"]"
CONFIG_PROBLEM_LANGUAGE_DUPLICATE: Final[str] = "язык «{value}» указан дважды"
CONFIG_PROBLEM_CHOICE: Final[str] = "допустимо: {allowed}"

# --- авторизация и каналы (ТЗ §5.3)
CLIENT_SECRET_MISSING: Final[str] = (
    "Нет файла {path} — без него планер не может обратиться к YouTube. "
    "Возьмите его у оператора и положите рядом с программой, в папку secrets."
)
CHANNELS_STATE_UNREADABLE: Final[str] = "Файл привязок каналов {path} не читается: {error}. Ничего не делалось."
AUTH_UNKNOWN_CHANNEL: Final[str] = "В {path} нет канала «{account_name}». Каналы в файле: {known}."
AUTH_STARTING: Final[str] = "Канал «{account_name}»: нужен вход в Google — сейчас откроется браузер."
AUTH_UNVERIFIED_APP_WARNING: Final[str] = (
    "Google покажет предупреждение «Google hasn't verified this app» — это ожидаемо, "
    "приложение ещё не проходило проверку Google. Нажмите Advanced, затем ссылку "
    "Go to ... (unsafe), затем Continue. На экране согласия должен быть пункт про управление "
    "вашим аккаунтом YouTube — отметьте его и подтвердите."
)
AUTH_CHOOSE_ACCOUNT: Final[str] = "Войдите в аккаунт Google {google_account} — это аккаунт канала «{account_name}»."
AUTH_CHOOSE_RIGHT_CHANNEL: Final[str] = (
    "Если в этом аккаунте несколько каналов, выберите тот, что в channels.json записан как «{account_name}»."
)
AUTH_OK: Final[str] = "Канал «{account_name}»: вход выполнен — {title} (id {youtube_channel_id})."
AUTH_BINDING_SAVED: Final[str] = "Привязка канала «{account_name}» записана в {path}."
AUTH_BINDING_MISMATCH: Final[str] = (
    "канал «{account_name}» уже привязан к YouTube-каналу {expected_title} (id {expected_id}), "
    "а токен ведёт на {actual_title} (id {actual_id}); ничего не переписано. "
    "Войдите заново и выберите правильный канал: planer.bat --auth \"{account_name}\""
)
AUTH_BINDING_TAKEN: Final[str] = (
    "токен канала «{account_name}» ведёт на YouTube-канал {actual_title} (id {actual_id}), "
    "а он уже записан под именем «{other_account_name}»; ничего не переписано. "
    "Один YouTube-канал — одно имя в channels.json: войдите заново и выберите правильный канал "
    "или исправьте account_name"
)
AUTH_FAILED: Final[str] = "Канал «{account_name}»: вход не удался — {reason}."
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
    "- {account_name}: {title} (id {youtube_channel_id}), язык канала на YouTube: {channel_language}; "
    "языки стримов из channels.json: {languages}; запланированных эфиров: {upcoming}"
)
CHECK_CHANNEL_LANGUAGE_UNSET: Final[str] = "не указан"
CHECK_CHANNEL_LANGUAGE_NOTE: Final[str] = (
    "Язык канала на YouTube — справочный, на решения планера он не влияет: "
    "язык стрима задаёт оператор в channels.json."
)
CHECK_CHANNEL_FAILED: Final[str] = "- {account_name}: {code} ({message})"
CHECK_CHANNEL_REFUSED: Final[str] = "- {message}"
CHANNEL_NAME_QUOTED: Final[str] = "«{account_name}»"
CHECK_ALL_OK: Final[str] = "Все каналы на месте, трансляции включены."
CHECK_HAS_PROBLEMS: Final[str] = "Часть каналов не прошла проверку — см. строки выше."

# --- bcast и пакеты (ТЗ §7.1)
BCAST_EMPTY: Final[str] = "В {path} нет пакетов *.bcast — сохраните туда пакет от оператора и запустите снова."
PACKAGE_ACCEPTED: Final[str] = "{file} — принят, слотов {total}, из них под мои языки {mine}"
PACKAGE_DAMAGED: Final[str] = "{file} — пакет повреждён: {reason}; файл не тронут"
PACKAGE_UNSUPPORTED_SCHEMA: Final[str] = (
    "{file} — версия пакета {version} не поддерживается (нужна {supported}); файл не тронут"
)
PACKAGE_ALL_PAST: Final[str] = "{file} — все слоты в прошлом, ничего из него не планируется"
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
SKIP_PAST: Final[str] = "{date} {time} {language} — уже прошло"
SKIP_TOO_LATE: Final[str] = "{date} {time} {language} — до старта меньше {minutes} минут"
SKIP_NO_CHANNEL: Final[str] = "{date} {time} {language} — нет канала для языка {language}"

# --- исходы пар (ТЗ §5.6, §7.3)
OUTCOME_SLOT_PREFIX: Final[str] = "{date} {time} {language} -> {account_name}"
OUTCOME_CHANNEL_PREFIX: Final[str] = "{account_name}"
OUTCOME_CREATED: Final[str] = "{prefix} — эфир создан, {form}"
OUTCOME_CREATE_PLANNED: Final[str] = "{prefix} — эфира нет, будет создан"
OUTCOME_FIXED: Final[str] = "{prefix} — на YouTube отличалось: {what}; исправлено, ключ и ссылка прежние{form}"
OUTCOME_FIXED_FORM: Final[str] = ", {mark}"
OUTCOME_FIX_PLANNED: Final[str] = "{prefix} — на YouTube отличается: {what}; будет исправлено, ключ уйдёт в форму"
OUTCOME_MATCHED: Final[str] = "{prefix} — {url}"
OUTCOME_NO_STREAM: Final[str] = (
    "{prefix} — эфир на канале есть ({url}), но к нему не привязан поток: ключ взять неоткуда. "
    "Привяжите поток в YouTube Studio или удалите эфир — планер создаст его заново"
)
OUTCOME_STREAM_ATTACHED: Final[str] = (
    "{prefix} — эфир был без потока, поток привязан, {form}"
)
WARNING_LINE: Final[str] = "{prefix}: {step} — {code} ({message})"
# ключи — WARNING_STEP_* (app/pipeline/plan.py)
MISMATCH_LINE: Final[str] = "{prefix}: {field} — хотели: {wanted}; на платформе: {actual}"
# поля спеки называются по CHANGED_FIELD_TEXT; здесь — только то, чего в спеке нет
MISMATCH_FIELD_START: Final[str] = "время старта"
MISMATCH_FIELD_LANGUAGE: Final[str] = "язык"
MISMATCH_FIELD_AUDIENCE: Final[str] = "аудитория"
SPEC_VALUE_TRUE: Final[str] = "да"
SPEC_VALUE_FALSE: Final[str] = "нет"
WARNING_REPORTED_FIELD: Final[str] = (
    "{prefix}: {field} — хотели: {wanted}; на YouTube: {actual}. Через API это не исправляется "
    "(нужен monitorStream) — поправьте в Студии; эфир и ключ в силе"
)
WARNING_AMBIGUOUS: Final[str] = (
    "{prefix}: на канале несколько эфиров без метки планера на эту минуту — планер не выбирает и не удаляет; "
    "оставьте один: {urls}"
)
AMBIGUOUS_URL_JOINER: Final[str] = ", "
# Описание ключа потока в Студии (Создать -> Управление ключами трансляции); зрителям не видно.
STREAM_DESCRIPTION: Final[str] = (
    "Ключ планера: канал «{account_name}», эфир {date} {time}, язык {language}; записано планером {written_at}"
)
WARNING_FORM_DIAGNOSTIC: Final[str] = "ответ формы сохранён для разбора: {path}"
# Постоянные особенности площадки — только в отчёте, разделом «Особенности площадки» (ТЗ §5.6).
WARNING_KEPT_KEY: Final[str] = (
    "эфир с меткой планера совпал с пакетом — его ключ уже уходил в форму раньше и повторно не отправляется. "
    "Если стример ключа не получил — передайте его из keys.txt вручную или удалите эфир на YouTube: "
    "планер создаст его заново с новым ключом и отправит"
)
WARNING_LIVE_CHAT: Final[str] = (
    "у эфиров YouTube всегда включён живой чат. Через API он не отключается: если чат не нужен, "
    "выключите его один раз в Студии на весь канал (Settings -> Community)"
)
MISMATCH_DESCRIPTION: Final[str] = "{length} символов, начало «{head}»"
AUDIENCE_NOT_FOR_KIDS: Final[str] = "не для детей"
AUDIENCE_FOR_KIDS: Final[str] = "для детей"
WARNING_STEP_TEXT: Final[dict[str, str]] = {
    "thumbnail": "обложка не поставлена (нужен подтверждённый канал); эфир и ключ в силе",
    "language": "язык эфира не записан; эфир и ключ в силе",
    "audience": "аудитория эфира была «для детей» (настройка канала) — планер снял её; проверьте настройки канала",
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
FORM_MARK_SENT: Final[str] = "ключ передан в форму"
FORM_MARK_FAILED: Final[str] = (
    "ключ в форму НЕ передан — {reason}; повторно планер его не отправит, передайте ключ стримеру из keys.txt вручную"
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
# ключи — значения ChangedField (app/pipeline/plan.py)
CHANGED_FIELD_TEXT: Final[dict[str, str]] = {
    "title": "название",
    "description": "описание",
    "category": "категория",
    "privacy": "видимость",
    "marker": "маркер потока",
    "auto_start": "автостарт",
    "auto_stop": "автостоп",
    "latency": "задержка трансляции",
}
CHANGED_FIELDS_JOINER: Final[str] = ", "
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
ORPHAN_LINE: Final[str] = "{date} {time} {language} -> {account_name} — {url} — эфир не удалён"
SCHEDULED_LINE: Final[str] = "{prefix} — {url}"

# --- отчёт logs\{дата}_{время}_report.md (ТЗ §5.6): подробности, можно переслать оператору
REPORT_TITLE: Final[str] = "# Планер {version} — отчёт {generated_at}"
REPORT_TITLE_DRY_RUN: Final[str] = " (dry-run)"
REPORT_NOTICE: Final[str] = "Внимание: {notice}"
REPORT_ITEM: Final[str] = "- {text}"
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
REPORT_SECTION_NOT_DELIVERED: Final[str] = "## Ключ не дошёл до стримера"
REPORT_SECTION_NOTES: Final[str] = "## Особенности площадки — так устроена площадка, это не про этот запуск"
NOT_DELIVERED_LINE: Final[str] = "{prefix} — {reason}; эфир на канале стоит — передайте ключ стримеру из keys.txt вручную"
REPORT_TOTAL: Final[str] = (
    "Итог: создано {created}, исправлено {fixed}, совпадает {matched}, пропущено {skipped}, "
    "ошибок {errors}.{keys_file}"
)
REPORT_STATUS_TOTAL: Final[str] = "Итог: запланировано {scheduled}, ошибок {errors}.{keys_file}"
REPORT_TOTAL_KEYS_FILE: Final[str] = " Файл ключей: {path}"

# --- консоль (ТЗ §5.6): что произошло и куда смотреть; без markdown и значков
CONSOLE_TITLE: Final[str] = "Планер — {generated_at}"
CONSOLE_TITLE_DRY_RUN: Final[str] = "Планер — {generated_at} — dry-run: ничего не создано и в форму не отправлено"
CONSOLE_TITLE_STATUS: Final[str] = "Планер — {generated_at} — --status: эфиры планера на каналах"
CONSOLE_INDENT: Final[str] = "  "
CONSOLE_LABEL_WIDTH: Final[int] = 14
CONSOLE_COUNT_WIDTH: Final[int] = 3
CONSOLE_COLUMN_GAP: Final[str] = "   "
CONSOLE_COUNTER: Final[str] = (
    CONSOLE_INDENT + "{label:<" + str(CONSOLE_LABEL_WIDTH) + "}{count:>" + str(CONSOLE_COUNT_WIDTH) + "}"
    + CONSOLE_COLUMN_GAP + "{detail}"
)
# строка исхода под счётчиком — ровно в колонке подробностей CONSOLE_COUNTER
CONSOLE_OUTCOME_INDENT: Final[str] = " " * (
    len(CONSOLE_INDENT) + CONSOLE_LABEL_WIDTH + CONSOLE_COUNT_WIDTH + len(CONSOLE_COLUMN_GAP)
)
CONSOLE_OUTCOME: Final[str] = CONSOLE_OUTCOME_INDENT + "{detail}"
CONSOLE_LABEL_PACKAGES: Final[str] = "пакеты"
CONSOLE_LABEL_CREATED: Final[str] = "создано"
CONSOLE_LABEL_FIXED: Final[str] = "исправлено"
CONSOLE_LABEL_MATCHED: Final[str] = "совпадает"
CONSOLE_LABEL_CREATE_PLANNED: Final[str] = "создать"
CONSOLE_LABEL_FIX_PLANNED: Final[str] = "исправить"
CONSOLE_LABEL_SKIPPED: Final[str] = "пропущено"
CONSOLE_LABEL_SCHEDULED: Final[str] = "запланировано"
CONSOLE_LABEL_ERRORS: Final[str] = "ошибок"
CONSOLE_DETAIL_JOINER: Final[str] = ", "
CONSOLE_SKIP_JOINER: Final[str] = "; "
CONSOLE_PACKAGES_DETAIL: Final[str] = "слотов {total}, моих {mine}"
CONSOLE_PACKAGES_UNREADABLE: Final[str] = "не прочитано {count}"
CONSOLE_FORMS_SENT: Final[str] = "ключ передан в форму: {sent} из {total}"
CONSOLE_FORM_NOT_SENT: Final[str] = "ключ в форму НЕ передан"
CONSOLE_FIXED_DETAIL: Final[str] = "{prefix}, обновлено: {what}"
CONSOLE_FIX_PLANNED_DETAIL: Final[str] = "{prefix}, будет обновлено: {what}"
CONSOLE_SKIP_PAST: Final[str] = "уже прошло: {count}"
CONSOLE_SKIP_TOO_LATE: Final[str] = "до старта меньше {minutes} минут: {count}"
CONSOLE_SKIP_NO_CHANNEL: Final[str] = "нет канала: {languages}"
CONSOLE_SKIP_LANGUAGE_COUNT: Final[str] = "{language} ({count})"
CONSOLE_ERROR: Final[str] = "  ошибка: {text}"
CONSOLE_FORM_ERROR: Final[str] = "  форма: {prefix} — {mark}"
CONSOLE_PACKAGE_ERROR: Final[str] = "  пакет: {text}"
CONSOLE_WARNING: Final[str] = "  внимание: {text}"
CONSOLE_PATH: Final[str] = "  {label:<8}{path}"
CONSOLE_LABEL_KEYS: Final[str] = "ключи"
CONSOLE_LABEL_REPORT: Final[str] = "отчёт"
CONSOLE_LABEL_LOG: Final[str] = "лог"

# --- файл ключей (ТЗ §5.5): блок на стрим, ключ — первой строкой блока
KEYS_FILE_HEADER: Final[tuple[str, ...]] = (
    "# Ключи трансляций. Сгенерировано планером {generated_at}.",
    "# Файл перезаписывается на каждом запуске — не править.",
    "# Строка «форма»: «отправлен в форму» — ключ у стримера; «в этом запуске в форму не отправлялся» — "
    "эфир с меткой планера совпал, ключ уходил раньше; «НЕ отправлен» — передайте ключ стримеру вручную.",
)
KEYS_BLOCK_TITLE: Final[str] = "{date} {time}  {language}  {account_name}"
KEYS_BLOCK_KEY: Final[str] = "  ключ   {value}"
KEYS_BLOCK_STREAM: Final[str] = "  поток  {value}"
KEYS_BLOCK_BROADCAST: Final[str] = "  эфир   {value}"
KEYS_BLOCK_FORM: Final[str] = "  форма  {value}"
KEY_FORM_SENT: Final[str] = "отправлен в форму {sent_at}"
KEY_FORM_KEPT: Final[str] = "в этом запуске в форму не отправлялся: эфир уже стоял с этим ключом"
KEY_FORM_FAILED: Final[str] = "НЕ отправлен: {reason} — передайте стримеру вручную"
