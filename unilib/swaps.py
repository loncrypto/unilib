"""
Swap execution - buying and selling through a chain's routers.

Deliberately kept apart from pools.py: reading prices needs no wallet, and a price
tracker should never have to touch a private key. Only this module does.

Buying and selling are separate methods rather than one swap(direction=...) call,
because they genuinely differ:

              buy (base -> token)        sell (token -> base)
  input       native coin as msg.value   an ERC20
  approve     not needed                 required
  output      an ERC20                   wrapped native, unless unwrapped
  V2 function swapExactETHForTokens      swapExactTokensForETH

Forcing those into one function would just move the branching inside it.
"""
import time
from dataclasses import dataclass

from web3 import Web3

from . import abis
from .chains import NATIVE_ADDRESS

MAX_UINT256 = 2**256 - 1
DEFAULT_DEADLINE_SECONDS = 120
DEFAULT_SLIPPAGE_PCT = 0.5


@dataclass
class TxResult:
    """
    Outcome of an attempted transaction.

    Carries the failure reason instead of raising, so a monitoring loop can log it
    and retry rather than crashing mid-run.
    """

    success: bool
    tx_hash: str | None = None
    error: str | None = None
    amount_out: float | None = None

    def __bool__(self):
        return self.success


class Swapper:
    """
    Sends swaps for one chain with one wallet.

    `account` is an eth_account LocalAccount. How it is stored (keyring, env, a
    hardware wallet) is the caller's business - this library never reads keys itself.
    """

    def __init__(self, chain, account, w3=None):
        self.chain = chain
        self.account = account
        self.w3 = w3 or chain.connect()
        self._v3_variant_cache = None

    @property
    def address(self):
        return self.account.address

    # -- routers ------------------------------------------------------------

    def v3_variant(self):
        """
        Which V3 router interface this chain deployed: "classic" or "router02".

        Read from config when set, otherwise detected once from the router's bytecode
        and cached. Detection matters because the two interfaces take different
        arguments, and calling the wrong one fails on an unknown selector.
        """
        if self.chain.v3_router_variant:
            return self.chain.v3_router_variant
        if self._v3_variant_cache:
            return self._v3_variant_cache

        code = self.w3.eth.get_code(Web3.to_checksum_address(self.chain.v3_router)).hex()
        classic = Web3.keccak(text=abis.V3_EXACT_INPUT_SINGLE_CLASSIC)[:4].hex()
        router02 = Web3.keccak(text=abis.V3_EXACT_INPUT_SINGLE_ROUTER02)[:4].hex()

        if classic in code:
            self._v3_variant_cache = "classic"
        elif router02 in code:
            self._v3_variant_cache = "router02"
        else:
            raise ValueError(
                f"{self.chain.v3_router} bilinen bir V3 router arayuzune uymuyor "
                "(ne klasik ne router02 exactInputSingle bulundu)"
            )
        return self._v3_variant_cache

    def _v3_router(self):
        if not self.chain.v3_router:
            raise ValueError(f"{self.chain.name} icin v3_router adresi tanimli degil")
        abi = abis.V3_ROUTER_ABI if self.v3_variant() == "classic" else abis.V3_ROUTER02_ABI
        return self.w3.eth.contract(address=self.chain.v3_router, abi=abi)

    def _exact_input_params(self, token_in, token_out, fee, recipient, amount_in_wei,
                            min_out_wei, deadline):
        """Params tuple for exactInputSingle, shaped for whichever interface is deployed."""
        common = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            recipient,
        )
        tail = (amount_in_wei, min_out_wei, 0)
        if self.v3_variant() == "classic":
            return common + (deadline,) + tail
        return common + tail

    def _v2_router(self):
        if not self.chain.v2_router:
            raise ValueError(f"{self.chain.name} icin v2_router adresi tanimli degil")
        return self.w3.eth.contract(address=self.chain.v2_router, abi=abis.V2_ROUTER_ABI)

    def _router_for(self, pool):
        """Router contract for this pool's version. Raises if the chain has none configured."""
        if pool.pool_type == "v3":
            return self._v3_router()
        if pool.pool_type == "v2":
            return self._v2_router()
        raise NotImplementedError(_v4_message(self.chain))

    # -- allowance ----------------------------------------------------------

    def allowance(self, token_address, spender):
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=abis.ERC20_ABI
        )
        return token.functions.allowance(self.address, Web3.to_checksum_address(spender)).call()

    def ensure_allowance(self, token_address, amount_wei, spender, unlimited=True):
        """
        Approve the router to spend a token, if it cannot already.

        Defaults to an unlimited approval so this costs one transaction ever, rather
        than one before every sell. The trade-off is that the router keeps standing
        permission to move that token - acceptable for a router whose address you
        have verified, but pass unlimited=False to approve only this trade's amount.

        Returns a TxResult; already-approved is a success with no tx_hash.
        """
        spender = Web3.to_checksum_address(spender)
        if self.allowance(token_address, spender) >= amount_wei:
            return TxResult(success=True)

        try:
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address), abi=abis.ERC20_ABI
            )
            amount = MAX_UINT256 if unlimited else amount_wei
            tx = token.functions.approve(spender, amount).build_transaction({
                "from": self.address,
                "nonce": self.w3.eth.get_transaction_count(self.address),
            })
            receipt = self._sign_and_send(tx)
            return TxResult(success=receipt.status == 1, tx_hash=receipt.transactionHash.hex())
        except Exception as e:
            return TxResult(success=False, error=f"approve basarisiz: {e}")

    # -- simulation ---------------------------------------------------------

    def simulate(self, pool, token_in, amount_in):
        """
        Ask the chain what this swap would actually return, without sending anything.

        This is an eth_call: the node runs the real router code against current state
        and hands back the result, costing nothing and changing nothing. Unlike a spot
        price it accounts for fees, price impact and any transfer tax - which is what
        makes it the right basis for a minimum-output figure.

        Note for sells: the router does a transferFrom, so an allowance must already
        exist or the simulation reverts. Approve first, then simulate.

        Returns the output amount in human units, or None if the call reverted.
        """
        is_native_in = token_in.lower() in (NATIVE_ADDRESS, self.chain.wrapped_native.lower())
        decimals_in = pool.decimals0 if token_in.lower() == pool.token0.lower() else pool.decimals1
        decimals_out = pool.decimals1 if token_in.lower() == pool.token0.lower() else pool.decimals0
        amount_in_wei = int(amount_in * 10**decimals_in)

        try:
            if pool.pool_type == "v3":
                router = self._v3_router()
                token_out = pool.token1 if token_in.lower() == pool.token0.lower() else pool.token0
                params = self._exact_input_params(
                    self._as_wrapped(token_in),
                    self._as_wrapped(token_out),
                    pool.fee,
                    self.address,
                    amount_in_wei,
                    0,
                    int(time.time()) + DEFAULT_DEADLINE_SECONDS,
                )
                out_wei = self._call_with_balance(
                    router.functions.exactInputSingle(params),
                    value=amount_in_wei if is_native_in else 0,
                )
                return out_wei / 10**decimals_out

            if pool.pool_type == "v2":
                # V2's own getAmountsOut is a view function and already exact for
                # ordinary tokens. It cannot see a transfer tax, so treat its answer
                # as an upper bound on fee-on-transfer tokens.
                router = self._v2_router()
                token_out = pool.token1 if token_in.lower() == pool.token0.lower() else pool.token0
                path = [
                    Web3.to_checksum_address(self._as_wrapped(token_in)),
                    Web3.to_checksum_address(self._as_wrapped(token_out)),
                ]
                amounts = router.functions.getAmountsOut(amount_in_wei, path).call()
                return amounts[-1] / 10**decimals_out

            raise NotImplementedError(_v4_message(self.chain))
        except NotImplementedError:
            raise
        except Exception:
            return None

    def _call_with_balance(self, fn, value=0):
        """
        eth_call that does not fail merely because the wallet is short of funds.

        A value-bearing call is balance-checked by the node, so simulating a buy from
        an empty or underfunded wallet errors out before the router logic ever runs -
        which would make it impossible to ask "what would I get for 5 HYPE?" without
        already holding 5 HYPE. state_override tells the node to pretend the balance
        is there for this call only; nothing is sent and no state changes.

        Not every RPC implements state_override, so this falls back to a plain call.
        """
        tx = {"from": self.address, "value": value}
        if value:
            headroom = value * 2 + 10**18  # cover the value plus gas
            override = {self.address: {"balance": hex(headroom)}}
            try:
                return fn.call(tx, state_override=override)
            except (TypeError, ValueError) as e:
                # TypeError: older web3 without the parameter.
                # ValueError: RPC rejecting the override (unsupported method).
                if "state_override" not in str(e) and not isinstance(e, TypeError):
                    raise
        return fn.call(tx)

    def _as_wrapped(self, address):
        """Routers speak in wrapped-native terms; the zero address only means native as msg.value."""
        if address.lower() == NATIVE_ADDRESS:
            return self.chain.wrapped_native
        return address

    def _min_out(self, pool, token_in, amount_in, slippage_pct, min_out):
        """
        Work out the minimum acceptable output.

        Prefers a simulation, because a spot-price estimate ignores price impact and
        so produces a floor the trade cannot actually meet - which is exactly how a
        swap ends up reverting with "Too little received".
        """
        if min_out is not None:
            return min_out, None
        expected = self.simulate(pool, token_in, amount_in)
        if expected is None:
            # Simulation unavailable (no router configured, or it reverted) - fall
            # back to the pool's own quote, which is exact on V2 and an estimate on V3.
            expected = pool.quote(token_in, amount_in)
        return expected * (1 - slippage_pct / 100), expected

    # -- buying -------------------------------------------------------------

    def buy(self, pool, amount_in, slippage_pct=DEFAULT_SLIPPAGE_PCT, min_out=None,
            fee_on_transfer=False, deadline_seconds=DEFAULT_DEADLINE_SECONDS):
        """
        Spend the chain's native coin to buy the pool's tracked token.

        No approval is involved: native coin travels as msg.value and the router
        wraps it, so there is no ERC20 for anyone to need permission over.

        Set fee_on_transfer=True for tokens that take a cut on transfer ("vergili"
        tokens) - on V2 those revert through the plain function. It is harmless on
        ordinary tokens too, just slightly more gas.
        """
        # Resolved up front, outside the try: a missing router is a configuration
        # problem, not a failed trade, so it should raise rather than come back as a
        # retryable TxResult that no amount of retrying will fix.
        router = self._router_for(pool)

        token_in = self.chain.wrapped_native
        min_out_human, expected = self._min_out(pool, token_in, amount_in, slippage_pct, min_out)

        amount_in_wei = int(amount_in * 10**18)
        min_out_wei = int(min_out_human * 10**pool.decimals)
        deadline = int(time.time()) + deadline_seconds

        try:
            if pool.pool_type == "v3":
                params = self._exact_input_params(
                    self.chain.wrapped_native, pool.token, pool.fee,
                    self.address, amount_in_wei, min_out_wei, deadline,
                )
                swap_fn = router.functions.exactInputSingle(params)
                tx = self._v3_tx(router, swap_fn, deadline, value=amount_in_wei)
            elif pool.pool_type == "v2":
                path = [
                    Web3.to_checksum_address(self.chain.wrapped_native),
                    Web3.to_checksum_address(pool.token),
                ]
                fn = (
                    router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens
                    if fee_on_transfer
                    else router.functions.swapExactETHForTokens
                )
                tx = fn(min_out_wei, path, self.address, deadline).build_transaction(
                    self._tx_params(value=amount_in_wei)
                )
            else:
                raise NotImplementedError(_v4_message(self.chain))

            receipt = self._sign_and_send(tx)
            return TxResult(
                success=receipt.status == 1,
                tx_hash=receipt.transactionHash.hex(),
                amount_out=expected,
            )
        except NotImplementedError:
            raise
        except Exception as e:
            return TxResult(success=False, error=str(e))

    # -- selling ------------------------------------------------------------

    def sell(self, pool, amount_in, slippage_pct=DEFAULT_SLIPPAGE_PCT, min_out=None,
             fee_on_transfer=False, unwrap=True, deadline_seconds=DEFAULT_DEADLINE_SECONDS,
             approve=True, unlimited_approve=True):
        """
        Sell the pool's tracked token back into the chain's native coin.

        Approval is handled first (unless approve=False), because the router has to
        pull the tokens out of the wallet - and because a simulation cannot run
        without it either.

        unwrap=True returns native coin rather than the wrapped ERC20. On V2 this is
        free: swapExactTokensForETH already unwraps. On V3 the router must be told to,
        which is done by batching an unwrapWETH9 call after the swap - otherwise the
        proceeds arrive as wrapped tokens.
        """
        # Resolved before anything else so a missing router raises here rather than
        # after an approval has already been sent.
        router = self._router_for(pool)
        amount_in_wei = int(amount_in * 10**pool.decimals)

        if approve:
            approval = self.ensure_allowance(
                pool.token, amount_in_wei, router.address, unlimited=unlimited_approve
            )
            if not approval:
                return approval

        min_out_human, expected = self._min_out(
            pool, pool.token, amount_in, slippage_pct, min_out
        )
        min_out_wei = int(min_out_human * 10**pool.base_decimals)
        deadline = int(time.time()) + deadline_seconds

        try:
            if pool.pool_type == "v3":
                # When unwrapping, proceeds must land on the router first so it has
                # something to unwrap, then unwrapWETH9 forwards the native coin on.
                recipient = router.address if unwrap else self.address
                params = self._exact_input_params(
                    pool.token, self.chain.wrapped_native, pool.fee,
                    recipient, amount_in_wei, min_out_wei, deadline,
                )
                swap_fn = router.functions.exactInputSingle(params)
                extra = None
                if unwrap:
                    extra = router.functions.unwrapWETH9(min_out_wei, self.address)
                tx = self._v3_tx(router, swap_fn, deadline, extra=extra)

            elif pool.pool_type == "v2":
                path = [
                    Web3.to_checksum_address(pool.token),
                    Web3.to_checksum_address(self.chain.wrapped_native),
                ]
                fn = (
                    router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens
                    if fee_on_transfer
                    else router.functions.swapExactTokensForETH
                )
                tx = fn(
                    amount_in_wei, min_out_wei, path, self.address, deadline
                ).build_transaction(self._tx_params())
            else:
                raise NotImplementedError(_v4_message(self.chain))

            receipt = self._sign_and_send(tx)
            return TxResult(
                success=receipt.status == 1,
                tx_hash=receipt.transactionHash.hex(),
                amount_out=expected,
            )
        except NotImplementedError:
            raise
        except Exception as e:
            return TxResult(success=False, error=str(e))

    # -- plumbing -----------------------------------------------------------

    def _v3_tx(self, router, swap_fn, deadline, extra=None, value=0):
        """
        Build the V3 transaction, hiding the difference between the two interfaces.

        classic:  deadline already sits inside the swap params, so a bare call works
                  and multicall is only needed to append something (like unwrapping).
        router02: the params have no deadline, so calls go through
                  multicall(deadline, data) - which is also where extra calls attach.

        Either way the caller just says "this swap, plus optionally this follow-up".
        """
        calls = [swap_fn]
        if extra is not None:
            calls.append(extra)

        if self.v3_variant() == "router02":
            data = [Web3.to_bytes(hexstr=c._encode_transaction_data()) for c in calls]
            fn = router.functions.multicall(deadline, data)
        elif len(calls) == 1:
            fn = swap_fn
        else:
            data = [Web3.to_bytes(hexstr=c._encode_transaction_data()) for c in calls]
            fn = router.functions.multicall(data)

        return fn.build_transaction(self._tx_params(value=value))

    def _tx_params(self, value=0):
        params = {
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
        }
        if value:
            params["value"] = value
        return params

    def _sign_and_send(self, tx):
        """
        Sign, broadcast, wait for the receipt.

        Gas is left to web3's estimate on purpose: estimation runs the transaction
        against current state first, so a trade that would revert fails here - before
        it is broadcast and before any gas is spent on it.
        """
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(tx_hash)


def _v4_message(chain):
    return (
        "V4 swap henuz desteklenmiyor. V4'te havuzlar tek bir PoolManager icinde "
        "yasiyor ve dogrudan swap icin unlock/callback mimarisi gerekiyor; siradan "
        "bir cuzdandan islem yapmanin yolu Universal Router uzerinden gecmek. "
        f"{chain.name} icin Universal Router adresi de henuz tanimli degil."
    )
