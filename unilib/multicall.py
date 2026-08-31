"""
Reading many things from the chain in one request.

A public RPC is fine for one call and hopeless for forty. Watching a handful of
tokens means a round trip per pool per tick, and at roughly half a second each that
is twenty seconds to price forty tokens - longer than the interval it was meant to
fit inside. Multicall3 turns the whole sweep into a single request, and the cost
stops scaling with how many tokens are being watched.

Multicall3 sits at the same address on every chain checked here, so `chain.multicall`
carries a default rather than needing to be looked up per chain.
"""
from web3 import Web3

from . import abis

# One request that fails outright is worth distinguishing from one pool that cannot be
# read; allowFailure keeps a single bad pool from losing the other thirty-nine.
ALLOW_FAILURE = True


def _contract(chain, w3):
    if not chain.multicall:
        raise ValueError(f"{chain.name} icin multicall adresi tanimli degil")
    return w3.eth.contract(
        address=Web3.to_checksum_address(chain.multicall), abi=abis.MULTICALL3_ABI
    )


def aggregate(chain, calls, w3=None, block=None):
    """
    Send many (target, calldata) pairs as one call.

    Returns a list of (success, data) in the order given. Nothing is decoded here -
    the caller knows what it asked for, and this layer stays useful for anything on
    an EVM chain rather than only for pools.
    """
    w3 = w3 or chain.connect()
    if not calls:
        return []

    payload = [(Web3.to_checksum_address(target), ALLOW_FAILURE,
                data if isinstance(data, bytes) else Web3.to_bytes(hexstr=data))
               for target, data in calls]

    results = _contract(chain, w3).functions.aggregate3(payload).call(
        block_identifier=block or "latest"
    )
    return [(success, data) for success, data in results]


def fetch_prices(chain, pools, w3=None, block=None):
    """
    (price_0, price_1) for many pools, from one request.

    Every version is asked in its own way - reserves from a V2 pair, slot0 from a V3
    pool, StateView from a V4 id - but each of those is a plain view call with fixed
    calldata, so they all travel together. The pools describe their own call; this
    only carries them.

    Returns {pool: (price_0, price_1)}. A pool whose call failed is left out rather
    than given a made-up number: a tracker can skip a tick, but it cannot recover
    from being told a wrong price.
    """
    pools = list(pools)
    if not pools:
        return {}

    w3 = w3 or pools[0].w3
    results = aggregate(chain, [pool.price_call() for pool in pools], w3=w3, block=block)

    prices = {}
    for pool, (success, data) in zip(pools, results):
        if not success or not data:
            continue
        try:
            prices[pool] = pool.decode_prices(data)
        except Exception:
            # A pool that answers in a shape we do not recognise is skipped for the
            # same reason a failed call is: silence beats a plausible wrong number.
            continue
    return prices


def quote_many(chain, pools, amount_in, w3=None, block=None):
    """
    What `amount_in` of each pool's base asset would buy, for many pools at once.

    Spot figures: reserve and tick arithmetic, carrying no fee and no price impact,
    and blind to whatever a hook takes on the side. That is the right trade for a
    watchlist, where what matters is how a number moves rather than what it would
    settle at - and it is the only version of this that fits in one request, since
    the quoters answer one swap at a time.

    Anything about to be traded should be priced with the quoter instead.
    """
    prices = fetch_prices(chain, pools, w3=w3, block=block)
    out = {}
    for pool, (price_0, price_1) in prices.items():
        if pool.token_is_0 is None:
            continue
        base_price = price_1 if pool.token_is_0 else price_0
        out[pool] = amount_in * base_price
    return out
