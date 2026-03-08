"""
Live execution test — places real orders on OKX to verify SL/TP placement.

Usage:
    python tests/test_execution_live.py [--cleanup]

What it does:
    1. Configures ETH/USDT (isolated margin, 3x leverage)
    2. Places a limit BUY at ask+0.1% (fills immediately like market but with limit)
    3. Waits for fill, then places SL and TP
    4. Verifies all orders are visible
    5. Cancels everything and closes position

This tests the full order placement path WITHOUT needing the pipeline.
Uses ~$7 of margin (0.01 ETH × 3x leverage).
Requires OKX_API_KEY, OKX_SECRET, OKX_PASSPHRASE in .env.
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from execution_service.executor import OrderExecutor


async def cleanup_only():
    """Cancel all open orders and close any position for ETH/USDT."""
    print("CLEANUP MODE — cancelling orders and closing positions...")
    executor = OrderExecutor()
    pair = "ETH/USDT"

    # Close any open position
    pos = await executor.fetch_position(pair)
    if pos and float(pos.get("contracts", 0)) > 0:
        side = pos.get("side", "")
        close_side = "sell" if side == "long" else "buy"
        contracts = float(pos["contracts"])
        print(f"Closing position: {side} {contracts} contracts")
        await executor.close_position_market(pair, close_side, contracts)
        print("Position closed")
    else:
        print("No open position")

    print("Cleanup complete")


async def main():
    if "--cleanup" in sys.argv:
        await cleanup_only()
        return

    print("=" * 60)
    print("LIVE EXECUTION TEST — OKX")
    print(f"Mode: {'SANDBOX' if settings.OKX_SANDBOX else 'LIVE'}")
    print(f"Margin mode: {settings.MARGIN_MODE}")
    print("=" * 60)

    if not settings.OKX_API_KEY:
        print("ERROR: OKX_API_KEY not set in .env")
        return

    executor = OrderExecutor()
    pair = "ETH/USDT"
    leverage = 3
    size = 0.01  # Minimum ETH size (~$19 notional, ~$7 margin at 3x)

    # Step 1: Configure pair
    print(f"\n[1] Configuring {pair} (isolated, {leverage}x)...")
    ok = await executor.configure_pair(pair, leverage)
    if not ok:
        print("FAILED to configure pair. Aborting.")
        return
    print("OK")

    # Step 2: Get current price
    print(f"\n[2] Fetching current price...")
    ticker = await executor.fetch_ticker(pair)
    if ticker is None:
        print("FAILED to fetch ticker. Aborting.")
        return
    current = float(ticker.get("last", 0))
    ask = float(ticker.get("ask", 0))
    print(f"Current: ${current:.2f} | Ask: ${ask:.2f}")

    # Step 3: Place entry at ask + 0.1% (will fill immediately)
    entry_price = round(ask * 1.001, 2)
    sl_price = round(current - 40, 2)    # $40 below = safe SL
    tp_price = round(current + 40, 2)    # $40 above

    print(f"\n[3] Placing limit buy @ ${entry_price:.2f} (ask+0.1%, will fill immediately)...")
    entry_order = await executor.place_limit_order(pair, "buy", size, entry_price)
    if entry_order is None:
        print("FAILED to place entry. Aborting.")
        return
    entry_id = entry_order.get("id")
    print(f"OK — order ID: {entry_id}")

    # Step 4: Wait for fill
    print("\n[4] Waiting for fill...")
    filled = False
    for i in range(10):
        await asyncio.sleep(1)
        order_status = await executor.fetch_order(entry_id, pair)
        if order_status and order_status.get("status") == "closed":
            actual_price = float(order_status.get("average", 0))
            filled_size = float(order_status.get("filled", 0))
            print(f"FILLED! Price: ${actual_price:.2f} | Size: {filled_size}")
            filled = True
            break
        print(f"  Waiting... ({i+1}/10)")

    if not filled:
        print("Entry did not fill in 10s. Cancelling...")
        await executor.cancel_order(entry_id, pair)
        return

    # Step 5: Place SL
    print(f"\n[5] Placing SL (stop-market sell) @ ${sl_price:.2f}...")
    sl_order = await executor.place_stop_market(pair, "sell", size, sl_price)
    if sl_order is None:
        print("CRITICAL: SL placement FAILED!")
        print("Emergency closing position...")
        await executor.close_position_market(pair, "sell", size)
        return
    sl_id = sl_order.get("id")
    print(f"OK — SL order ID: {sl_id}")

    # Step 6: Place TP
    print(f"\n[6] Placing TP (limit sell reduceOnly) @ ${tp_price:.2f}...")
    tp_order = await executor.place_take_profit(pair, "sell", size, tp_price)
    tp_id = None
    if tp_order is None:
        print("WARNING: TP placement failed. SL still active.")
    else:
        tp_id = tp_order.get("id")
        print(f"OK — TP order ID: {tp_id}")

    # Step 7: Verify all orders
    print(f"\n{'=' * 60}")
    print("RESULTS:")
    print(f"  Entry: FILLED at ${actual_price:.2f}")
    print(f"  SL:    {'OK' if sl_order else 'FAIL'} — trigger=${sl_price:.2f}")
    print(f"  TP:    {'OK' if tp_order else 'FAIL'} — price=${tp_price:.2f}")

    # Verify SL is visible
    print(f"\n[7] Verifying SL order on exchange...")
    sl_check = await executor.fetch_order(sl_id, pair)
    if sl_check:
        print(f"  SL status: {sl_check.get('status')} — VISIBLE ON EXCHANGE")
    else:
        print(f"  SL status: NOT FOUND — PROBLEM!")

    if tp_id:
        print(f"\n[8] Verifying TP order on exchange...")
        tp_check = await executor.fetch_order(tp_id, pair)
        if tp_check:
            print(f"  TP status: {tp_check.get('status')} — VISIBLE ON EXCHANGE")
        else:
            print(f"  TP status: NOT FOUND — PROBLEM!")

    print(f"\n{'=' * 60}")
    print("TEST COMPLETE — Now cleaning up...")
    print(f"{'=' * 60}")

    # Step 8: Cleanup — cancel SL/TP and close position
    print("\n[9] Cancelling SL...")
    await executor.cancel_order(sl_id, pair)

    if tp_id:
        print("[10] Cancelling TP...")
        await executor.cancel_order(tp_id, pair)

    print("[11] Closing position at market...")
    close_result = await executor.close_position_market(pair, "sell", size)
    if close_result:
        print("Position closed successfully")
    else:
        print("WARNING: Market close failed — check OKX manually!")

    # Verify position is gone
    await asyncio.sleep(1)
    pos = await executor.fetch_position(pair)
    if pos and float(pos.get("contracts", 0)) > 0:
        print(f"WARNING: Position still open — {pos.get('contracts')} contracts remaining!")
    else:
        print("Position confirmed closed. All clean.")

    print(f"\n{'=' * 60}")
    print("ALL DONE")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
