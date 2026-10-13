"""REST client for the Arcus exchange API.

Public endpoints need no auth; trading endpoints use:
  - Scheme 1 (typed payload) for placeOrder / cancelOrder / modifyOrder,
    sent via X-API-Key / X-Timestamp / X-Signature headers.
  - Scheme 2 (legacy message) for cancelAllOrders / setLeverage.
"""

import json
import time
import urllib.error
import urllib.request
from decimal import Decimal

from .auth import canonical, sign_legacy, sign_typed_payload

OP_PLACE, OP_CANCEL, OP_MODIFY = 1, 2, 3
SIDE = {"BUY": 0, "SELL": 1}
TIF = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}


def to_int(value, unit) -> int:
    """Exact decimal → integer ticks/quantums (price÷tickSize, size÷stepSize)."""
    n = Decimal(str(value)) / Decimal(str(unit))
    if n != n.to_integral_value():
        raise ValueError(f"{value} is not a multiple of {unit}")
    return int(n)


class ArcusError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class ArcusClient:
    """Thin synchronous REST client. Pass priv/api_key/account_index/address
    to enable signed trading calls; public reads work without them."""

    def __init__(self, base_url: str, priv=None, api_key: str | None = None,
                 account_index: int = 0, address: str | None = None, timeout: int = 15):
        self.base = base_url.rstrip("/")
        self.priv = priv
        self.api_key = api_key
        self.account_index = account_index
        self.address = address
        self.timeout = timeout

    # ---------- low level ----------

    def _request(self, method: str, path: str, body=None, headers=None):
        data = canonical(body) if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise ArcusError(e.code, e.read().decode(errors="replace")) from e

    def get(self, path: str, headers=None):
        return self._request("GET", path, headers=headers)

    def post(self, path: str, body: dict, headers=None):
        return self._request("POST", path, body, headers)

    def _auth_headers(self, ts_ns: int, sig: str) -> dict:
        return {"X-API-Key": self.api_key, "X-Timestamp": str(ts_ns),
                "X-Signature": sig}

    def _require_key(self):
        if not (self.priv and self.api_key and self.address):
            raise RuntimeError("signed call needs priv/api_key/address")

    # ---------- market data (public) ----------

    def markets(self, market_id: int | None = None):
        path = "/v1/markets" + (f"?market={market_id}" if market_id else "")
        return self.get(path)["markets"]

    def market(self, market_id: int) -> dict:
        return next(m for m in self.markets(market_id))

    def bbo(self, market: int):
        return self.get(f"/v1/bbo?market={market}")

    def l2(self, market: int, depth: int = 20):
        return self.get(f"/v1/l2/{market}?depth={depth}")

    def account(self, address: str | None = None):
        addr = (address or self.address).lower()
        return self.get(f"/v1/account?address={addr}",
                        headers={"X-API-Key": self.api_key} if self.api_key else None)

    def positions(self, address: str | None = None):
        addr = (address or self.address).lower()
        return self.get(f"/v1/positions?address={addr}")

    def open_orders(self, address: str | None = None):
        addr = (address or self.address).lower()
        return self.get(f"/v1/openOrders?address={addr}")

    # ---------- typed-payload builders (Scheme 1) ----------

    def _place_payload(self, market_id, side, price_ticks, qty_quantums,
                       good_til_us, tif, ts, client_id="", reduce_only=0):
        parts = [f'"ad":"{self.address.lower()}"', f'"ai":{self.account_index}']
        if client_id:
            parts.append(f'"c":"{client_id}"')          # omitted entirely when empty
        parts += [f'"ct":{ts}', f'"g":{good_til_us * 1000}', f'"m":{market_id}',
                  f'"op":{OP_PLACE}', f'"p":{price_ticks}', f'"q":{qty_quantums}',
                  f'"r":{reduce_only}', f'"s":SIDE[side.upper()]',
                  f'"t":{TIF[tif]}', '"v":1']
        return "{" + ",".join(parts) + "}"

    def _cancel_payload(self, market_id, order_id, ts, client_id=""):
        parts = [f'"ad":"{self.address.lower()}"', f'"ai":{self.account_index}']
        if client_id:
            parts.append(f'"c":"{client_id}"')
        parts.append(f'"ct":{ts}')
        if order_id and not client_id:
            parts.append(f'"id":"{order_id}"')           # exactly one of id / c
        parts += [f'"m":{market_id}', f'"op":{OP_CANCEL}', '"v":1']
        return "{" + ",".join(parts) + "}"

    # ---------- trading (signed) ----------

    def place_order(self, market_id: int, side: str, quantity: str, price: str,
                    tick: str, step: str, tif: str = "GTT",
                    good_til_us: int | None = None, client_id: str = "",
                    reduce_only: int = 0) -> dict:
        """LIMIT order. `quantity`/`price` are decimal strings that must be exact
        multiples of step/tick. goodTilTime must be ≥1 month ahead (µs epoch)."""
        self._require_key()
        if good_til_us is None:
            good_til_us = int(time.time() * 1_000_000) + 40 * 86_400 * 1_000_000
        ts = time.time_ns()
        payload = self._place_payload(market_id, side, to_int(price, tick),
                                      to_int(quantity, step), good_til_us, tif,
                                      ts, client_id, reduce_only)
        body = {
            "address": self.address, "accountIndex": self.account_index,
            "marketId": market_id, "orderSide": side.upper(), "orderType": "LIMIT",
            "quantity": str(quantity), "price": str(price), "timeInForce": tif,
            "goodTilTime": str(good_til_us), "timestamp": ts,
        }
        if client_id:
            body["clientId"] = client_id
        return self.post("/v1/placeOrder", body,
                         self._auth_headers(ts, sign_typed_payload(self.priv, payload)))

    def market_order(self, market_id: int, side: str, quantity: str,
                     limit_price: str, tick: str, step: str) -> dict:
        """MARKET = IOC with a protective price bound within 10% of mark."""
        self._require_key()
        good_til_us = int(time.time() * 1_000_000) + 40 * 86_400 * 1_000_000
        ts = time.time_ns()
        payload = self._place_payload(market_id, side, to_int(limit_price, tick),
                                      to_int(quantity, step), good_til_us, "IOC", ts)
        body = {
            "address": self.address, "accountIndex": self.account_index,
            "marketId": market_id, "orderSide": side.upper(), "orderType": "MARKET",
            "quantity": str(quantity), "price": str(limit_price),
            "timeInForce": "IOC", "goodTilTime": str(good_til_us), "timestamp": ts,
        }
        return self.post("/v1/placeOrder", body,
                         self._auth_headers(ts, sign_typed_payload(self.priv, payload)))

    def cancel_order(self, market_id: int, order_id: str | None = None,
                     client_id: str = "") -> dict:
        self._require_key()
        ts = time.time_ns()
        payload = self._cancel_payload(market_id, order_id, ts, client_id)
        body = {"address": self.address, "accountIndex": self.account_index,
                "marketId": market_id, "timestamp": ts}
        if client_id:
            body.update({"kind": "clientId", "clientId": client_id})
        else:
            body.update({"kind": "orderId", "orderId": order_id})
        return self.post("/v1/cancelOrder", body,
                         self._auth_headers(ts, sign_typed_payload(self.priv, payload)))

    def batch_place(self, orders: list[dict]) -> dict:
        """orders: dicts with keys market_id/side/quantity/price/tif — each signed
        individually under one shared timestamp; X-Signature = any element's sig."""
        self._require_key()
        ts = time.time_ns()
        good_til_us = int(time.time() * 1_000_000) + 40 * 86_400 * 1_000_000
        elements, sigs = [], []
        for o in orders:
            payload = self._place_payload(o["market_id"], o["side"],
                                          to_int(o["price"], o["tick"]),
                                          to_int(o["quantity"], o["step"]),
                                          good_til_us, o.get("tif", "GTT"), ts)
            sig = sign_typed_payload(self.priv, payload)
            sigs.append(sig)
            elements.append({
                "address": self.address, "accountIndex": self.account_index,
                "marketId": o["market_id"], "orderSide": o["side"].upper(),
                "orderType": "LIMIT", "quantity": str(o["quantity"]),
                "price": str(o["price"]), "timeInForce": o.get("tif", "GTT"),
                "goodTilTime": str(good_til_us), "timestamp": ts, "signature": sig})
        headers = self._auth_headers(ts, sigs[0])
        return self.post("/v1/batchPlaceOrders", {"orders": elements}, headers)

    def cancel_all(self, market_id: int | None = None) -> dict:
        """Scheme 2 legacy signing."""
        self._require_key()
        ts = time.time_ns()
        body = {"address": self.address, "accountIndex": self.account_index,
                "timestamp": ts}
        if market_id is not None:
            body["marketId"] = market_id
        sig = sign_legacy(self.priv, ts, "cancelAllOrders", body)
        return self.post("/v1/cancelAllOrders", body, self._auth_headers(ts, sig))

    def set_leverage(self, market_id: int, leverage: float) -> dict:
        self._require_key()
        ts = time.time_ns()
        body = {"address": self.address, "accountIndex": self.account_index,
                "marketId": market_id, "leverage": leverage, "timestamp": ts}
        sig = sign_legacy(self.priv, ts, "setLeverage", body)
        return self.post("/v1/setLeverage", body, self._auth_headers(ts, sig))

    def schedule_cancel(self, deadline_us: int | None = None,
                        market_id: int | None = None) -> dict:
        """Dead man's switch (POST /v1/scheduleCancel, Scheme 2). deadline_us =
        absolute epoch-microsecond deadline, 5s..5min ahead; None disarms.
        Omit market_id for an account-wide switch. Refresh before it elapses —
        resting orders are NOT canceled when a socket drops."""
        self._require_key()
        ts = time.time_ns()
        body = {"address": self.address, "accountIndex": self.account_index}
        if deadline_us is not None:
            body["time"] = deadline_us
        if market_id is not None:
            body["marketId"] = market_id
        sig = sign_legacy(self.priv, ts, "scheduleCancel", body)
        return self.post("/v1/scheduleCancel", body, self._auth_headers(ts, sig))
