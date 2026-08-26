"""
Pool objects - one class per Uniswap version, one shared interface.

The point of this module is that "what is the price?" and "what do I get for X?"
are the same questions on V2, V3 and V4, even though the on-chain answer comes from
three completely different places (reserves / slot0 / a shared PoolManager). Code
using this library asks the question once and does not branch on the version.

Metadata (which tokens, their decimals, a V4 pool's key) is read once when the pool
is loaded. Only the price itself is re-read on every call, which keeps polling cheap.
"""
from web3 import Web3

from . import abis, pricing
from .chains import NATIVE_ADDRESS


def fetch_token_info(w3, address, chain=None):
    """
    Read a token's symbol and decimals from the chain.

    The zero address means the chain's native coin, which has no ERC20 contract to
    call - so it is answered from config instead of on-chain.
    """
    if address.lower() == NATIVE_ADDRESS:
        return (chain.native_symbol if chain else "NATIVE"), 18
    token = w3.eth.contract(address=Web3.to_checksum_address(address), abi=abis.ERC20_ABI)
    return token.functions.symbol().call().upper(), token.functions.decimals().call()


def detect_pool_type(w3, pool_address):
    """
    Work out whether an address is a V2 pair or a V3 pool.

    Both expose token0()/token1(), so those cannot tell them apart. fee() exists
    only on V3 and getReserves() only on V2, so probing those does distinguish them.
    Returns "v2" or "v3".
    """
    address = Web3.to_checksum_address(pool_address)

    v3 = w3.eth.contract(address=address, abi=abis.V3_POOL_ABI)
    try:
        v3.functions.fee().call()
        return "v3"
    except Exception:
        pass

    v2 = w3.eth.contract(address=address, abi=abis.V2_PAIR_ABI)
    try:
        v2.functions.getReserves().call()
        return "v2"
    except Exception:
        pass

    raise ValueError(
        f"{pool_address} icin pool tipi tespit edilemedi "
        "(ne fee() ne getReserves() calisti - bu adres bir pool olmayabilir)"
    )


def get_pool_manager(w3, state_view_address):
    """Ask StateView which PoolManager it reads from, instead of hardcoding it per chain."""
    state_view = w3.eth.contract(
        address=Web3.to_checksum_address(state_view_address), abi=abis.STATE_VIEW_ABI
    )
    return state_view.functions.poolManager().call()


def fetch_v4_pool_key(w3, state_view_address, pool_id, from_block=0):
    """
    Recover a V4 pool's full PoolKey from its id.

    A pool id is keccak of the PoolKey, so it cannot be reversed. But PoolManager
    emits an Initialize event once at pool creation, indexed by pool id, carrying
    every field - so one log query gives back what the hash hid.

    Returns (currency0, currency1, fee, tick_spacing, hooks).

    from_block defaults to 0 (whole chain history). On older/busier chains an RPC
    may refuse that range, in which case pass the deployment block or scan in chunks.
    """
    pool_manager = get_pool_manager(w3, state_view_address)
    topic0 = Web3.keccak(text=abis.V4_INITIALIZE_EVENT)

    logs = w3.eth.get_logs({
        "address": pool_manager,
        "fromBlock": from_block,
        "toBlock": "latest",
        "topics": [topic0, Web3.to_bytes(hexstr=pool_id)],
    })
    if not logs:
        raise ValueError(
            f"{pool_id} icin Initialize event'i bulunamadi "
            "(yanlis pool_id, yanlis chain, ya da from_block cok ileride olabilir)"
        )

    log = logs[0]
    currency0 = w3.codec.decode(["address"], log["topics"][2])[0]
    currency1 = w3.codec.decode(["address"], log["topics"][3])[0]
    fee, tick_spacing, hooks, _sqrt_price, _tick = w3.codec.decode(
        ["uint24", "int24", "address", "uint160", "int24"], log["data"]
    )
    return currency0, currency1, fee, tick_spacing, hooks


class Pool:
    """
    Shared interface for every pool version.

    A pool has two sides. In practice one of them is almost always a base asset
    (native coin, wrapped native, a stablecoin) and the other is the token being
    tracked - so this class resolves that once and then talks in terms of "token"
    and "base" rather than the raw token0/token1 ordering, which is just an
    address-sorting artifact and flips unpredictably between pools.
    """

    pool_type = None
    exact_quotes = False  # True only where a closed-form formula exists (V2)

    def __init__(self, w3, chain, token0, token1, decimals0, decimals1, symbol0, symbol1):
        self.w3 = w3
        self.chain = chain
        self.token0 = token0
        self.token1 = token1
        self.decimals0 = decimals0
        self.decimals1 = decimals1
        self.symbol0 = symbol0
        self.symbol1 = symbol1

        base0 = chain.is_base_token(token0)
        base1 = chain.is_base_token(token1)
        if base0 and not base1:
            self.token_is_0 = False
        elif base1 and not base0:
            self.token_is_0 = True
        else:
            # Both sides base (e.g. ETH/USDC) or neither - there is no single
            # obvious "tracked token", so refuse to guess. quote() still works.
            self.token_is_0 = None

    # -- identity -----------------------------------------------------------

    @property
    def token(self):
        """Address of the tracked (non-base) token."""
        self._require_resolved()
        return self.token0 if self.token_is_0 else self.token1

    @property
    def base(self):
        """Address of the base/quote asset."""
        self._require_resolved()
        return self.token1 if self.token_is_0 else self.token0

    @property
    def symbol(self):
        self._require_resolved()
        return self.symbol0 if self.token_is_0 else self.symbol1

    @property
    def base_symbol(self):
        self._require_resolved()
        return self.symbol1 if self.token_is_0 else self.symbol0

    @property
    def decimals(self):
        self._require_resolved()
        return self.decimals0 if self.token_is_0 else self.decimals1

    @property
    def base_decimals(self):
        self._require_resolved()
        return self.decimals1 if self.token_is_0 else self.decimals0

    def _require_resolved(self):
        if self.token_is_0 is None:
            raise ValueError(
                f"Bu pool'da hangi tarafin takip edilen token oldugu belirsiz "
                f"({self.symbol0}/{self.symbol1}). Acik olan quote() metodunu kullan."
            )

    # -- prices -------------------------------------------------------------

    def _prices(self):
        """(price_0, price_1) for this pool - implemented per version."""
        raise NotImplementedError

    def price(self):
        """Current price of the tracked token, expressed in the base asset."""
        price_0, price_1 = self._prices()
        return price_0 if self.token_is_0 else price_1

    def base_price(self):
        """Current price of the base asset, expressed in the tracked token."""
        price_0, price_1 = self._prices()
        return price_1 if self.token_is_0 else price_0

    # -- quotes -------------------------------------------------------------

    def quote_sell(self, amount_in):
        """How much of the base asset selling `amount_in` tracked tokens yields."""
        self._require_resolved()
        return self.quote(self.token, amount_in)

    def quote_buy(self, amount_in):
        """How many tracked tokens spending `amount_in` of the base asset yields."""
        self._require_resolved()
        return self.quote(self.base, amount_in)

    def quote(self, token_in, amount_in):
        """
        Output for swapping `amount_in` of `token_in` into the other side.

        Unambiguous by construction - use this when a pool has no obvious base side.
        Unless exact_quotes is True this is a spot-price estimate: it ignores price
        impact and fees, so it reads high on anything but small trades.
        """
        price_0, price_1 = self._prices()
        if token_in.lower() == self.token0.lower():
            return amount_in * price_0
        elif token_in.lower() == self.token1.lower():
            return amount_in * price_1
        raise ValueError(f"{token_in} bu pool'un tokenlarindan biri degil")

    def metadata(self):
        """
        Everything about this pool that does not change, as a plain dict.

        Pairs with restore_pool(): save this once when a pool is first added, and
        later rebuilds cost no chain reads at all. Discovering a pool takes several
        sequential RPC calls - probing the version, reading token0/token1, then symbol
        and decimals for each side - and none of those answers ever change.

        Only the price is worth re-reading, and that is one call.
        """
        return {
            "pool_type": self.pool_type,
            "identifier": self.identifier,
            "token0": self.token0,
            "token1": self.token1,
            "decimals0": self.decimals0,
            "decimals1": self.decimals1,
            "symbol0": self.symbol0,
            "symbol1": self.symbol1,
        }

    def __repr__(self):
        pair = f"{self.symbol0}/{self.symbol1}"
        return f"<{type(self).__name__} {pair}>"


class V2Pool(Pool):
    """
    Constant-product pair. Each pool is its own contract holding two reserves.

    The only version where an exact quote is possible off-chain: the output of a
    trade has a closed-form formula, so fees and price impact are both accounted for
    without simulating anything.
    """

    pool_type = "v2"
    exact_quotes = True

    @property
    def identifier(self):
        return self.address

    def __init__(self, w3, chain, address, **kwargs):
        self.address = Web3.to_checksum_address(address)
        self.contract = w3.eth.contract(address=self.address, abi=abis.V2_PAIR_ABI)
        super().__init__(w3, chain, **kwargs)

    def reserves(self):
        reserve0, reserve1, _ = self.contract.functions.getReserves().call()
        return reserve0, reserve1

    def _prices(self):
        reserve0, reserve1 = self.reserves()
        amount0 = reserve0 / 10**self.decimals0
        amount1 = reserve1 / 10**self.decimals1
        price_0 = amount1 / amount0
        return price_0, 1 / price_0

    def quote(self, token_in, amount_in):
        """Exact output, including the pool's fee and the price impact of this trade."""
        reserve0, reserve1 = self.reserves()

        if token_in.lower() == self.token0.lower():
            reserve_in, reserve_out = reserve0, reserve1
            decimals_in, decimals_out = self.decimals0, self.decimals1
        elif token_in.lower() == self.token1.lower():
            reserve_in, reserve_out = reserve1, reserve0
            decimals_in, decimals_out = self.decimals1, self.decimals0
        else:
            raise ValueError(f"{token_in} bu pool'un tokenlarindan biri degil")

        amount_out_wei = pricing.v2_amount_out(
            int(amount_in * 10**decimals_in),
            reserve_in,
            reserve_out,
            self.chain.v2_fee_numerator,
            self.chain.v2_fee_denominator,
        )
        return amount_out_wei / 10**decimals_out


class V3Pool(Pool):
    """
    Concentrated liquidity. Still one contract per pool, price held in slot0.

    No closed-form quote exists here: liquidity can change at every tick boundary,
    so the true output of a trade is only knowable by walking those ticks. Quotes
    from this class are spot-price estimates.
    """

    pool_type = "v3"

    @property
    def identifier(self):
        return self.address

    def metadata(self):
        return {**super().metadata(), "fee": self.fee}

    def __init__(self, w3, chain, address, fee=None, **kwargs):
        self.address = Web3.to_checksum_address(address)
        self.contract = w3.eth.contract(address=self.address, abi=abis.V3_POOL_ABI)
        self.fee = fee if fee is not None else self.contract.functions.fee().call()
        super().__init__(w3, chain, **kwargs)

    def slot0(self):
        return self.contract.functions.slot0().call()

    def _prices(self):
        sqrt_price_x96, _tick, *_ = self.slot0()
        return pricing.sqrt_price_x96_to_prices(sqrt_price_x96, self.decimals0, self.decimals1)


class V4Pool(Pool):
    """
    Singleton architecture: pools have no address, they live inside one PoolManager
    and are identified by a bytes32 pool id. State is read through StateView.

    `hooks` being non-zero means a custom contract runs around swaps and may change
    the economics - a pool can report fee=0 while the hook charges its own cut. Treat
    quotes on hooked pools with extra suspicion.
    """

    pool_type = "v4"

    @property
    def identifier(self):
        return self.pool_id

    def metadata(self):
        return {**super().metadata(), "fee": self.fee,
                "tick_spacing": self.tick_spacing, "hooks": self.hooks}

    def __init__(self, w3, chain, pool_id, fee, tick_spacing, hooks, **kwargs):
        if not chain.state_view:
            raise ValueError(f"{chain.name} icin StateView adresi tanimli degil (V4 yok?)")
        self.pool_id = pool_id
        self.fee = fee
        self.tick_spacing = tick_spacing
        self.hooks = hooks
        self.state_view = w3.eth.contract(
            address=Web3.to_checksum_address(chain.state_view), abi=abis.STATE_VIEW_ABI
        )
        super().__init__(w3, chain, **kwargs)

    @property
    def has_hooks(self):
        return self.hooks.lower() != NATIVE_ADDRESS

    @property
    def pool_key(self):
        """The PoolKey tuple, in the order the contracts expect it."""
        return (
            Web3.to_checksum_address(self.token0),
            Web3.to_checksum_address(self.token1),
            self.fee,
            self.tick_spacing,
            Web3.to_checksum_address(self.hooks),
        )

    def slot0(self):
        return self.state_view.functions.getSlot0(self.pool_id).call()

    def _prices(self):
        sqrt_price_x96, _tick, *_ = self.slot0()
        return pricing.sqrt_price_x96_to_prices(sqrt_price_x96, self.decimals0, self.decimals1)

    def quote_exact(self, token_in, amount_in, hook_data=b""):
        """
        Ask the V4 quoter what this swap really returns.

        Worth preferring over quote() on this version especially: a hooked pool can
        charge through the hook while reporting fee=0, so the spot price understates
        the cost. The quoter runs the actual swap logic, hook included.

        Needs chain.v4_quoter to be set. Returns None if the quoter is unavailable or
        the call reverts, so callers can fall back to the spot estimate.
        """
        if not self.chain.v4_quoter:
            return None

        # Checked rather than assumed: treating "not token0" as token1 turns a wrong
        # address into a confident answer for the opposite direction. On V4 the base
        # is often the native coin, so passing the wrapped address lands here.
        if token_in.lower() not in (self.token0.lower(), self.token1.lower()):
            raise ValueError(
                f"{token_in} is not in this pool ({self.symbol0}/{self.symbol1}). "
                "On V4 the base side may be the native coin - pass pool.base rather "
                "than the wrapped address."
            )

        zero_for_one = token_in.lower() == self.token0.lower()
        decimals_in = self.decimals0 if zero_for_one else self.decimals1
        decimals_out = self.decimals1 if zero_for_one else self.decimals0

        quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.chain.v4_quoter), abi=abis.V4_QUOTER_ABI
        )
        params = (self.pool_key, zero_for_one, int(amount_in * 10**decimals_in), hook_data)
        try:
            amount_out, _gas = quoter.functions.quoteExactInputSingle(params).call()
            return amount_out / 10**decimals_out
        except Exception:
            return None


def load_pool(chain, identifier, w3=None, from_block=0):
    """
    Load a pool from a V2/V3 address or a V4 pool id, working out everything else.

    The identifier's length says which it is: an address is 20 bytes (40 hex chars),
    a V4 pool id is 32 bytes (64). For addresses the version is probed on-chain; for
    pool ids the PoolKey is recovered from the Initialize event.

    Pass an existing `w3` when polling repeatedly - otherwise a new connection is
    opened (and the chain id verified) on every call.
    """
    w3 = w3 or chain.connect()
    hex_part = identifier[2:] if identifier.lower().startswith("0x") else identifier

    if len(hex_part) == 40:
        pool_type = detect_pool_type(w3, identifier)
        cls = V2Pool if pool_type == "v2" else V3Pool
        abi = abis.V2_PAIR_ABI if pool_type == "v2" else abis.V3_POOL_ABI
        contract = w3.eth.contract(address=Web3.to_checksum_address(identifier), abi=abi)
        token0 = contract.functions.token0().call()
        token1 = contract.functions.token1().call()
        symbol0, decimals0 = fetch_token_info(w3, token0, chain)
        symbol1, decimals1 = fetch_token_info(w3, token1, chain)
        return cls(
            w3, chain, identifier,
            token0=token0, token1=token1,
            decimals0=decimals0, decimals1=decimals1,
            symbol0=symbol0, symbol1=symbol1,
        )

    if len(hex_part) == 64:
        if not chain.state_view:
            raise ValueError(
                f"{chain.name} icin StateView adresi tanimli degil - V4 pool okunamaz"
            )
        currency0, currency1, fee, tick_spacing, hooks = fetch_v4_pool_key(
            w3, chain.state_view, identifier, from_block
        )
        symbol0, decimals0 = fetch_token_info(w3, currency0, chain)
        symbol1, decimals1 = fetch_token_info(w3, currency1, chain)
        return V4Pool(
            w3, chain, identifier, fee, tick_spacing, hooks,
            token0=currency0, token1=currency1,
            decimals0=decimals0, decimals1=decimals1,
            symbol0=symbol0, symbol1=symbol1,
        )

    raise ValueError(
        f"Girilen deger ({len(hex_part)} hex karakter) ne pool adresine (40) "
        "ne de V4 pool id'ye (64) benziyor"
    )


POOL_CLASSES = {"v2": V2Pool, "v3": V3Pool, "v4": V4Pool}


def restore_pool(chain, metadata, w3=None):
    """
    Rebuild a pool from a saved metadata() dict, without asking the chain anything.

    Discovering a pool costs several sequential RPC calls - probe the version, read
    token0/token1, then symbol and decimals for each side. On a slow endpoint that is
    seconds of waiting before the first price appears. None of those answers change
    though, so once a pool is known there is no reason to look them up again.

        entry = pool.metadata()      # save this when the token is first added
        pool = restore_pool(chain, entry, w3=w3)

    Raises KeyError if the dict is missing fields, so a caller can fall back to
    load_pool() when an old registry predates this.
    """
    pool_type = metadata["pool_type"]
    cls = POOL_CLASSES.get(pool_type)
    if cls is None:
        raise ValueError(f"bilinmeyen pool tipi: {pool_type}")

    w3 = w3 or chain.connect()
    common = {
        "token0": metadata["token0"],
        "token1": metadata["token1"],
        "decimals0": metadata["decimals0"],
        "decimals1": metadata["decimals1"],
        "symbol0": metadata["symbol0"],
        "symbol1": metadata["symbol1"],
    }

    if pool_type == "v4":
        return V4Pool(w3, chain, metadata["identifier"], metadata["fee"],
                      metadata["tick_spacing"], metadata["hooks"], **common)
    if pool_type == "v3":
        # fee passed in, so V3Pool skips its own fee() call too.
        return V3Pool(w3, chain, metadata["identifier"], fee=metadata["fee"], **common)
    return V2Pool(w3, chain, metadata["identifier"], **common)
