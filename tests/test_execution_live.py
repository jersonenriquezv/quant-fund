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
    """Cancel all open orders (including algo/trigger) and close any position for ETH/USDT."""
    print("CLEANUP MODE — cancelling orders and closing positions...")
    executor = OrderExecutor()
    pair = "ETH/USDT"
    inst_id = "ETH-USDT-SWAP"

    # Cancel all pending algo orders (trigger + conditional)
    for ord_type in ["trigger", "conditional"]:
        try:
            result = await executor._run_sync(
                executor._exchange.privateGetTradeOrdersAlgoPending,
                {"instType": "SWAP", "instId": inst_id, "ordType": ord_type}
            )
            orders = result.get("data", [])
            for o in orders:
                algo_id = o.get("algoId")
                print(f"Cancelling {ord_type} algo order: {algo_id}")
                await executor.cancel_algo_order(algo_id, pair)
            if not orders:
                print(f"No pending {ord_type} orders")
        except Exception as e:
            print(f"Error checking {ord_type} orders: {e}")

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
    leverage = settings.MAX_LEVERAGE  # Use real bot leverage (5x)

    # Calculate size: TRADE_CAPITAL_PCT % of balance as notional
    ticker_pre = await executor.fetch_ticker(pair)
    current_price = float(ticker_pre.get("last", 0))
    # Use $106 as test capital (approximate live balance)
    capital = 106.0
    notional = capital * settings.TRADE_CAPITAL_PCT
    size = round(notional / current_price, 4)
    margin = notional / leverage
    print(f"Position sizing: {settings.TRADE_CAPITAL_PCT*100:.0f}% of ${capital:.0f} = ${notional:.2f} notional = {size} ETH (margin ~${margin:.2f} at {leverage}x)")

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

    # Step 3: Place entry at ask + 0.1% with ATTACHED SL/TP
    entry_price = round(ask * 1.001, 2)
    sl_price = round(current - 40, 2)    # $40 below = safe SL
    tp_price = round(current + 40, 2)    # $40 above

    print(f"\n[3] Placing limit buy @ ${entry_price:.2f} WITH attached SL=${sl_price:.2f} TP=${tp_price:.2f}...")
    entry_order = await executor.place_limit_order(
        pair, "buy", size, entry_price,
        sl_trigger_price=sl_price, tp_price=tp_price,
    )
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

    # Step 5: Verify attached SL/TP on exchange
    print(f"\n[5] Checking for attached SL/TP orders (waiting 1s for OKX to create them)...")
    await asyncio.sleep(1)
    algos = await executor.find_pending_algo_orders(pair)
    sl_id = None
    tp_id = None

    print(f"  Found {len(algos)} pending algo orders:")
    for algo in algos:
        algo_id = algo.get("algoId", "")
        trigger = algo.get("triggerPx", "")
        sl_trigger = algo.get("slTriggerPx", "")
        tp_trigger = algo.get("tpTriggerPx", "")
        ord_type = algo.get("ordType", "")
        print(f"    algoId={algo_id} ordType={ord_type} triggerPx={trigger} slTriggerPx={sl_trigger} tpTriggerPx={tp_trigger}")

        # Match SL — check both triggerPx (trigger type) and slTriggerPx (conditional type)
        sl_px = float(sl_trigger or trigger or 0)
        if sl_px > 0 and abs(sl_px - sl_price) < 1:
            sl_id = algo_id
            print(f"    → Matched as SL")

    # Check for TP in open limit orders
    symbol = f"{pair}:USDT"
    open_orders = await executor._run_sync(executor._exchange.fetch_open_orders, symbol)
    for order in open_orders:
        o_price = float(order.get("price", 0) or 0)
        if o_price > 0 and abs(o_price - tp_price) < 1:
            tp_id = order.get("id")
            print(f"  TP found in open orders: id={tp_id} price={o_price:.2f}")

    # Step 6: Results
    print(f"\n{'=' * 60}")
    print("RESULTS:")
    print(f"  Entry: FILLED at ${actual_price:.2f}")
    print(f"  SL:    {'OK — ATTACHED' if sl_id else 'NOT FOUND — placing manually...'} trigger=${sl_price:.2f}")
    print(f"  TP:    {'OK — ATTACHED' if tp_id else 'NOT FOUND — placing manually...'} price=${tp_price:.2f}")

    # Fallback: place SL manually if not attached
    if not sl_id:
        sl_order = await executor.place_stop_market(pair, "sell", size, sl_price)
        if sl_order:
            sl_id = sl_order.get("id")
            print(f"  SL placed manually: {sl_id}")
        else:
            print("  CRITICAL: SL placement FAILED!")

    # Fallback: place TP manually if not attached
    if not tp_id:
        tp_order = await executor.place_take_profit(pair, "sell", size, tp_price)
        if tp_order:
            tp_id = tp_order.get("id")
            print(f"  TP placed manually: {tp_id}")
        else:
            print("  WARNING: TP placement failed")

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
