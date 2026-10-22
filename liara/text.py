"""Persian text helpers for everything the user reads: numbers in Persian digits, money in toman, and bidi isolation
for paths / errors inside a Persian sentence."""

_FA = str.maketrans("0123456789,.-", "۰۱۲۳۴۵۶۷۸۹٬٫−")


def fa_num(x: float | int, digits: int = 0) -> str:
    """12345.6 → «۱۲٬۳۴۶»; with digits=1 → «۱۲٬۳۴۵٫۶». Trailing zeros are dropped (0.50 → «۰٫۵», 1.0 → «۱»)."""
    s = f"{x:,.{digits}f}" if digits else f"{round(x):,}"
    if digits:
        s = s.rstrip("0").rstrip(".")
    return s.translate(_FA)


def toman(n: float) -> str:
    return f"{fa_num(n)} تومان"


def ltr(text: str) -> str:
    """Isolate a path / error / file:line inside a Persian sentence so the browser doesn't scramble it."""
    return f"⁦{text}⁩"
