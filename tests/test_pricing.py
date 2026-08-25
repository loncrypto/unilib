"""
Tests for the pure price math. No network needed - run directly:

    python tests/test_pricing.py

These exist because a direction/sign mistake in these formulas produces a
plausible-looking number rather than an error. One such bug (an inverted sell
price) shipped once and was only caught by a human noticing an absurd figure.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unilib import pricing  # noqa: E402


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def test_v2_amount_out_matches_known_values():
    # Balanced 1000/1000 pool (18 decimals), 0.3% fee, swapping 1 whole token.
    # The textbook answer for x*y=k with that fee is 0.996006... out.
    out = pricing.v2_amount_out(10**18, 1000 * 10**18, 1000 * 10**18)
    assert out == 996006981039903216
    assert approx(out / 10**18, 0.9960069810399033, tol=1e-12)


def test_v2_amount_out_floors_tiny_trades_to_zero():
    # Integer math: an input too small to move the pool by one unit yields nothing.
    # Worth pinning down so it is never mistaken for a failed call.
    assert pricing.v2_amount_out(1, 1000, 1000) == 0


def test_v2_output_is_reduced_by_price_impact():
    # A trade large relative to reserves must come back well under the naive
    # spot-price answer - this is what a plain multiplication gets wrong.
    reserve_in = reserve_out = 10**18
    spot_estimate = 10**17  # 10% of the pool, naively
    actual = pricing.v2_amount_out(10**17, reserve_in, reserve_out)
    assert actual < spot_estimate * 0.92


def test_v2_amount_in_inverts_amount_out():
    reserve_in, reserve_out = 5 * 10**18, 3 * 10**18
    amount_in = 10**17
    out = pricing.v2_amount_out(amount_in, reserve_in, reserve_out)
    back = pricing.v2_amount_in(out, reserve_in, reserve_out)
    # Rounding in the contracts' integer math means this returns to within a wei or two.
    assert abs(back - amount_in) <= 2


def test_v2_fee_lowers_output():
    no_fee = pricing.v2_amount_out(10**16, 10**18, 10**18, 1000, 1000)
    with_fee = pricing.v2_amount_out(10**16, 10**18, 10**18, 997, 1000)
    assert with_fee < no_fee


def test_pancake_style_fee_beats_uniswap_fee():
    # 0.25% (9975/10000) should return more than 0.3% (997/1000) on the same pool.
    uni = pricing.v2_amount_out(10**16, 10**18, 10**18, 997, 1000)
    cake = pricing.v2_amount_out(10**16, 10**18, 10**18, 9975, 10000)
    assert cake > uni


def test_tick_zero_is_parity_for_equal_decimals():
    price_0, price_1 = pricing.tick_to_prices(0, 18, 18)
    assert approx(price_0, 1.0)
    assert approx(price_1, 1.0)


def test_prices_are_reciprocal():
    price_0, price_1 = pricing.tick_to_prices(198060, 18, 18)
    assert approx(price_0 * price_1, 1.0)


def test_positive_tick_means_token0_worth_more():
    price_0, _ = pricing.tick_to_prices(100, 18, 18)
    assert price_0 > 1.0


def test_decimals_difference_is_applied():
    # A 6-decimal token1 against an 18-decimal token0 shifts the price by 10^12.
    equal, _ = pricing.tick_to_prices(0, 18, 18)
    mixed, _ = pricing.tick_to_prices(0, 18, 6)
    assert approx(mixed, equal * 10**12)


def test_tick_and_sqrt_price_agree():
    # slot0 gives both; they encode the same price, so they must roughly match.
    tick = 198060
    sqrt_price_x96 = int((1.0001 ** (tick / 2)) * pricing.Q96)
    from_tick, _ = pricing.tick_to_prices(tick, 18, 18)
    from_sqrt, _ = pricing.sqrt_price_x96_to_prices(sqrt_price_x96, 18, 18)
    assert abs(from_tick - from_sqrt) / from_tick < 1e-6


def test_percentage_change():
    assert approx(pricing.percentage_change(100, 110), 10.0)
    assert approx(pricing.percentage_change(100, 90), -10.0)
    assert pricing.percentage_change(0, 50) == 0.0  # no division by zero


def test_apply_slippage():
    assert approx(pricing.apply_slippage(100, 1), 99.0)
    assert approx(pricing.apply_slippage(100, 0), 100.0)


def test_v2_amount_in_rejects_impossible_output():
    try:
        pricing.v2_amount_in(10**18, 10**18, 10**18)
    except ValueError:
        return
    raise AssertionError("rezervden buyuk cikti icin hata bekleniyordu")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok  {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {test.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} gecti")
    sys.exit(1 if failed else 0)
