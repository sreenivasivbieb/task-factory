"""Money helpers. All amounts are integer cents."""

GROUP = 3


def _ungroup(whole: str, amount: str) -> str:
    """``"12,345"`` -> ``"12345"``; commas must separate groups of exactly three digits."""
    if "," not in whole:
        return whole
    groups = whole.split(",")
    if not 1 <= len(groups[0]) <= GROUP or any(len(g) != GROUP for g in groups[1:]):
        raise ValueError(f"bad thousands separators: {amount!r}")
    return "".join(groups)


def to_cents(amount: str) -> int:
    """Parse a decimal string such as "12.50", "$1,234.50" or "-$0.05" into cents."""
    text = amount.strip()
    if not text:
        raise ValueError("empty amount")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if text.startswith("$"):
        text = text[1:]
    if "." in text:
        whole, frac = text.split(".", 1)
        if len(frac) > 2:
            raise ValueError(f"too many decimals: {amount!r}")
        frac = frac.ljust(2, "0")
    else:
        whole, frac = text, "00"
    whole = _ungroup(whole, amount)
    if not whole.isdigit() or not frac.isdigit():
        raise ValueError(f"not a number: {amount!r}")
    cents = int(whole) * 100 + int(frac)
    return -cents if negative else cents


def format_cents(cents: int, grouping: bool = False) -> str:
    """Format cents as a dollar string: 1250 -> "$12.50", -5 -> "-$0.05"; grouping adds commas."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    whole = f"{cents // 100:,}" if grouping else str(cents // 100)
    return f"{sign}${whole}.{cents % 100:02d}"


def percent_of(cents: int, percent: int) -> int:
    """Return ``percent`` % of ``cents``, rounded half up to a whole cent."""
    return (cents * percent + 50) // 100
