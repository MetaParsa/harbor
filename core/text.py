"""Persian text helpers shared by all products: normalisation for search, digits, toman and age labels."""

import re
from datetime import datetime

_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_AR_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_TO_LATIN = str.maketrans(_FA_DIGITS + _AR_DIGITS, "0123456789" * 2)
_TO_FA = str.maketrans("0123456789", _FA_DIGITS)

_CHAR_MAP = str.maketrans({
    "ي": "ی", "ى": "ی", "ئ": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه",
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ؤ": "و",
    "‌": " ",  # ZWNJ (half-space) -> space so «هندزفری‌ها» and «هندزفری ها» match
    "​": None, "‍": None, "﻿": None, "ـ": None,  # zero-width, BOM, tatweel
})
_DIACRITICS = re.compile(r"[ً-ٰٟۖ-ۭ]")
_SPACES = re.compile(r"\s+")


def to_latin_digits(s: str) -> str:
    return s.translate(_TO_LATIN)


def to_fa_digits(s: str | int) -> str:
    return str(s).translate(_TO_FA)


def normalize_fa(s: str | None) -> str:
    """Canonical form for search and matching: unify letters and digits, drop diacritics, lowercase Latin."""
    if not s:
        return ""
    s = _DIACRITICS.sub("", s.translate(_CHAR_MAP))
    s = to_latin_digits(s).lower()
    return _SPACES.sub(" ", s).strip()


def format_toman(n: int | None) -> str:
    """1490000 -> «۱٬۴۹۰٬۰۰۰»."""
    if n is None:
        return ""
    return to_fa_digits(f"{n:,}").replace(",", "٬")


def format_million(n: int) -> str:
    """3000000 -> «۳ میلیون», 1490000 -> «۱٫۴۹ میلیون»."""
    m = n / 1_000_000
    text = f"{m:.2f}".rstrip("0").rstrip(".")
    return to_fa_digits(text).replace(".", "٫") + " میلیون"


def price_age_label(changed_at: datetime | None, now: datetime) -> tuple[str, bool]:
    """Relative age of a registered price, plus whether it is old enough to say «احتمالاً تغییر کرده» (> 90 days)."""
    if changed_at is None:
        return "تاریخ ثبت نشده", True
    days = (now - changed_at).days
    if days <= 0:
        label = "امروز"
    elif days == 1:
        label = "دیروز"
    elif days < 7:
        label = f"{to_fa_digits(days)} روز پیش"
    elif days < 30:
        label = f"{to_fa_digits(days // 7)} هفته پیش"
    elif days < 365:
        label = f"{to_fa_digits(days // 30)} ماه پیش"
    else:
        label = f"{to_fa_digits(days // 365)} سال پیش"
    return label, days > 90
