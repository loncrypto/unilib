"""
Pure price math - no network, no config, no web3.

Everything here takes numbers and returns numbers, which means it can be tested
without touching a chain. That matters: a sign/direction mistake in these formulas
is silent and expensive, so they are the part most worth testing in isolation.
"""

Q96 = 2**96


def tick_to_prices(tick, decimals0, decimals1):
    """
    Convert a V3/V4 tick into the two human-readable prices of a pool.

    A tick is the pool's own logarithmic encoding of its current price. The raw
    formula gives price in "smallest unit" terms, so the decimals difference has
    to be divided out before the number means anything to a human.

    Returns (price_0, price_1) where:
      price_0 = how much token1 one token0 is worth
      price_1 = how much token0 one token1 is worth  (always 1 / price_0)
    """
    price_0 = (1.0001**tick) / (10 ** (decimals1 - decimals0))
    return price_0, 1 / price_0


def sqrt_price_x96_to_prices(sqrt_price_x96, decimals0, decimals1):
    """
    Same result as tick_to_prices, but starting from sqrtPriceX96.

    Both values come out of slot0. The tick is a rounded-down version of the price,
    while sqrtPriceX96 carries the exact current price - so this is the more precise
    of the two when the pool sits between ticks.
    """
    raw = (sqrt_price_x96 / Q96) ** 2
    price_0 = raw / (10 ** (decimals1 - decimals0))
    return price_0, 1 / price_0


def v2_amount_out(amount_in_wei, reserve_in, reserve_out, fee_numerator=997, fee_denominator=1000):
    """
    Exact V2 output for a given input, including the swap fee and price impact.

    This is the same integer formula as UniswapV2Library.getAmountOut in the
    contracts, so the result matches what the pool will actually give - unlike a
    spot-price multiplication, which ignores both the fee and the fact that a
    trade moves the price against itself.

    fee_numerator defaults to Uniswap's 0.3%. Forks differ (PancakeSwap uses 9975
    /10000 for 0.25%), so pass the chain's own values when they are known.
    """
    amount_in_with_fee = amount_in_wei * fee_numerator
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * fee_denominator + amount_in_with_fee
    return numerator // denominator


def v2_amount_in(amount_out_wei, reserve_in, reserve_out, fee_numerator=997, fee_denominator=1000):
    """
    Exact V2 input required to receive a given output - the inverse of v2_amount_out.

    Mirrors UniswapV2Library.getAmountIn. Useful for "I want exactly N tokens, what
    does that cost me" rather than "I have N to spend".
    """
    if amount_out_wei >= reserve_out:
        raise ValueError("Istenen cikti miktari pool'un rezervinden buyuk olamaz")
    numerator = reserve_in * amount_out_wei * fee_denominator
    denominator = (reserve_out - amount_out_wei) * fee_numerator
    return numerator // denominator + 1


def percentage_change(old_value, new_value):
    """Percentage change from old to new. Returns 0 when old is 0 rather than dividing by it."""
    if old_value == 0:
        return 0.0
    return ((new_value - old_value) / old_value) * 100


def apply_slippage(amount, slippage_pct):
    """
    Lower an expected output by a slippage tolerance, giving the minimum acceptable amount.

    slippage_pct is a percentage (1 means 1%), not a fraction, to match how it is
    normally typed in by a user.
    """
    return amount * (1 - slippage_pct / 100)
