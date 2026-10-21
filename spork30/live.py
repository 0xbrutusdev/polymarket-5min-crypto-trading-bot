"""Live gateway.

`python -m spork30.live` checks the account and sends nothing.
`python -m spork30.live --run` starts the order loop. That path is the one
that signs.
"""

from __future__ import annotations

import json
import os
import urllib.request
from decimal import Decimal
from typing import Callable

from spork30.config import AppConfig, TradingMode, load_config


class LiveTradingDisabled(RuntimeError):
    pass


Signer = Callable[[dict], str]


def assert_live_allowed(config: AppConfig) -> None:
    if config.trading_mode is not TradingMode.LIVE:
        raise LiveTradingDisabled("TRADING_MODE is not LIVE")
    if os.environ.get("LIVE_TRADING_CONFIRM") != "YES":
        raise LiveTradingDisabled("LIVE_TRADING_CONFIRM must be YES")
    if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        raise LiveTradingDisabled("POLYMARKET_PRIVATE_KEY is not set")


def submit(config: AppConfig, payload: dict, signer: Signer | None) -> str:
    """Build is the caller's job. Sending requires the live gates and a signer.

    `payload` is the order or CTF action already accepted by the risk layer.
    The signer returns a transaction or order id. The live loop passes the
    Polymarket venue. Tests pass a fake signer. There is no signer when this
    function is called without one.
    """
    assert_live_allowed(config)
    if signer is None:
        raise LiveTradingDisabled("no signer injected; refusing to broadcast")
    if "slug" not in payload or "kind" not in payload:
        raise LiveTradingDisabled("refusing unsigned incomplete payload")
    return signer(payload)


_SECRET_ENV = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_BUILDER_SECRET",
    "POLYMARKET_BUILDER_PASSPHRASE",
    "POLYMARKET_BUILDER_API_KEY",
    "POLYMARKET_RELAYER_API_KEY",
)


def _redact(text: str) -> str:
    """Strip every configured secret from a message before it is logged or shown.

    The previous version only knew about the private key; the builder secret
    and passphrase could reach the decision log through an SDK error string.
    """
    for name in _SECRET_ENV:
        secret = os.environ.get(name) or ""
        if len(secret) >= 8 and secret in text:
            text = text.replace(secret, f"[{name.lower()} redacted]")
    return text


def _eoa_address(private_key: str) -> str:
    from eth_account import Account

    return Account.from_key(private_key).address


def _funder_owner(rpc: str, funder: str) -> str | None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": funder, "data": "0x8da5cb5b"}, "latest"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        rpc,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "spork30"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = str(payload.get("result") or "")
    if not result.startswith("0x") or len(result) < 42 or int(result, 16) == 0:
        return None
    return "0x" + result[-40:]


def read_collateral(config: AppConfig) -> Decimal:
    """USDC the funder holds on the CLOB, in whole dollars. Sends nothing."""
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    client = ClobClient(
        "https://clob.polymarket.com",
        chain_id=137,
        key=os.environ.get("POLYMARKET_PRIVATE_KEY") or "",
        signature_type=config.signature_type,
        funder=config.funder,
        use_server_time=True,
    )
    try:
        creds = client.derive_api_key()
    except Exception:
        creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)
    balance = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw = balance.get("balance") if isinstance(balance, dict) else balance
    return Decimal(str(raw or "0")) / Decimal("1000000")


def live_preflight(config: AppConfig, *, collateral: Decimal | None, open_markets: int) -> list[str]:
    """Reasons the live loop must not start. Empty means go.

    Quoting off the Polymarket touch (QUOTE_MODE=touch, or FAIR_EDGE=0)
    replayed at -12.4 USDC per window on 288 windows, Oct 12-13 2026, but
    the replay cannot price a touch quoter (docs/reference-gap-2026-10.md
    §3.4), and the reference account sells the touch. It needs an explicit
    ALLOW_TOUCH_QUOTING=YES until the simulation comparison passes. A split the
    funder cannot pay for reverts on the relayer and halts the window, so an
    empty wallet is refused unless there are open markets left to unwind.
    """
    problems: list[str] = []
    touch = config.strategy.quote_mode == "touch" or config.strategy.fair_edge <= 0
    if touch and os.environ.get("ALLOW_TOUCH_QUOTING") != "YES":
        problems.append("touch quoting (QUOTE_MODE=touch or FAIR_EDGE=0) is not validated against a real book in "
                        "live; set ALLOW_TOUCH_QUOTING=YES to opt in")
    if collateral is not None and collateral < config.strategy.split_size and open_markets == 0:
        problems.append(f"collateral {collateral.quantize(Decimal('0.01'))} USDC is below SPLIT_SIZE {config.strategy.split_size}")
    return problems


def account_report(config: AppConfig | None = None) -> list[str]:
    """Authenticate and read collateral. Never posts an order."""
    config = config or load_config()
    lines = [
        f"mode {config.trading_mode.value}",
        f"confirm {'yes' if os.environ.get('LIVE_TRADING_CONFIRM') == 'YES' else 'no'}",
        f"signature_type {config.signature_type}",
        f"strategy split {config.strategy.split_size} lead {config.strategy.split_lead_seconds}s redeem {config.strategy.redeem_delay_seconds}s merge {config.strategy.flatten_delay_seconds}s",
    ]
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY") or ""
    if not private_key or not config.funder or not config.polygon_rpc:
        lines.append("blocked: private key, funder, and POLYGON_RPC are all required")
        return lines
    if config.signature_type != 3:
        lines.append("blocked: this deposit wallet needs POLYMARKET_SIGNATURE_TYPE=3")
        return lines
    try:
        eoa = _eoa_address(private_key)
        owner = _funder_owner(config.polygon_rpc, config.funder)
    except Exception as exc:
        lines.append(f"blocked: {_redact(str(exc))}")
        return lines
    lines.append(f"eoa {eoa}")
    lines.append(f"funder {config.funder}")
    lines.append(f"funder_owner {owner or 'not a contract with owner()'}")
    if owner is None or owner.lower() != eoa.lower():
        lines.append("blocked: the private key is not the owner of POLYMARKET_FUNDER")
        return lines
    try:
        collateral = read_collateral(config)
    except Exception as exc:
        lines.append(f"clob auth failed: {_redact(str(exc))}")
        return lines
    lines.append(f"collateral {collateral.quantize(Decimal('0.01'))} USDC")
    lines.append("clob auth ok")
    problems = live_preflight(config, collateral=collateral, open_markets=0)
    for problem in problems:
        lines.append(f"preflight: {problem}")
    lines.append("no order was sent")
    if config.trading_mode is not TradingMode.LIVE or os.environ.get("LIVE_TRADING_CONFIRM") != "YES":
        lines.append("send gate closed")
    elif problems:
        lines.append("send gate open, but --run will refuse to start until the preflight passes")
    else:
        lines.append("send gate open; python -m spork30.live --run starts the loop")
    return lines


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Check the account, or run the live loop")
    parser.add_argument("--run", action="store_true", help="sign and send the strategy loop")
    parser.add_argument("--init", action="store_true", help="clear the live decision log and state before --run")
    args = parser.parse_args()
    if args.run:
        from spork30.config import load_config, reset_runtime
        from spork30.loop import run_live

        if args.init:
            reset_runtime(load_config())
        run_live()
        return
    if args.init:
        from spork30.config import load_config, reset_runtime

        reset_runtime(load_config())
        print("cleared the decision log and state")
        return
    for line in account_report():
        print(line)


if __name__ == "__main__":
    main()
