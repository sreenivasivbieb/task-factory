"""Sales tax by region. Rates are basis points (1 bp = 0.01 %)."""

from .errors import UnknownRegion

BASIS_POINTS = 10000
TAX_RATES = {"CA": 725, "NY": 400, "TX": 625, "OR": 0}


def _lookup(region: str) -> tuple[str, int]:
    code = region.upper()
    if code not in TAX_RATES:
        raise UnknownRegion(f"unknown tax region {region!r}")
    return code, TAX_RATES[code]


def _div_half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def tax_cents(amount_cents: int, region: str) -> int:
    """Tax on ``amount_cents`` in ``region``, rounded half up to a whole cent."""
    _, rate = _lookup(region)
    return _div_half_up(amount_cents * rate, BASIS_POINTS)


def rate_label(region: str) -> str:
    """The region and its rate for receipts, e.g. ``CA 7.25%``."""
    code, rate = _lookup(region)
    return f"{code} {rate // 100}.{rate % 100:02d}%"
