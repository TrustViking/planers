"""Объект-форма: одна прочитанная форма ключей и правила ответа на неё (ТЗ §7.5 п.3–4).

KeyForm строится один раз на форму за запуск — из FormSpec пакета (названия вопросов, тексты вариантов,
формат даты) и FormStructure самой формы (вопросы, entry-ID, варианты, разделы). Поля заполняются при
создании и не пересчитываются. Ответ (answers) строится по значениям, а не по объекту эфира: язык, момент
старта слота, название канала, ключ, адрес потока. Ключа и адреса ещё нет — эти поля «ожидаются после
публикации», это не ошибка.

Железное правило: значение, которого нет среди вариантов вопроса, в ответ не попадает — поле незаполнено
(missingOption). Времени в форме нет — только дата (время указано в объявлении).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import date, datetime
from typing import Final

from app.core.dates import format_date
from app.form.base import FORM_CODE_MISSING_OPTION, FORM_CODE_REQUIRED_MISSING, FormError
from app.form.discovery import FormQuestion, FormStructure, QuestionKind, SectionJump
from app.observability.logging_setup import get_logger
from app.package.model import FormSpec

LOGGER = get_logger("form.key_form")

# Поля пакета (form.fields), на которые форма отвечает, — в этом порядке идут ответы.
FIELD_LANGUAGE: Final[str] = "language"
FIELD_ACCOUNT_NAME: Final[str] = "account_name"
FIELD_DATE: Final[str] = "date"
FIELD_PLATFORM: Final[str] = "platform"
FIELD_STREAM_KEY: Final[str] = "stream_key"
FIELD_STREAM_URL: Final[str] = "stream_url"
ANSWER_FIELDS: Final[frozenset[str]] = frozenset(
    {FIELD_LANGUAGE, FIELD_ACCOUNT_NAME, FIELD_DATE, FIELD_PLATFORM, FIELD_STREAM_KEY, FIELD_STREAM_URL}
)
# Эти поля известны только после публикации эфира.
PUBLISHED_FIELDS: Final[frozenset[str]] = frozenset({FIELD_STREAM_KEY, FIELD_STREAM_URL})
PLATFORM_CODE: Final[str] = "youtube"
FIRST_PAGE: Final[int] = 0
MISSING_VALUE: Final[str] = "-"
TITLE_JOINER: Final[str] = ", "
DATES_JOINER: Final[str] = ", "
# Образец даты для длины префикса варианта «Время стрима»: две цифры в каждой части.
DATE_SAMPLE: Final[datetime] = datetime(2000, 12, 28)


@dataclass(frozen=True)
class FormAnswer:
    question: FormQuestion
    value: str


@dataclass(frozen=True)
class MissingAnswer:
    """Незаполненное поле: код исхода, текст «вопрос: значение» для лога и отказа отправки,
    вопрос и значение по отдельности — для текста владельцу.
    """

    field: str
    code: str
    text: str
    question: str = ""     # название вопроса формы (requiredMissing — названия через TITLE_JOINER)
    value: str = ""        # вариант, которого нет; MISSING_VALUE — пакет не дал текста варианта


@dataclass(frozen=True)
class FormAnswers:
    """Ответ формы для одного эфира: что отправить, какие разделы пройти, чего не хватает."""

    answers: tuple[FormAnswer, ...]
    pages: tuple[int, ...]
    missing: tuple[MissingAnswer, ...]   # missingOption по полям, затем requiredMissing
    pending: tuple[str, ...]             # поля, которые ожидаются после публикации (ключ, адрес потока)

    @property
    def is_complete(self) -> bool:
        return not self.missing and not self.pending

    def as_record(self) -> tuple[tuple[str, str, str], ...]:
        """Канонический вид для сравнения и памяти планера: (entry_id, вопрос, значение) по порядку ответов."""
        return tuple((answer.question.entry_id, answer.question.title, answer.value) for answer in self.answers)

    def error(self) -> FormError | None:
        """Первая причина не отправлять — та же, что давала отправка до объекта-формы."""
        if self.missing:
            return FormError(self.missing[0].code, self.missing[0].text)
        if self.pending:
            return FormError(FORM_CODE_REQUIRED_MISSING, TITLE_JOINER.join(self.pending))
        return None


@dataclass(frozen=True)
class DateCoverage:
    """Покрывает ли вопрос «Время стрима» даты запуска — проверка до входов и до площадки.

    Даты сопоставляются тем же кодом, что и при отправке (KeyForm._date_option): что здесь названо
    недостающим, то при допуске даст missingOption, и наоборот.
    """

    form_url: str
    question_title: str                 # пусто — вопроса даты нет в пакете или в форме
    is_checkable: bool                  # вопрос есть и он с вариантами (не текст)
    wanted: tuple[str, ...]             # даты запуска в date_format формы, по возрастанию, без повторов
    missing: tuple[str, ...]            # из wanted — без варианта в форме, тот же порядок
    missing_dates: tuple[date, ...]     # те же даты объектами date, тот же порядок
    accepted_count: int                 # сколько дат форма принимает (len(accepted_dates))

    @property
    def is_complete(self) -> bool:
        return not self.missing

    @property
    def missing_text(self) -> str:
        """Недостающие даты для владельца — в формате планера DD-MM-YYYY."""
        return DATES_JOINER.join(format_date(value) for value in self.missing_dates)


@dataclass(frozen=True)
class KeyForm:
    """Форма ключей: поля пакета → найденные вопросы; варианты — у самих вопросов."""

    url: str                                        # form.url пакета, как пришёл
    spec: FormSpec
    structure: FormStructure
    questions: Mapping[str, FormQuestion | None]    # поле пакета → вопрос формы; None — нет в пакете или в форме
    # адреса форм, для которых строка form_pages_by_navigation уже выдана: структура за запуск одна
    navigation_logged_urls: set[str] = dataclass_field(default_factory=set, compare=False, repr=False)

    @classmethod
    def build(cls, spec: FormSpec, structure: FormStructure) -> KeyForm:
        questions: dict[str, FormQuestion | None] = {}
        for field, title in spec.fields.items():
            if field in ANSWER_FIELDS:
                questions[field] = structure.question_by_title(title) if title is not None else None
        return cls(url=spec.url, spec=spec, structure=structure, questions=questions)

    def for_spec(self, spec: FormSpec) -> KeyForm:
        """Та же прочитанная форма для другого пакета с той же ссылкой: свои названия и варианты,
        а отметка «строка разделов уже выдана» — общая, строка остаётся одной на форму за запуск.
        """
        rebuilt: KeyForm = KeyForm.build(spec, self.structure)
        return replace(rebuilt, navigation_logged_urls=self.navigation_logged_urls)

    @property
    def accepted_dates(self) -> tuple[str, ...]:
        """Даты, на которые в форме есть вариант «Время стрима», — в date_format пакета."""
        question: FormQuestion | None = self.questions.get(FIELD_DATE)
        if question is None or question.kind is QuestionKind.TEXT:
            return ()
        width: int = len(DATE_SAMPLE.strftime(self.spec.date_format))
        return tuple(option[:width] for option in question.options if self._is_date(option[:width]))

    @property
    def accepted_languages(self) -> tuple[str, ...]:
        """Коды языков пакета, для которых в форме есть вариант."""
        question: FormQuestion | None = self.questions.get(FIELD_LANGUAGE)
        if question is None:
            return ()
        texts: Mapping[str, str] = self.spec.values.get(FIELD_LANGUAGE, {})
        return tuple(
            code for code, text in texts.items() if question.kind is QuestionKind.TEXT or text in question.options
        )

    @property
    def stream_url_options(self) -> tuple[str, ...]:
        question: FormQuestion | None = self.questions.get(FIELD_STREAM_URL)
        return question.options if question is not None else ()

    def date_coverage(self, starts: Iterable[datetime]) -> DateCoverage:
        """Есть ли в форме вариант на каждую дату этих стартов; сеть не нужна — форма уже прочитана."""
        question: FormQuestion | None = self.questions.get(FIELD_DATE)
        if question is None or question.kind is QuestionKind.TEXT:
            return DateCoverage(
                form_url=self.url,
                question_title=question.title if question is not None else "",
                is_checkable=False,
                wanted=(),
                missing=(),
                missing_dates=(),
                accepted_count=len(self.accepted_dates),
            )
        by_date: dict[date, str] = {start.date(): start.strftime(self.spec.date_format) for start in starts}
        ordered: list[date] = sorted(by_date)
        absent: list[date] = [day for day in ordered if self._date_option(question, by_date[day]) is None]
        return DateCoverage(
            form_url=self.url,
            question_title=question.title,
            is_checkable=True,
            wanted=tuple(by_date[day] for day in ordered),
            missing=tuple(by_date[day] for day in absent),
            missing_dates=tuple(absent),
            accepted_count=len(self.accepted_dates),
        )

    def log_ready(self) -> None:
        LOGGER.info(
            "form_ready url=%s questions=%d pages=%d dates=%s languages=%s stream_urls=%d",
            self.structure.view_url,
            len(self.structure.questions),
            self.structure.page_count,
            ",".join(self.accepted_dates) or MISSING_VALUE,
            ",".join(self.accepted_languages) or MISSING_VALUE,
            len(self.stream_url_options),
        )

    def answers(
        self,
        *,
        language: str,
        start: datetime,
        account_name: str,
        stream_key: str | None,
        stream_url: str | None,
    ) -> FormAnswers:
        """Ответ на форму по значениям эфира; ключ и адрес None — «ожидаются после публикации»."""
        values: dict[str, str | None] = {
            FIELD_ACCOUNT_NAME: account_name,
            FIELD_STREAM_KEY: stream_key,
            FIELD_STREAM_URL: stream_url,
        }
        answers: list[FormAnswer] = []
        missing: list[MissingAnswer] = []
        pending: list[FormQuestion] = []
        unanswerable: list[FormQuestion] = []   # вопросы с незаполненным полем: в requiredMissing не повторяются
        for field, question in self.questions.items():
            if question is None:
                continue            # вопроса нет в пакете или в форме: обязательность проверит _required_missing
            if field in PUBLISHED_FIELDS and values[field] is None:
                pending.append(question)
                continue
            value, wanted = self._value_for(field, question, language, start, values)
            if wanted is not None:
                missing.append(_missing_option(field, question, wanted))
                unanswerable.append(question)
            elif value is not None:
                answers.append(FormAnswer(question, value))
        pages: list[int] = self._visited_pages(answers)
        required: MissingAnswer | None = self._required_missing(answers, pages, [*pending, *unanswerable])
        if required is not None:
            missing.append(required)
        return FormAnswers(tuple(answers), tuple(pages), tuple(missing), tuple(item.title for item in pending))

    def _value_for(
        self,
        field: str,
        question: FormQuestion,
        language: str,
        start: datetime,
        values: Mapping[str, str | None],
    ) -> tuple[str | None, str | None]:
        """(значение, вариант, которого в форме нет); оба None — поле не отправляется."""
        if field in (FIELD_ACCOUNT_NAME, FIELD_STREAM_KEY):
            return values[field], None
        if field == FIELD_LANGUAGE:
            return self._option_by_text(question, self.spec.values.get(FIELD_LANGUAGE, {}).get(language))
        if field == FIELD_PLATFORM:
            return self._option_by_text(question, self.spec.values.get(FIELD_PLATFORM, {}).get(PLATFORM_CODE))
        if field == FIELD_DATE:
            return self._option_by_date(question, start)
        return self._option_by_url(question, values[FIELD_STREAM_URL] or "")

    @staticmethod
    def _option_by_text(question: FormQuestion, wanted: str | None) -> tuple[str | None, str | None]:
        if wanted is None:
            return None, MISSING_VALUE
        if question.kind is QuestionKind.TEXT or wanted in question.options:
            return wanted, None
        return None, wanted

    def _option_by_date(self, question: FormQuestion, start: datetime) -> tuple[str | None, str | None]:
        """Вариант «13.09.2026 Дата стрима …» опознаётся по началу текста (§7.5 п.3)."""
        wanted: str = start.strftime(self.spec.date_format)
        if question.kind is QuestionKind.TEXT:
            return wanted, None
        option: str | None = self._date_option(question, wanted)
        if option is not None:
            return option, None
        return None, wanted

    @staticmethod
    def _date_option(question: FormQuestion, wanted: str) -> str | None:
        """Первый вариант, начинающийся с даты; один код и для отправки, и для предстартовой проверки."""
        for option in question.options:
            if option.startswith(wanted):
                return option
        return None

    @staticmethod
    def _option_by_url(question: FormQuestion, stream_url: str) -> tuple[str | None, str | None]:
        """Сравнение нормализованное, отправляется текст варианта как он есть в форме."""
        if not stream_url:
            return None, MISSING_VALUE
        if question.kind is QuestionKind.TEXT:
            return stream_url, None
        wanted: str = _normalize_url(stream_url)
        for option in question.options:
            if _normalize_url(option) == wanted:
                return option, None
        return None, stream_url

    def _visited_pages(self, answers: list[FormAnswer]) -> list[int]:
        """Раздел вопроса «Платформа» и раздел, куда ведёт выбранный вариант (§7.5 п.4)."""
        fork: FormAnswer | None = self._fork_answer(answers)
        jump: SectionJump | None = self._jump_for(fork)
        pages: list[int] = [FIRST_PAGE]
        if fork is not None and jump is not None and self._is_page_in_range(jump.page_index):
            self._append_page(pages, fork.question.page_index)
            self._append_page(pages, jump.page_index)
            self._log_navigation(pages, fork, jump)
            return pages
        for answer in answers:
            self._append_page(pages, answer.question.page_index)
        LOGGER.info(
            "form_pages_by_answers pages=%s section_id=%s page=%s page_count=%d",
            pages,
            jump.section_id if jump is not None else None,
            jump.page_index if jump is not None else None,
            self.structure.page_count,
        )
        return pages

    def _log_navigation(self, pages: list[int], fork: FormAnswer, jump: SectionJump) -> None:
        """Разделы по переходу — DEBUG, один раз на форму за запуск: у всех ответов одной формы они одни."""
        if self.url in self.navigation_logged_urls:
            return
        self.navigation_logged_urls.add(self.url)
        LOGGER.debug(
            "form_pages_by_navigation pages=%s entry=%s option=%r section_id=%d page=%s page_count=%d",
            pages,
            fork.question.entry_id,
            fork.value,
            jump.section_id,
            jump.page_index,
            self.structure.page_count,
        )

    def _fork_answer(self, answers: list[FormAnswer]) -> FormAnswer | None:
        """Вопрос-развилка — тот, у которого есть переходы по вариантам."""
        for answer in answers:
            if answer.question.entry_id in self.structure.navigation:
                return answer
        return None

    def _jump_for(self, fork: FormAnswer | None) -> SectionJump | None:
        if fork is None:
            return None
        return self.structure.navigation.get(fork.question.entry_id, {}).get(fork.value)

    def _is_page_in_range(self, page: int | None) -> bool:
        """pageHistory принимает только номера существующих разделов: всё прочее Google отвергает 400."""
        return page is not None and 0 <= page < self.structure.page_count

    def _append_page(self, pages: list[int], page: int) -> None:
        if not self._is_page_in_range(page):
            LOGGER.warning("form_page_out_of_range page=%s page_count=%d", page, self.structure.page_count)
            return
        if page not in pages:
            pages.append(page)

    def _required_missing(
        self,
        answers: list[FormAnswer],
        pages: list[int],
        skipped: list[FormQuestion],
    ) -> MissingAnswer | None:
        """Обязательные вопросы пройденных разделов без ответа; ожидаемые и уже отмеченные — не повторяются.

        Обязательные вопросы непройденных разделов (Facebook, Rumble) не проверяются.
        """
        answered: set[str] = {answer.question.entry_id for answer in answers}
        answered.update(question.entry_id for question in skipped)
        titles: list[str] = [
            question.title
            for question in self.structure.questions
            if question.is_required and question.page_index in pages and question.entry_id not in answered
        ]
        if not titles:
            return None
        joined: str = TITLE_JOINER.join(titles)
        return MissingAnswer("", FORM_CODE_REQUIRED_MISSING, joined, question=joined)

    def _is_date(self, text: str) -> bool:
        try:
            datetime.strptime(text, self.spec.date_format)
        except ValueError:
            return False
        return True


def _normalize_url(value: str) -> str:
    return value.strip().rstrip("/").lower()


def _missing_option(field: str, question: FormQuestion, wanted: str) -> MissingAnswer:
    return MissingAnswer(
        field, FORM_CODE_MISSING_OPTION, f"{question.title}: {wanted}", question=question.title, value=wanted
    )
