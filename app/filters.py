import re

QUESTION_START_RE = re.compile(
    r"^\s*(а\s+кто|кто[\s-]?нибудь|кто\s+нибудь|подскажите|посоветуйте|"
    r"где\s+найти|есть\s+ли|кто\s+знает|никто\s+не)",
)


class TokenIndex:
    """Индекс токенов текста для проверки близости (в токенах) HF-3/HF-5."""

    def __init__(self, text: str):
        self.text = text
        self._tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]

    def token_index_at(self, char_pos: int) -> int:
        for i, (start, _end) in enumerate(self._tokens):
            if start > char_pos:
                return max(0, i - 1)
        return len(self._tokens) - 1 if self._tokens else 0

    def near(self, pos_a: int, pos_b: int, window: int = 5) -> bool:
        return abs(self.token_index_at(pos_a) - self.token_index_at(pos_b)) <= window


def _first_match_pos(pattern: re.Pattern, text: str) -> int | None:
    m = pattern.search(text)
    return m.start() if m else None


def hf1_length(text_norm: str) -> str | None:
    n = len(text_norm)
    if n < 40 or n > 8000:
        return "HF-1_length"
    return None


def hf2_no_stack(stack_matched: list[str]) -> str | None:
    if not stack_matched:
        return "HF-2_no_stack"
    return None


def hf3_closed(text_norm: str, closed_re: re.Pattern | None, hiring_re: re.Pattern | None) -> str | None:
    if closed_re is None:
        return None
    idx = TokenIndex(text_norm)
    for m in closed_re.finditer(text_norm):
        # маркер в первых 200 символах — тоже считаем срабатыванием (частая позиция "Вакансия закрыта")
        if m.start() < 200:
            return "HF-3_closed"
        if hiring_re is not None:
            hire_m = hiring_re.search(text_norm)
            if hire_m and idx.near(m.start(), hire_m.start(), window=5):
                return "HF-3_closed"
    return None


def hf4_question_form(text_norm: str) -> str | None:
    if len(text_norm) >= 200:
        return None
    stripped = text_norm.strip()
    if QUESTION_START_RE.match(stripped) or stripped.endswith("?"):
        return "HF-4_question"
    return None


def hf5_anti_jobseeker(text_norm: str, anti_re: re.Pattern | None, hiring_re: re.Pattern | None) -> str | None:
    if anti_re is None:
        return None
    idx = TokenIndex(text_norm)
    for m in anti_re.finditer(text_norm):
        if m.start() < 200:
            return "HF-5_anti_jobseeker"
        if hiring_re is not None:
            hire_m = hiring_re.search(text_norm)
            if hire_m and idx.near(m.start(), hire_m.start(), window=5):
                return "HF-5_anti_jobseeker"
    return None


def hf6_short_reply(text_norm: str, reply_to_id: int | None) -> str | None:
    if reply_to_id is not None and len(text_norm) < 200:
        return "HF-6_short_reply"
    return None


def hf7_duplicate() -> str | None:
    # обрабатывается отдельно в dedup.py до вызова остальных HF — здесь заглушка для нумерации
    return None


def run_hard_filters(
    text_norm: str,
    stack_matched: list[str],
    reply_to_id: int | None,
    closed_re: re.Pattern | None,
    anti_re: re.Pattern | None,
    hiring_re: re.Pattern | None,
) -> str | None:
    """Прогоняет HF-1..HF-6 по порядку, первое срабатывание останавливает обработку. HF-7 (дубли) — отдельно в dedup.py."""
    for check in (
        lambda: hf1_length(text_norm),
        lambda: hf2_no_stack(stack_matched),
        lambda: hf3_closed(text_norm, closed_re, hiring_re),
        lambda: hf4_question_form(text_norm),
        lambda: hf5_anti_jobseeker(text_norm, anti_re, hiring_re),
        lambda: hf6_short_reply(text_norm, reply_to_id),
    ):
        result = check()
        if result:
            return result
    return None
