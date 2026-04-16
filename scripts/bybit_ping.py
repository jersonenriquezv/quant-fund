"""Bybit connection test. Validates API key, permissions, account type."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pybit.unified_trading import HTTP

from config.settings import Settings


def main() -> int:
    s = Settings()
    if not s.BYBIT_API_KEY or not s.BYBIT_API_SECRET:
        print("[ERROR] BYBIT_API_KEY / BYBIT_API_SECRET missing in config/.env")
        return 1

    print(f"testnet: {s.BYBIT_TESTNET}")
    print(f"key prefix: {s.BYBIT_API_KEY[:6]}***")

    client = HTTP(
        testnet=s.BYBIT_TESTNET,
        api_key=s.BYBIT_API_KEY,
        api_secret=s.BYBIT_API_SECRET,
    )

    print("\n[1] Account info...")
    try:
        info = client.get_account_info()
        result = info.get("result", {})
        print(f"  unifiedMarginStatus: {result.get('unifiedMarginStatus')}")
        print(f"  marginMode: {result.get('marginMode')}")
        print(f"  isMasterTrader: {result.get('isMasterTrader')}")
    except Exception as exc:
        print(f"  [FAIL] {exc}")
        return 2

    print("\n[2] API key info...")
    try:
        key = client.get_api_key_information()
        result = key.get("result", {})
        print(f"  readOnly: {result.get('readOnly')}")
        print(f"  permissions: {result.get('permissions')}")
        print(f"  ips: {result.get('ips')}")
        print(f"  expiredAt: {result.get('expiredAt')}")
    except Exception as exc:
        print(f"  [FAIL] {exc}")
        return 3

    print("\n[3] Wallet balance (UTA)...")
    try:
        bal = client.get_wallet_balance(accountType="UNIFIED")
        result = bal.get("result", {})
        for acct in result.get("list", []):
            equity = acct.get("totalEquity")
            print(f"  totalEquity: {equity} USD")
    except Exception as exc:
        print(f"  [FAIL] {exc}")
        return 4

    print("\n[4] Recent executions (last 7d, linear)...")
    try:
        fills = client.get_executions(category="linear", limit=5)
        data = fills.get("result", {}).get("list", [])
        print(f"  found: {len(data)} fills")
        for f in data[:3]:
            print(f"    {f.get('symbol')} {f.get('side')} qty={f.get('execQty')} px={f.get('execPrice')} ts={f.get('execTime')}")
    except Exception as exc:
        print(f"  [FAIL] {exc}")
        return 5

    print("\n[5] Closed PnL (last 7d, linear)...")
    try:
        pnl = client.get_closed_pnl(category="linear", limit=5)
        data = pnl.get("result", {}).get("list", [])
        print(f"  found: {len(data)} closed positions")
        for p in data[:3]:
            print(f"    {p.get('symbol')} side={p.get('side')} pnl={p.get('closedPnl')} ts={p.get('updatedTime')}")
    except Exception as exc:
        print(f"  [FAIL] {exc}")
        return 6

    print("\n[OK] All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
