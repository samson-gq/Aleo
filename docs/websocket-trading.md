> ## Documentation Index
> Fetch the complete documentation index at: https://docs.arcus.xyz/llms.txt
> Use this file to discover all available pages before exploring further.

# Trade over WebSocket

> Register a key and place your first order over WebSocket — the primary trading interface

WebSocket is the primary way to trade on Arcus: one connection multiplexes order routing and every data channel, and each request is signed on its own. This quickstart gets you from nothing to a live order on testnet in three steps.

<Note>
  New here? The [REST track](/guides/rest-trading) is a gentler start. Requests target testnet (`https://api.testnet.arcus.xyz`); new accounts start empty — fund yours with the **Testnet Deposit** button (\~\$1,000), or an [on-chain deposit](/guides/fund-testnet-account) for more. See [Authentication](/api-reference/authentication) for signing.
</Note>

## Step 1: Install

Only published libraries — no Arcus package required.

```bash theme={null}
pip install cryptography eth-account websockets
```

## Step 2: Get an API key

Your API key is an **Ed25519 keypair**; the server stores only the public half, and the key is registered to your wallet by a one-time REST call authenticated by an **EIP-712 typed-data signature** (`eth_signTypedData_v4`) — `createApiKey` is REST-only, so even socket-first clients register once over REST (the legacy EIP-191 scheme is deprecated but still accepted during the migration window). The Arcus web app does all of this for you and is the quickest way to get a key; you can also do it entirely in code.

### Option A — Generate on the web app (recommended)

1. Open the API Keys page — [testnet](https://testnet.arcus.xyz/api-keys), or [mainnet](https://app.arcus.xyz/api-keys) for production — and connect the wallet you'll trade from.
2. Enter a name and click **Generate**. The app creates the keypair in your browser.
3. **Copy the API Signing Key now** — it's the private half, shown once, and cannot be recovered. Pick a **Subaccount #** (default `0`) and **Days Valid** (up to 180), then click **Authorize** and confirm the signature in your wallet.

That registers the key — nothing to run. Load the signing key you copied and skip to [Step 3](#step-3-place-an-order):

```python theme={null}
# guide-test: skip — placeholder values; Option B is the runnable path.
from cryptography.hazmat.primitives.asymmetric import ed25519

priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex("<API Signing Key>"))
api_key = priv.public_key().public_bytes_raw().hex()   # matches the "API Key" the app shows
account_index = 0                 # the Subaccount # you selected
wallet_address = "0x..."          # the wallet you connected
```

The steps below read the master address as `wallet.address`; with a web-app key, use your `wallet_address` wherever you see it.

### Option B — Generate in code

Generate a fresh keypair and register it with one `createApiKey` call — the same operation the web app performs:

```python theme={null}
import json, time, urllib.request
from cryptography.hazmat.primitives.asymmetric import ed25519
from eth_account import Account
from eth_account.messages import encode_defunct

BASE = "https://api.testnet.arcus.xyz"

def canonical(obj):
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()

def call(method, path, body=None, headers=None):
    data = canonical(body) if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")

priv = ed25519.Ed25519PrivateKey.generate()
api_key = priv.public_key().public_bytes_raw().hex()
wallet = Account.create()   # or Account.from_key("0x...") to reuse an address

valid_until = int(time.time() * 1000) + 7 * 86_400_000   # within [now+1d, now+180d]
msg = canonical({"apiWalletName": "quickstart", "apiWalletPublicKey": api_key, "validUntil": valid_until})
sig = wallet.sign_message(encode_defunct(primitive=msg))
created = call("POST", "/v1/createApiKey", {
    "address": wallet.address, "publicKey": api_key, "apiWalletName": "quickstart",
    "validUntil": valid_until, "signature": {"r": hex(sig.r), "s": hex(sig.s), "v": hex(sig.v)}})
account_index = created["accountIndex"]

# A new key takes a moment to start authenticating — wait for it.
for _ in range(60):
    if any(k["apiKey"] == api_key for k in call("GET", f"/v1/apiKeys?address={wallet.address}")["apiKeys"]):
        break
    time.sleep(1)
print("key is live, accountIndex:", account_index)
```

## Step 3: Place an order

Open the socket, then send signed requests correlated to their responses by `id`. Trading methods sign the same **typed canonical payload** as REST: the compact JSON object **is** the signing message, and its `ct` field must equal the envelope `timestamp` (Unix **nanoseconds**). Price and size are signed as integer ticks/quantums — convert them with the market's `tickSize` / `stepSize`. See [Authentication](/api-reference/authentication) for the field reference.

```python theme={null}
import asyncio, websockets
from decimal import Decimal

OP_PLACE, OP_CANCEL = 1, 2
SIDE = {"BUY": 0, "SELL": 1}
TIF  = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}

mkt  = next(m for m in call("GET", "/v1/markets")["markets"] if m["marketId"] == 1)  # BTC-USD
TICK, STEP = mkt["tickSize"], mkt["stepSize"]
addr = wallet.address.lower()   # the signed payload uses the lowercase address

def to_int(value, unit):        # exact decimal → integer ticks/quantums
    n = Decimal(str(value)) / Decimal(unit)
    if n != n.to_integral_value():
        raise ValueError(f"{value} is not a multiple of {unit}")
    return int(n)

async def main():
    async with websockets.connect("wss://api.testnet.arcus.xyz/v1/ws") as ws:
        async def send(req_id, method, payload, body, ts):
            # The envelope `signature` covers the typed payload, not the body.
            sig = priv.sign(payload.encode()).hex()
            await ws.send(json.dumps({"type": "post", "id": req_id, "request": {
                "type": method, "payload": body,
                "apiKey": api_key, "timestamp": str(ts), "signature": sig}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if msg.get("id") == req_id:
                    return msg

        # A resting GTT buy ~5% below the BTC-USD oracle price, snapped to the tick.
        price    = round(round(float(mkt["oraclePrice"]) * 0.95 / float(TICK)) * float(TICK), 10)
        good_til = int(time.time() * 1_000_000) + 40 * 86_400 * 1_000_000   # epoch µs, ≥1 month ahead

        ts = time.time_ns()
        payload = (f'{{"ad":"{addr}","ai":{account_index},"ct":{ts},"g":{good_til * 1000},'
                   f'"m":1,"op":{OP_PLACE},"p":{to_int(price, TICK)},"q":{to_int("0.001", STEP)},'
                   f'"r":0,"s":{SIDE["BUY"]},"t":{TIF["GTT"]},"v":1}}')
        placed = await send(1, "placeOrder", payload, {
            "address": wallet.address, "accountIndex": account_index, "marketId": 1,
            "orderSide": "BUY", "orderType": "LIMIT", "quantity": "0.001", "price": str(price),
            "timeInForce": "GTT", "goodTilTime": str(good_til), "timestamp": ts}, ts)
        print("placed:", placed["result"]["orderId"], placed["status"])

        ts = time.time_ns()
        payload = (f'{{"ad":"{addr}","ai":{account_index},"ct":{ts},'
                   f'"id":"{placed["result"]["orderId"]}","m":1,"op":{OP_CANCEL},"v":1}}')
        canceled = await send(2, "cancelOrder", payload, {
            "address": wallet.address, "accountIndex": account_index, "marketId": 1,
            "kind": "orderId", "orderId": placed["result"]["orderId"], "timestamp": ts}, ts)
        print("canceled:", canceled["status"])

asyncio.run(main())
```

A successful order returns a `202` acknowledgement; the order's lifecycle (fills, cancels) plays out on the `orders` and `userFills` channels. `placeOrder`, `cancelOrder`, and `modifyOrder` use the typed payload above; `cancelAllOrders` and `setLeverage` use the legacy `timestamp + action + canonicalJSON(payload)` message. A batch signs each order individually with its own typed payload under one shared `timestamp`, each embedding its own `signature` — see the [REST batch example](/guides/rest-trading#step-4-place-a-batch) for the exact headers.

<Note>
  In the request **body**, `quantity` and `price` stay **decimal strings**. The integer `p`/`q` in the signed payload are derived from them via `tickSize` / `stepSize`; the two must agree.
</Note>

## Next: stream your fills

<Note>
  **Coming soon.** The [`userFills`](/api-reference/channels#userfills) and [`orders`](/api-reference/channels#orders) channels stream your fills and order updates in real time (subscribe by account address). A copy-paste example will be added here.
</Note>


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.