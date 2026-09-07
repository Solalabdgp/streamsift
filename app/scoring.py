import re
from dataclasses import dataclass, field

CONTACT_RE = re.compile(r"@\w{4,}|t\.me/\w+|пишит\w* в л[сич]\w*|пиши\w* в л[сич]\w*|в личк\w+|\bdm\b|[\w.+-]+@[\w-]+\.\w+")
MONEY_AMOUNT_RE = re.compile(
    r"\d{3,}\s*(\$|usd|usdt|₸|тг|руб|₽|€|eur)|от\s*\d{2,}\s*(к|k|тыс)|\d{2,}\s*k\b",
    re.IGNORECASE,
)
SECTION_HEADER_RE = re.compile(
    r"(требовани|обязанност|услови|мы предлагаем|что предлагаем|стек\b|задачи|"
    r"формат работы|о проекте|о нас\b|ожидани)\w*\s*:",
)


def _build_alternation(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    escaped = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b")


@dataclass
class RulesCache:
    version: int = 0
    stack_weights: dict[str, int] = field(default_factory=dict)
    hiring_weights: dict[str, int] = field(default_factory=dict)
    money_weights: dict[str, int] = field(default_factory=dict)
    anti_terms: list[str] = field(default_factory=list)
    closed_terms: list[str] = field(default_factory=list)
    noise_weights: dict[str, int] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    rule_weights: dict[str, int] = field(default_factory=dict)
    stack_cap: int = 9
    hiring_cap: int = 8
    noise_cap: int = -8
    notify_threshold: int = 10
    rules_version_tag: str = "v1"

    stack_re: re.Pattern | None = field(default=None, repr=False)
    hiring_re: re.Pattern | None = field(default=None, repr=False)
    anti_re: re.Pattern | None = field(default=None, repr=False)
    closed_re: re.Pattern | None = field(default=None, repr=False)
    noise_re: re.Pattern | None = field(default=None, repr=False)
    alias_re: re.Pattern | None = field(default=None, repr=False)

    def compile(self) -> None:
        self.stack_re = _build_alternation(list(self.stack_weights.keys()))
        self.hiring_re = _build_alternation(list(self.hiring_weights.keys()) + list(self.money_weights.keys()))
        self.anti_re = _build_alternation(self.anti_terms)
        self.closed_re = _build_alternation(self.closed_terms)
        self.noise_re = _build_alternation(list(self.noise_weights.keys()))
        self.alias_re = _build_alternation(list(self.aliases.keys()))


def _matched_terms(pattern: re.Pattern | None, text: str) -> list[str]:
    if pattern is None:
        return []
    seen: list[str] = []
    seen_set = set()
    for m in pattern.finditer(text):
        term = m.group(0)
        if term not in seen_set:
            seen_set.add(term)
            seen.append(term)
    return seen


@dataclass
class ScoreResult:
    score: int
    signals: dict[str, int]
    stack_matched: list[str]
    hiring_matched: list[str]
    compensation: str | None
    contact: str | None


def score_message(
    text_norm: str,
    struct_features: dict,
    reply_to_id: int | None,
    source_kind: str,
    source_priority: int,
    author_reputation_bonus: int,
    rules: RulesCache,
) -> ScoreResult:
    signals: dict[str, int] = {}

    # A. Стек
    stack_matched = _matched_terms(rules.stack_re, text_norm)
    a_raw = sum(rules.stack_weights.get(t, 0) for t in stack_matched)
    a_score = min(a_raw, rules.stack_cap)
    if a_score:
        signals["stack"] = a_score

    # B. Сигнал найма (+деньги-ключевики)
    hiring_matched = _matched_terms(rules.hiring_re, text_norm)
    b_raw = sum(
        rules.hiring_weights.get(t, rules.money_weights.get(t, 0)) for t in hiring_matched
    )
    b_score = min(b_raw, rules.hiring_cap)
    if b_score:
        signals["hiring"] = b_score

    # C. Структура
    c_score = 0
    if SECTION_HEADER_RE.search(text_norm):
        w = rules.rule_weights.get("has_sections", 3)
        c_score += w
        signals["has_sections"] = w
    if CONTACT_RE.search(text_norm):
        w = rules.rule_weights.get("has_contact", 2)
        c_score += w
        signals["has_contact"] = w
    money_match = MONEY_AMOUNT_RE.search(text_norm)
    if money_match:
        w = rules.rule_weights.get("has_money_pattern", 2)
        c_score += w
        signals["has_money_pattern"] = w
    if struct_features.get("bullet_line_count", 0) >= 3:
        w = rules.rule_weights.get("has_bullets", 2)
        c_score += w
        signals["has_bullets"] = w
    length = struct_features.get("length", 0)
    if length > 400:
        w = rules.rule_weights.get("long_len", 1)
        c_score += w
        signals["long_len"] = w
    if length < 150:
        w = rules.rule_weights.get("short_len", -3)
        c_score += w
        signals["short_len"] = w
    if reply_to_id is not None:
        w = rules.rule_weights.get("reply_penalty", -2)
        c_score += w
        signals["reply_penalty"] = w
    if struct_features.get("hashtag_count", 0) > 3:
        w = rules.rule_weights.get("many_hashtags", -2)
        c_score += w
        signals["many_hashtags"] = w

    # D. Контекст источника
    d_score = 0
    if source_kind == "job_board":
        w = rules.rule_weights.get("source_job_board", 3)
        d_score += w
        signals["source_job_board"] = w
    elif source_kind == "dm":
        w = rules.rule_weights.get("source_dm", 2)
        d_score += w
        signals["source_dm"] = w
    if source_priority:
        d_score += source_priority
        signals["source_priority"] = source_priority
    if author_reputation_bonus:
        d_score += author_reputation_bonus
        signals["author_reputation"] = author_reputation_bonus

    # Мягкие штрафы (шум)
    noise_matched = _matched_terms(rules.noise_re, text_norm)
    noise_raw = sum(rules.noise_weights.get(t, -4) for t in noise_matched)
    noise_score = max(noise_raw, rules.noise_cap)
    if noise_score:
        signals["noise"] = noise_score

    total = a_score + b_score + c_score + d_score + noise_score

    contact_m = CONTACT_RE.search(text_norm)
    contact = contact_m.group(0) if contact_m else None
    compensation = money_match.group(0) if money_match else None

    return ScoreResult(
        score=total,
        signals=signals,
        stack_matched=stack_matched,
        hiring_matched=hiring_matched,
        compensation=compensation,
        contact=contact,
    )
