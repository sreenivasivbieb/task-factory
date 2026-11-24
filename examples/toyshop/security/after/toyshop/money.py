"""Money helpers. All amounts are integer cents."""

import re

_DIGITS = re.compile(r"[0-9]+")  # ASCII only: str.isdigit() also accepts other scripts' digits


def to_cents(amount: str) -> int:
    """Parse a decimal string such as "12.50" into cents (1250)."""
    text = amount.strip()
    if not text:
        raise ValueError("empty amount")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if "." in text:
        whole, frac = text.split(".", 1)
        if len(frac) > 2:
            raise ValueError(f"too many decimals: {amount!r}")
        frac = frac.ljust(2, "0")
    else:
        whole, frac = text, "00"
    if not _DIGITS.fullmatch(whole) or not _DIGITS.fullmatch(frac):
        raise ValueError(f"not a number: {amount!r}")
    cents = int(whole) * 100 + int(frac)
    return -cents if negative else cents


def format_cents(cents: int) -> str:
    """Format cents as a dollar string: 1250 -> "$12.50", -5 -> "-$0.05"."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def percent_of(cents: int, percent: int) -> int:
    """Return ``percent`` % of ``cents``, rounded half up to a whole cent."""
    return (cents * percent + 50) // 100
