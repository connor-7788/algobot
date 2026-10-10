"""
Kalshi trading fees.

Checked against the published fee schedule (kalshi.com/docs/kalshi-fee-schedule.pdf,
"Last updated and effective: July 7, 2026") and docs.kalshi.com/getting_started/fee_rounding:

    taker fee = round up( M_taker * 0.07   * C * P * (1 - P) )
    maker fee = round up( M_maker * 0.0175 * C * P * (1 - P) )

P is the price in dollars, C the contract count, M a per-series multiplier
(taker defaults to 1, maker defaults to 0 except on series with maker fees).
There is no settlement fee.

Rounding (2026 rules): the model fee is first rounded up to $0.000001, then a
"rounding fee" aligns the balance change to the account's precision, $0.01
for FCM-cleared members or $0.0001 for direct members. Kalshi keeps a per-order
accumulator and rebates overpayment on later fills; this module ignores the
rebate, so it can only overstate fees, which is the side we want to err on.

For a whole-cent price and integer count, this collapses to the familiar
ceil_to_cent(0.07 * C * P * (1-P)) when precision is $0.01.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

SIX_DP = Decimal("0.000001")


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def ceil_to(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


def floor_to(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


def model_fee(price_usd, count, coefficient, multiplier=1.0) -> Decimal:
    """Unrounded fee in dollars."""
    p = _d(price_usd)
    return _d(multiplier) * _d(coefficient) * _d(count) * p * (Decimal(1) - p)


def charged_fee(price_usd, count, coefficient, multiplier=1.0,
                precision=0.01, buying: bool = True) -> Decimal:
    """Fee actually taken from the balance for one fill, before any rebate.

    Follows the documented algorithm:
        trade_fee      = ceil_6dp(model_fee)
        aligned_change = floor_precision(revenue - trade_fee)
        rounding_fee   = (revenue - trade_fee) - aligned_change
    where revenue is negative for a buyer. Returns trade_fee + rounding_fee.
    """
    if _d(count) <= 0:
        return Decimal(0)
    step = _d(precision)
    trade_fee = ceil_to(model_fee(price_usd, count, coefficient, multiplier), SIX_DP)
    notional = _d(price_usd) * _d(count)
    revenue = -notional if buying else notional
    pre = revenue - trade_fee
    aligned = floor_to(pre, step)
    rounding_fee = pre - aligned
    return trade_fee + rounding_fee


def taker_fee_usd(price_cents: int, count: int, settings) -> float:
    return float(charged_fee(
        Decimal(price_cents) / 100, count,
        settings.fee_taker_coefficient, settings.fee_taker_multiplier,
        settings.fee_balance_precision, buying=True,
    ))


def worst_case_fee_per_contract_cents(price_cents: int, settings) -> float:
    """Per-contract fee for a 1-contract fill, taking the larger of taker and
    maker. Rounding is per fill, so one contract is the worst per-contract
    case; larger fills only lower it. We don't know in advance whether a
    limit order rests or crosses, so we charge the worse of the two."""
    taker = charged_fee(Decimal(price_cents) / 100, 1, settings.fee_taker_coefficient,
                        settings.fee_taker_multiplier, settings.fee_balance_precision)
    maker = charged_fee(Decimal(price_cents) / 100, 1, settings.fee_maker_coefficient,
                        settings.fee_maker_multiplier, settings.fee_balance_precision)
    return float(max(taker, maker) * 100)
