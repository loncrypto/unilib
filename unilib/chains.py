"""
Chain configuration - the only thing that actually differs between EVM chains.

Uniswap's protocol is identical everywhere; only the addresses change. Keeping
those as data (rather than hardcoded in each project) is what makes the same code
work on Robinhood Chain, HyperEVM, Base, Arbitrum and so on.

Forks (PancakeSwap, HyperSwap, Project X...) are still Uniswap-shaped, so they fit
here too - they just may use a different V2 fee or lack V4 entirely.
"""
from dataclasses import dataclass, field, replace

from web3 import Web3

# V4 and most routers use the zero address to mean "the chain's native coin"
# (ETH, HYPE, BNB...) as opposed to its wrapped ERC20 form.
NATIVE_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class ChainConfig:
    """
    Addresses and constants for one chain.

    Anything set to None means "this chain does not have it, or it has not been
    verified yet" - the library raises a clear error instead of guessing when a
    feature needs a missing address.
    """

    name: str
    chain_id: int
    rpc_url: str
    wrapped_native: str
    native_symbol: str = "ETH"

    # V4: single PoolManager for all pools; StateView is its read-only view helper.
    state_view: str | None = None

    v3_router: str | None = None
    v2_router: str | None = None

    # Which interface v3_router exposes: "classic", "router02", or None to detect it
    # from bytecode on first use. A deployment often ships BOTH routers, so this
    # describes the address configured above - not the chain. Detection is the safer
    # default; the wrong guess just fails on an unknown selector.
    v3_router_variant: str | None = None

    # QuoterV2: the contract built for asking "what would this swap return", exactly,
    # including fees and price impact. Simulating through the router works too, but
    # the quoter is the purpose-built tool and needs no balance or allowance.
    v3_quoter: str | None = None

    # Uniswap V2 charges 0.3% (997/1000). Forks differ - PancakeSwap uses 0.25%.
    # A wrong value here silently skews every V2 quote, so it belongs with the chain.
    v2_fee_numerator: int = 997
    v2_fee_denominator: int = 1000

    # Tokens that count as the "base" side of a pair, beyond native and wrapped
    # native. Used to work out which side of a pool is the token being tracked.
    # Format: {"USDC": ("0x...", 6)}
    extra_base_tokens: dict = field(default_factory=dict)

    def __post_init__(self):
        # Address comparison bugs are silent and nasty, so normalise once here
        # rather than sprinkling .lower() through every call site.
        for attr in ("wrapped_native", "state_view", "v3_router", "v2_router", "v3_quoter"):
            value = getattr(self, attr)
            if value is not None:
                object.__setattr__(self, attr, Web3.to_checksum_address(value))

    @property
    def base_tokens(self):
        """
        All tokens treated as the quote/base side, as {symbol: (address, decimals)}.

        Native and wrapped native are always included; a chain can add stablecoins
        or anything else it commonly pairs against.
        """
        tokens = {
            self.native_symbol: (NATIVE_ADDRESS, 18),
            f"W{self.native_symbol}": (self.wrapped_native, 18),
        }
        tokens.update(self.extra_base_tokens)
        return tokens

    def is_base_token(self, address):
        """True if the address is one of this chain's base/quote assets."""
        address = (address or "").lower()
        return any(known.lower() == address for known, _ in self.base_tokens.values())

    def base_token_name(self, address):
        """Symbol for a base token address, or None if it is not a base token."""
        address = (address or "").lower()
        for symbol, (known, _) in self.base_tokens.items():
            if known.lower() == address:
                return symbol
        return None

    def with_rpc(self, rpc_url):
        """
        Same chain, different endpoint.

        The bundled rpc_url is a public endpoint: fine to start with, but rate-limited
        and shared. Anyone running this seriously will want their own (Alchemy,
        QuickNode, a local node), and that belongs in their project - not in the
        library. Returns a copy, so the shared config stays untouched.

            CHAIN = CHAINS[999].with_rpc("https://...")
        """
        return replace(self, rpc_url=rpc_url)

    def connect(self, verify=True):
        """
        Build a Web3 instance for this chain.

        Verifying the chain id catches a whole class of confusing bugs early -
        pointing at the wrong RPC otherwise just returns plausible-looking data
        from the wrong chain.
        """
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if verify:
            actual = w3.eth.chain_id
            if actual != self.chain_id:
                raise RuntimeError(
                    f"{self.name}: beklenen chain_id {self.chain_id}, gelen {actual}. "
                    "Yanlis RPC'ye mi baglaniliyor?"
                )
        return w3


ROBINHOOD = ChainConfig(
    name="Robinhood Chain",
    chain_id=4663,
    rpc_url="https://rpc.mainnet.chain.robinhood.com",
    wrapped_native="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
    native_symbol="ETH",
    # Official Uniswap V4 deployment - PoolManager is discoverable from StateView.
    state_view="0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
    # Found by looking at what successful swaps on a live V2 pool actually call, then
    # verified: has all four swap functions (including the fee-on-transfer variants),
    # its factory() matches the pool's, and its WETH() matches wrapped_native.
    v2_router="0x89e5DB8B5aA49aA85AC63f691524311AEB649eba",
    # v3_router still unverified on this chain.
)

HYPEREVM = ChainConfig(
    name="HyperEVM",
    chain_id=999,
    # The official endpoint (rpc.hyperliquid.xyz/evm) caps at 100 requests/minute and
    # kept rate-limiting during normal use. Measured on the same simulation call:
    #   drpc 357ms | official 792ms | hyperlend 1182ms
    # Other working public endpoints if this one degrades:
    #   https://rpc.hyperlend.finance
    #   https://rpc.purroofgroup.com
    #   https://hyperliquid-json-rpc.stakely.io
    # For anything serious, point a project at its own endpoint via chain.with_rpc().
    rpc_url="https://hyperliquid.drpc.org",
    # WHYPE is a canonical immutable system contract, same code as WETH.
    wrapped_native="0x5555555555555555555555555555555555555555",
    native_symbol="HYPE",
    # HyperSwap is an independent Uniswap fork, not an official Uniswap deployment.
    # No V4 found here, so state_view stays None.
    #
    # HyperSwap ships both routers (verified against their docs and on-chain: both
    # report this chain's V3 factory and WETH9):
    #   SwapRouter01  0x4E2960a8cd19B467b82d26D83fAcb0fAE26b094D  - classic interface
    #   SwapRouter02  0x6D99e7f6747AF2cDbB5164b6DD50e40D4fDe1e77  - router02 interface
    # 01 is configured because it is the one already proven in use here; switching to
    # 02 only means changing these two lines, the variant is handled automatically.
    v3_router="0x4e2960a8cd19b467b82d26d83facb0fae26b094d",
    v3_router_variant="classic",
    v3_quoter="0x03A918028f22D9E1473B7959C927AD7425A45C7C",  # QuoterV2
    # v2_router: two candidate addresses were checked and neither held a usable V2
    # router (one had no contract at all), so it stays unset rather than wrong.
    # V2 price reading still works without it - only V2 swaps need a router.
)

BASE = ChainConfig(
    name="Base",
    chain_id=8453,
    # mainnet.base.org rate-limits aggressively; publicnode held up better under
    # repeated calls. Swap in a paid endpoint if polling gets heavy.
    rpc_url="https://base-rpc.publicnode.com",
    # OP-stack standard predeploy.
    wrapped_native="0x4200000000000000000000000000000000000006",
    native_symbol="ETH",
    # Official Uniswap deployment; StateView.poolManager() verified to return
    # 0x498581fF718922c3f8e6A244956aF099B2652b2b.
    state_view="0xa3c0c9b65bad0b08107aa264b0f3db444b867a71",
    # SwapRouter02 - deadline is not in the params struct here.
    v3_router="0x2626664c2603336E57B271c5C0b26F421741e481",
    v3_router_variant="router02",
    v2_router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
    extra_base_tokens={
        "USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
    },
)

CHAINS = {
    ROBINHOOD.chain_id: ROBINHOOD,
    HYPEREVM.chain_id: HYPEREVM,
    BASE.chain_id: BASE,
}


def get_chain(chain_id):
    """Look up a known chain by id, with a readable error when it is not registered."""
    if chain_id not in CHAINS:
        known = ", ".join(f"{cid} ({c.name})" for cid, c in CHAINS.items())
        raise KeyError(f"chain_id {chain_id} tanimli degil. Bilinenler: {known}")
    return CHAINS[chain_id]
