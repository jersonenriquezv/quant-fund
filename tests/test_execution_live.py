"""
Live execution test — places real orders on OKX to verify SL/TP placement.

Usage:
    python tests/test_execution_live.py

What it does:
    1. Configures ETH/USDT (isolated margin, 3x leverage)
    2. Places a limit BUY order $20 below current price (won't fill immediately)
    3. Places a stop-market SL $40 below entry
    4. Places a limit TP $40 above entry (reduceOnly)
    5. Prints all order IDs
    6. Waits for confirmation, then cancels all orders

This tests the full order placement path WITHOUT needing the pipeline.
Requires OKX_API_KEY, OKX_SECRET, OKX_PASSPHRASE in .env.
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from execution_service.executor import OrderExecutor


async def main():
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

    # Step 1: Configure pair
    print(f"\n[1] Configuring {pair} (isolated, {leverage}x)...")
    ok = await executor.configure_pair(pair, leverage)
    if not ok:
        print("FAILED to configure pair. Aborting.")
        return
    print("OK — pair configured")

    # Step 2: Get current price
    print(f"\n[2] Fetching current price for {pair}...")
    ticker = await executor.fetch_ticker(pair)
    if ticker is None:
        print("FAILED to fetch ticker. Aborting.")
        return
    current = float(ticker.get("last", 0))
    bid = float(ticker.get("bid", 0))
    ask = float(ticker.get("ask", 0))
    print(f"Current: ${current:.2f} | Bid: ${bid:.2f} | Ask: ${ask:.2f}")

    # Step 3: Calculate prices — entry $20 below (won't fill)
    entry_price = round(current - 20, 2)
    sl_price = round(entry_price - 40, 2)
    tp_price = round(entry_price + 40, 2)
    size = 0.01  # Minimum ETH size

    print(f"\n[3] Order plan:")
    print(f"  Entry (limit buy): ${entry_price:.2f}")
    print(f"  SL (stop-market sell): ${sl_price:.2f}")
    print(f"  TP (limit sell reduceOnly): ${tp_price:.2f}")
    print(f"  Size: {size} ETH")
    print(f"  Direction: LONG")

    input("\nPress Enter to place orders (or Ctrl+C to abort)...")

    # Step 4: Place entry
    print(f"\n[4] Placing limit buy @ ${entry_price:.2f}...")
    entry_order = await executor.place_limit_order(pair, "buy", size, entry_price)
    if entry_order is None:
        print("FAILED to place entry order. Aborting.")
        return
    entry_id = entry_order.get("id")
    print(f"OK — entry order ID: {entry_id}")

    # Step 5: Place SL
    print(f"\n[5] Placing stop-market sell @ ${sl_price:.2f}...")
    sl_order = await executor.place_stop_market(pair, "sell", size, sl_price)
    if sl_order is None:
        print("FAILED to place SL. This is the critical bug we're testing.")
        print("Cancelling entry order...")
        await executor.cancel_order(entry_id, pair)
        return
    sl_id = sl_order.get("id")
    print(f"OK — SL order ID: {sl_id}")

    # Step 6: Place TP
    print(f"\n[6] Placing limit sell (TP) @ ${tp_price:.2f}...")
    tp_order = await executor.place_take_profit(pair, "sell", size, tp_price)
    if tp_order is None:
        print("FAILED to place TP. SL is still active.")
        print("Note: TP failure is non-critical — SL protects us.")
    else:
        tp_id = tp_order.get("id")
        print(f"OK — TP order ID: {tp_id}")

    # Step 7: Verify
    print(f"\n{'=' * 60}")
    print("RESULTS:")
    print(f"  Entry: {'OK' if entry_order else 'FAIL'} — ID: {entry_id}")
    print(f"  SL:    {'OK' if sl_order else 'FAIL'} — ID: {sl_id}")
    print(f"  TP:    {'OK' if tp_order else 'FAIL'} — ID: {tp_order.get('id') if tp_order else 'N/A'}")
    print(f"\nCheck OKX app/web to verify orders are visible.")
    print(f"{'=' * 60}")

    input("\nPress Enter to cancel all orders and clean up...")

    # Step 8: Cleanup
    print("\n[7] Cancelling all orders...")
    await executor.cancel_order(entry_id, pair)
    print(f"  Entry cancelled: {entry_id}")

    # SL is algo order — cancel via algo endpoint
    await executor.cancel_order(sl_id, pair)
    print(f"  SL cancelled: {sl_id}")

    if tp_order:
        await executor.cancel_order(tp_order.get("id"), pair)
        print(f"  TP cancelled: {tp_order.get('id')}")

    print("\nAll orders cancelled. Test complete.")


if __name__ == "__main__":
    asyncio.run(main())
