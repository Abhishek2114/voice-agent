"""CSV formula-injection protection (OWASP "CSV Injection")."""

from __future__ import annotations

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")


def is_formula_like(value: str) -> bool:
    if not value:
        return False
    v = value.lstrip(" ")
    if not v.startswith(FORMULA_PREFIXES):
        return False
    # plain negative numbers / phone numbers like +91... are data, not formulas
    body = v[1:].replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace(".", "")
    return not (v[0] in "+-" and body.isdigit())


def neutralize(value: str) -> str:
    """Prefix formula-like values with an apostrophe so spreadsheets treat them as text."""
    return "'" + value if is_formula_like(value) else value


def escape_cell(value: object) -> str:
    """Escape any value written to an exported CSV."""
    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip(" ")
    if stripped.startswith(FORMULA_PREFIXES) and not (stripped[:1] in "+-" and stripped[1:].replace(" ", "").isdigit()):
        return "'" + text
    return text
