"""Async WebSocket client for Arcus: market data channels + signed order routing.

One socket at wss://api.arcus.xyz/v1/ws multiplexes subscriptions and RPC.

Subscribe (never authenticated):  {"type":"subscribe","channel":"bbo","id":"sub-1"}
  → server replies {"type":"subscribed", "contents": <initial snapshot>}
  → then streams  {"type":"channel_data","channel":"bbo","contents":{...}}

RPC (order routing):  {"type":"post","id":N,"request":{"type":"placeOrder",
     "payload":{...},"apiKey":"...","timestamp":"<ns>","signature":"<hex>"}}
Response echoes the numeric `id`; IDs must be strictly increasing per connection.
"""

import asyncio
import json
import time

import websockets

from .auth import sign_legacy, sign_typed_payload
from .client import SIDE, TIF, to_int


class ArcusWS:
    def __init__(self, url: str, priv=None, api_key: str | None = None,
                 account_index: int = 0, address: str | None = None):
        self.url = url
        self.priv = priv
        self.api_key = api_key
        self.account_index = account_index
        self.address = address
        self.ws = None
        self._req_id = 0
        self._subs = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: dict[str, list] = {}   # channel -> [callback]
        self._recv_task = None

    # ---------- connection ----------

    async def connect(self):
        self.ws = await websockets.connect(self.url, ping_interval=20)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
        if self.ws:
            await self.ws.close()

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                rid = msg.get("id")
                if isinstance(rid, int) and rid in self._pending:
                    fut = self._pending.pop(rid)
                    if not fut.done():
                        fut.set_result(msg)
                mtype = msg.get("type")
                # dispatch channel pushes: subscribed snapshot & channel_data
                if mtype in ("subscribed", "channel_data"):
                    contents = msg.get("contents", {})
                    channel = msg.get("channel", "")
                    for cb in self._listeners.get(channel, []):
                        try:
                            cb(contents)
                        except Exception:
                            pass
                elif mtype == "error":
                    for cb in self._listeners.get("_error", []):
                        cb(msg)
        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed:
            pass

    # ---------- primitives ----------

    async def _send_wait(self, envelope: dict, timeout: float = 10) -> dict:
        rid = envelope["id"]
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self.ws.send(json.dumps(envelope))
        return await asyncio.wait_for(fut, timeout)

    async def post(self, req_type: str, payload: dict, signature: str | None = None,
                   timestamp_ns: int | None = None, timeout: float = 10) -> dict:
        """Signed RPC (placeOrder/cancelOrder/scheduleCancel...). The response
        carries an HTTP-like `status` and either `result` or `error`."""
        self._req_id += 1
        request = {"type": req_type, "payload": payload}
        if signature is not None:
            request.update({"apiKey": self.api_key,
                            "timestamp": str(timestamp_ns or time.time_ns()),
                            "signature": signature})
        return await self._send_wait(
            {"type": "post", "id": self._req_id, "request": request}, timeout)

    async def get(self, req_type: str, payload: dict, timeout: float = 10) -> dict:
        self._req_id += 1
        return await self._send_wait(
            {"type": "get", "id": self._req_id,
             "request": {"type": req_type, "payload": payload}}, timeout)

    async def subscribe(self, channel: str, sub_id: str | int | None = None,
                        extra: dict | None = None):
        """Subscribe to a channel. Subscriptions are never authenticated.

        For per-market channels (bbo, trades, oraclePrices, l2Orderbook...) the
        frame's `id` field IS the market display name — e.g.
            subscribe("bbo", "BTC-USD")
        For account-scoped channels (orders, positions, userFills, account) pass
        the address via extra, e.g. subscribe("userFills", extra={"address": ...}).
        Register handlers first via .on(channel, cb); the initial snapshot (if
        any) arrives as a `subscribed` frame whose `contents` is the snapshot.
        """
        sub = {"type": "subscribe", "channel": channel}
        if sub_id is not None:
            sub["id"] = sub_id
        if extra:
            sub.update(extra)
        await self.ws.send(json.dumps(sub))

    def on(self, channel: str, callback):
        """Register a handler for a channel's contents (snapshot + updates)."""
        self._listeners.setdefault(channel, []).append(callback)

    async def authenticate(self):
        """Optional. NOT required before subscribing or placing signed orders;
        kept for completeness. Scheme 2: ts + 'authenticate' + canonical body."""
        ts = time.time_ns()
        body = {"address": self.address, "accountIndex": self.account_index}
        sig = sign_legacy(self.priv, ts, "authenticate", body)
        return await self.post("authenticate", body, sig, ts)

    async def schedule_cancel(self, deadline_us: int | None = None,
                              market_id: int | None = None) -> dict:
        """Dead man's switch over WS. deadline_us = absolute epoch µs, 5s..5min
        ahead; omit (None) to disarm. Resting orders are NOT canceled on socket
        drop — always arm this."""
        ts = time.time_ns()
        body = {"address": self.address, "accountIndex": self.account_index}
        if deadline_us is not None:
            body["time"] = deadline_us
        if market_id is not None:
            body["marketId"] = market_id
        sig = sign_legacy(self.priv, ts, "scheduleCancel", body)
        return await self.post("scheduleCancel", body, sig, ts)

    # ---------- trading over WS ----------

    async def place_order(self, market_id: int, side: str, quantity: str,
                          price: str, tick: str, step: str, tif: str = "GTT",
                          good_til_us: int | None = None,
                          reduce_only: int = 0) -> dict:
        if good_til_us is None:
            good_til_us = int(time.time() * 1_000_000) + 40 * 86_400 * 1_000_000
        ts = time.time_ns()
        payload = (f'{{"ad":"{self.address.lower()}","ai":{self.account_index},'
                   f'"ct":{ts},"g":{good_til_us * 1000},"m":{market_id},"op":1,'
                   f'"p":{to_int(price, tick)},"q":{to_int(quantity, step)},'
                   f'"r":{reduce_only},"s":{SIDE[side.upper()]},'
                   f'"t":{TIF[tif]},"v":1}}')
        sig = sign_typed_payload(self.priv, payload)
        body = {"address": self.address, "accountIndex": self.account_index,
                "marketId": market_id, "orderSide": side.upper(),
                "orderType": "LIMIT", "quantity": str(quantity), "price": str(price),
                "timeInForce": tif, "goodTilTime": str(good_til_us)}
        return await self.post("placeOrder", body, sig, ts)

    async def cancel_order(self, market_id: int, order_id: str) -> dict:
        ts = time.time_ns()
        payload = (f'{{"ad":"{self.address.lower()}","ai":{self.account_index},'
                   f'"ct":{ts},"id":"{order_id}","m":{market_id},"op":2,"v":1}}')
        sig = sign_typed_payload(self.priv, payload)
        body = {"address": self.address, "accountIndex": self.account_index,
                "marketId": market_id, "kind": "orderId", "orderId": order_id}
        return await self.post("cancelOrder", body, sig, ts)

    async def cancel_all_orders(self, market_id: int | None = None) -> dict:
        """Scheme 2 over WS: cancel every resting order for this subaccount."""
        ts = time.time_ns()
        body = {"address": self.address, "accountIndex": self.account_index}
        if market_id is not None:
            body["marketId"] = market_id
        sig = sign_legacy(self.priv, ts, "cancelAllOrders", body)
        return await self.post("cancelAllOrders", body, sig, ts)
