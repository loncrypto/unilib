"""
Paths through more than one pool.

Some tokens have no pool against the chain's own coin. On Robinhood Chain a launchpad
pairs its tokens against tokenised stocks instead, so reaching one from ETH means
going through the stock: ETH -> NVDA -> CPU. Two pools, one transaction.

A route is deliberately given, not discovered. Searching for a path sounds like the
helpful thing to do, but on a chain where most pools are dead, several charge 90%,
and the liquidity figure a pool reports does not describe its depth, picking a route
automatically means picking one wrong eventually - with money on it. The caller names
the pools; this module checks they connect and asks the chain what they pay.

A single pool is a route of length one, so nothing needs a separate path for the
simple case.
"""
from web3 import Web3

from . import abis
from .chains import NATIVE_ADDRESS


class Route:
    """
    An ordered path from one currency to another through a list of pools.

    Direction is worked out rather than stored: starting from currency_in, each pool
    must contain the currency arrived with, and hands over its other side. A stored
    direction can drift out of step with the pools it describes; a derived one cannot,
    and a route that does not connect is rejected here instead of at send time.
    """

    def __init__(self, chain, currency_in, pools, w3=None):
        if not pools:
            raise ValueError("rota en az bir havuz icermeli")

        self.chain = chain
        self.pools = list(pools)
        self.w3 = w3 or self.pools[0].w3
        self.currency_in = Web3.to_checksum_address(currency_in)

        # Walk the chain, checking every link. The currencies list ends up one longer
        # than the pools: what goes in, what comes out of each hop.
        self.currencies = [self.currency_in]
        current = self.currency_in
        for i, pool in enumerate(self.pools):
            sides = (pool.token0, pool.token1)
            if current.lower() not in (s.lower() for s in sides):
                raise ValueError(
                    f"rota kopuk: {i + 1}. havuz ({pool.symbol0}/{pool.symbol1}) "
                    f"{self._symbol_of(current)} icermiyor"
                )
            nxt = sides[1] if current.lower() == sides[0].lower() else sides[0]
            self.currencies.append(Web3.to_checksum_address(nxt))
            current = nxt

    # -- identity -----------------------------------------------------------

    def _symbol_of(self, address):
        if address.lower() == NATIVE_ADDRESS:
            return self.chain.native_symbol
        for pool in self.pools:
            for addr, sym in ((pool.token0, pool.symbol0), (pool.token1, pool.symbol1)):
                if addr.lower() == address.lower():
                    return sym
        return address[:10]

    def _decimals_of(self, address):
        if address.lower() == NATIVE_ADDRESS:
            return 18
        for pool in self.pools:
            for addr, dec in ((pool.token0, pool.decimals0), (pool.token1, pool.decimals1)):
                if addr.lower() == address.lower():
                    return dec
        raise ValueError(f"{address} rotadaki hicbir havuzda yok")

    @property
    def token(self):
        """The currency this route arrives at - the one being bought."""
        return self.currencies[-1]

    @property
    def symbol(self):
        return self._symbol_of(self.token)

    @property
    def decimals(self):
        return self._decimals_of(self.token)

    @property
    def base(self):
        return self.currency_in

    @property
    def base_symbol(self):
        return self._symbol_of(self.currency_in)

    @property
    def base_decimals(self):
        return self._decimals_of(self.currency_in)

    @property
    def is_direct(self):
        return len(self.pools) == 1

    def reversed(self):
        """The same path walked the other way - selling rather than buying."""
        return Route(self.chain, self.token, list(reversed(self.pools)), w3=self.w3)

    # -- quoting ------------------------------------------------------------

    def path_keys(self):
        """
        The route as V4 PathKeys: one per hop, naming the currency that hop swaps INTO.

        Returned in the router codec's shape - a dict per hop. The quoter's ABI wants
        the same five fields as a plain tuple, which _quoter_path() derives from this
        rather than building separately: the figure that was quoted and the swap that
        gets sent then cannot describe two different paths.
        """
        keys = []
        for pool, currency_out in zip(self.pools, self.currencies[1:]):
            if pool.pool_type != "v4":
                raise ValueError(
                    f"cok adimli rota simdilik yalnizca V4 havuzlariyla calisiyor "
                    f"({pool.symbol0}/{pool.symbol1} {pool.pool_type})"
                )
            keys.append({
                "intermediate_currency": Web3.to_checksum_address(currency_out),
                "fee": pool.fee,
                "tick_spacing": pool.tick_spacing,
                "hooks": Web3.to_checksum_address(pool.hooks),
                "hook_data": b"",
            })
        return keys

    def _quoter_path(self):
        """The same hops as ABI tuples, in the order the quoter's struct declares."""
        return [(k["intermediate_currency"], k["fee"], k["tick_spacing"],
                 k["hooks"], k["hook_data"]) for k in self.path_keys()]

    def quote(self, amount_in):
        """
        What this route really pays, hooks and price impact included.

        One call for the whole path. Asking each pool separately and multiplying gives
        the same answer only when nothing charges outside the pool fee - which on this
        chain is exactly the assumption that does not hold.

        Returns the output in human units, or None if the quoter will not answer -
        a dead hop, most often, which is worth surfacing rather than dressing up as a
        small number.
        """
        if not self.chain.v4_quoter:
            return None

        amount_wei = int(amount_in * 10**self.base_decimals)
        quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.chain.v4_quoter), abi=abis.V4_QUOTER_ABI
        )
        try:
            if self.is_direct:
                out, _gas = quoter.functions.quoteExactInputSingle(
                    (self.pools[0].pool_key,
                     self.currency_in.lower() == self.pools[0].token0.lower(),
                     amount_wei, b"")
                ).call()
            else:
                out, _gas = quoter.functions.quoteExactInput(
                    (self.currency_in, self._quoter_path(), amount_wei)
                ).call()
        except Exception:
            return None
        return out / 10**self.decimals

    def spot(self, amount_in):
        """
        The same trade priced hop by hop from each pool's own state.

        Carries no fee and no price impact, so it always reads high. Its use is as a
        yardstick: the gap between this and quote() is what the route actually costs,
        including anything a hook takes quietly.
        """
        amount = amount_in
        for pool, currency_in in zip(self.pools, self.currencies[:-1]):
            amount = pool.quote(currency_in, amount)
        return amount

    def cost_pct(self, amount_in):
        """How much worse the real answer is than the spot one, as a percentage."""
        real = self.quote(amount_in)
        if not real:
            return None
        reference = self.spot(amount_in)
        return (1 - real / reference) * 100 if reference else None

    def __repr__(self):
        arrow = " -> ".join(self._symbol_of(c) for c in self.currencies)
        return f"<Route {arrow}>"
