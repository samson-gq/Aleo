#!/usr/bin/env python3
"""Arcus market-making demo bot.

Strategy (simple two-sided quote / micro spread around the mark price):
  - Stream BBO over WebSocket; every tick, compute a symmetric quote band
    around the current best mid.
  - Maintain one BUY and one SELL resting GTT limit order at `spread_bps`
    from mid; cancel & replace when mid moves more than `reprice_bps`.
  - Arm the dead man's switch so a crash/cutoff auto-cancels all orders.
  - --dry-run prints decisions without signing/sending anything.

Config via env or CLI: MARKET_ID (default 1 = BTC-USD), SIZE (base units as
decimal string), SPREAD_BPS, REPRICE_BPS, LEVERAGE.

Run:  python bot/main.py [--dry-run]
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from arcus import TESTNET_REST, TESTNET_WS, ArcusClient, ArcusWS
from arcus.auth import load_or_create_key

KEY_FILE = Path(__file__).parent.parent / "keys" / "arcus_key.json"


class QuoteState:
    """Tracks our two resting orders and the last mid we quoted around."""

    def __init__(self):
        self.buy_id: str | None = None
        self.sell_id: str | None = None
        self.mid: float | None = None
        self.fills = 0

    def on_fill(self, msg):
        self.fills += 1
        print(f"[FILL] {json.dumps(msg, default=str)[:300]}")


async def run(args):
    # ---- credentials ----
    if args.dry_run:
        priv, api_key, acct_idx, addr = None, "DRY", 0, "0x" + "00" * 20
        client = ArcusClient(args.base)
    else:
        if not KEY_FILE.exists():
            sys.exit("No keys/arcus_key.json — run  python onboard.py  first.")
        priv, meta = load_or_create_key(KEY_FILE)
        api_key = meta["apiKey"]
        acct_idx = meta.get("accountIndex", 0)
        addr = meta["walletAddress"]
        client = ArcusClient(args.base, priv=priv, api_key=api_key,
                             account_index=acct_idx, address=addr)

    mkt = client.market(args.market_id)
    tick, step = mkt["tickSize"], mkt["stepSize"]
    name = mkt.get("marketDisplayName", f"market{args.market_id}")
    symbol = mkt.get("marketDisplayName") or f"{mkt.get('baseAsset','')}-{mkt.get('quoteAsset','USD')}"
    args.symbol = symbol
    min_size = Decimal(mkt.get("minOrderSize", "0"))
    min_notional = Decimal(mkt.get("minOrderNotional", "0"))
    print(f"Market {args.market_id} ({name}) tick={tick} step={step} size={args.size}")

    # sanity-check our size against stepSize / exchange minimums up front
    size = Decimal(args.size)
    if size / Decimal(step) != (size / Decimal(step)).to_integral_value():
        sys.exit(f"size {args.size} is not a multiple of stepSize {step}")
    if min_size and size < min_size:
        sys.exit(f"size {args.size} < minOrderSize {min_size}")
    if min_notional:
        est = size * Decimal(mkt.get("markPrice") or mkt["oraclePrice"])
        if est < min_notional:
            sys.exit(f"order notional ~{est:.2f} < minOrderNotional {min_notional}")

    state = QuoteState()

    async def snap(side: str, price: float) -> str:
        p = round(round(price / float(tick)) * float(tick), 10)
        if args.dry_run:
            fake_id = f"dry-{side}-{int(time.time()*1000)}"
            print(f"[DRY] place {side} {args.size} @ {p}")
            return fake_id
        res = await ws.place_order(args.market_id, side, args.size, str(p),
                                   tick, step)
        oid = res.get("result", {}).get("orderId") or res.get("orderId")
        print(f"[PLACE] {side} {args.size} @ {p} -> {oid} (status {res.get('status')})")
        return oid

    async def requote(mid: float):
        half = mid * args.spread_bps / 20_000   # each side gets half the spread
        bid, ask = mid - half, mid + half
        if state.buy_id:
            if not args.dry_run:
                await ws.cancel_order(args.market_id, state.buy_id)
            state.buy_id = None
        if state.sell_id:
            if not args.dry_run:
                await ws.cancel_order(args.market_id, state.sell_id)
            state.sell_id = None
        state.buy_id = await snap("BUY", bid)
        state.sell_id = await snap("SELL", ask)
        state.mid = mid

    # ---- websocket loop ----
    ws = ArcusWS(args.ws_url, priv=priv, api_key=None if args.dry_run else api_key,
                 account_index=acct_idx, address=addr)
    await ws.connect()

    def arm_switch():
        """Dead man's switch: server cancels our resting orders if we go silent.
        Deadline must be 5s..5min ahead (epoch µs); refresh every few seconds."""
        deadline = int(time.time() * 1_000_000) + args.switch_secs * 1_000_000
        client.schedule_cancel(deadline, args.market_id)

    if not args.dry_run:
        arm_switch()
        print(f"[SAFETY] dead man's switch armed ({args.switch_secs}s)")

    last_requote = 0.0
    stop = asyncio.Event()

    async def on_bbo(contents):
        nonlocal last_requote
        bb, ba = contents.get("bestBid"), contents.get("bestAsk")
        if not bb or not ba:
            return
        # bbo frames carry objects: {"price": "...", "size": "..."}
        bb, ba = float(bb["price"] if isinstance(bb, dict) else bb), \
                 float(ba["price"] if isinstance(ba, dict) else ba)
        mid = (bb + ba) / 2
        now = time.monotonic()
        moved = state.mid and abs(mid - state.mid) / state.mid * 10_000 >= args.reprice_bps
        stale = now - last_requote > args.refresh_secs
        if moved or stale or state.mid is None:
            last_requote = now
            await requote(mid)

    # wrap callback: our WS dispatch is sync, push into an async-safe path
    loop = asyncio.get_running_loop()

    def bbo_handler(contents):
        loop.create_task(on_bbo(contents))

    ws.on("bbo", bbo_handler)
    await ws.subscribe("bbo", symbol)    # per-market channels: `id` = market ticker
    if not args.dry_run:
        ws.on("userFills", state.on_fill)
        await ws.subscribe("userFills", extra={"address": addr, "market": symbol})

    print(f"Quoting {name}: spread={args.spread_bps}bps reprice>{args.reprice_bps}bps "
          f"(heartbeat {args.refresh_secs}s). Ctrl-C to stop.")

    def shutdown(*_):
        stop.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, shutdown)

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(2, args.switch_secs // 3))
        except asyncio.TimeoutError:
            if not args.dry_run:
                try:
                    arm_switch()      # refresh heartbeat
                except Exception as e:
                    print(f"[WARN] switch refresh failed: {e}")
    print("Shutting down…")
    if not args.dry_run:
        try:
            client.cancel_all(args.market_id)
            client.schedule_cancel(None)   # disarm after clean exit
            print("[SAFETY] orders canceled, switch disarmed")
        except Exception as e:
            print(f"[WARN] cleanup failed: {e} — dead man's switch will cancel all")
    await ws.close()
    print(f"fills seen: {state.fills}")


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no signed orders sent")
    ap.add_argument("--market-id", type=int, default=int(os.environ.get("MARKET_ID", 1)))
    ap.add_argument("--size", default=os.environ.get("SIZE", "0.001"))
    ap.add_argument("--spread-bps", type=float, default=float(os.environ.get("SPREAD_BPS", 20)))
    ap.add_argument("--reprice-bps", type=float, default=float(os.environ.get("REPRICE_BPS", 5)))
    ap.add_argument("--refresh-secs", type=float, default=30.0)
    ap.add_argument("--switch-secs", type=int, default=60,
                    help="dead man's switch deadline, 5..300s")
    ap.add_argument("--base", default=os.environ.get("ARCUS_BASE", TESTNET_REST))
    ap.add_argument("--ws-url", default=os.environ.get("ARCUS_WS", TESTNET_WS))
    return ap.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
