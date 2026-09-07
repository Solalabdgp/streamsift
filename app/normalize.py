import re

MD_CHARS_RE = re.compile(r"[*_`~\[\]]")
URL_RE = re.compile(r"https?://\S+|t\.me/\S+")
MENTION_RE = re.compile(r"@\w{4,}")
BULLET_LINE_RE = re.compile(r"^\s*([-–—•*●▪️🔹]|\d+[.)])\s+", re.MULTILINE)
HASHTAG_RE = re.compile(r"#\w+")
WHITESPACE_RE = re.compile(r"[ \t]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")

YO_MAP = str.maketrans({"ё": "е", "Ё": "Е"})


def compute_struct_features(raw_text: str) -> dict:
    """Структурные признаки исходного (ненормализованного) текста — FR-2.3."""
    lines = raw_text.split("\n")
    non_empty_lines = [ln for ln in lines if ln.strip()]
    bullet_lines = BULLET_LINE_RE.findall(raw_text)
    return {
        "length": len(raw_text),
        "line_count": len(non_empty_lines),
        "bullet_line_count": len(bullet_lines),
        "has_links": bool(URL_RE.search(raw_text)),
        "has_mentions": bool(MENTION_RE.search(raw_text)),
        "hashtag_count": len(HASHTAG_RE.findall(raw_text)),
    }


def normalize_text(raw_text: str) -> str:
    """FR-2.1: markdown -> lowercase -> ё/е -> схлопывание пробелов (без синонимов)."""
    text = MD_CHARS_RE.sub("", raw_text)
    text = text.lower()
    text = text.translate(YO_MAP)
    text = WHITESPACE_RE.sub(" ", text)
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def apply_aliases(text_norm: str, alias_pattern: re.Pattern | None, aliases: dict[str, str]) -> str:
    """FR-2.2: раскрытие синонимов поверх нормализованного текста."""
    if alias_pattern is None or not aliases:
        return text_norm

    def _sub(m: re.Match) -> str:
        return aliases.get(m.group(0), m.group(0))

    return alias_pattern.sub(_sub, text_norm)


def extract_headline(raw_text: str, max_len: int = 80) -> str:
    """FR-7.1: заголовок из первой непустой строки исходного (не нормализованного) текста."""
    for line in raw_text.split("\n"):
        line = line.strip()
        if line:
            return line[:max_len]
    return raw_text[:max_len]
