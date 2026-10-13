"""Ed25519 API-key handling and the two Arcus signing schemes.

Scheme 1 (typed payload)  — placeOrder / cancelOrder / modifyOrder:
    signature = ed25519(canonical_payload)          # no prefix, `ct` inside
Scheme 2 (legacy message) — cancelAllOrders / setLeverage / ws authenticate:
    signature = ed25519(timestamp + action + canonical_json(body))

Key file format (JSON): {"privateKey": "<hex ed25519 seed>", "apiKey": "<hex pub>",
                         "accountIndex": 0, "walletAddress": "0x..."}
"""

import json
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519


def canonical(obj) -> bytes:
    """Compact, key-sorted JSON — the exact bytes the server re-serializes."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def sign_typed_payload(priv: ed25519.Ed25519PrivateKey, payload: str) -> str:
    """Scheme 1: sign the compact payload string verbatim (it IS the message)."""
    return priv.sign(payload.encode()).hex()


def sign_legacy(priv: ed25519.Ed25519PrivateKey, ts_ns: int, action: str,
                body: dict | None = None) -> str:
    """Scheme 2: timestamp(str, ns) + action + canonical JSON body (or none for GETs)."""
    msg = str(ts_ns) + action
    if body is not None:
        msg += canonical(body).decode()
    return priv.sign(msg.encode()).hex()


def load_or_create_key(key_file: str | Path) -> tuple[ed25519.Ed25519PrivateKey, dict]:
    """Load an Ed25519 key from a JSON file; generate in-memory if missing."""
    path = Path(key_file)
    if path.exists():
        data = json.loads(path.read_text())
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(data["privateKey"]))
        return priv, data
    priv = ed25519.Ed25519PrivateKey.generate()
    meta = {
        "privateKey": priv.private_bytes_raw().hex(),
        "apiKey": priv.public_key().public_bytes_raw().hex(),
    }
    return priv, meta


def register_api_key(client, wallet_account, priv, name: str = "bot",
                     days_valid: int = 90) -> dict:
    """Register a fresh Ed25519 key via POST /v1/createApiKey (EIP-712 gated).

    `wallet_account` is an eth_account.Account for the master Ethereum address.
    Returns the parsed createApiKey response (contains `accountIndex`).
    Waits until the key starts authenticating (new keys take a moment to go live).
    """
    api_key = priv.public_key().public_bytes_raw().hex()
    valid_until = int(time.time() * 1000) + days_valid * 86_400_000
    msg = canonical({"apiWalletName": name, "apiWalletPublicKey": api_key,
                     "validUntil": valid_until})
    from eth_account.messages import encode_defunct
    sig = wallet_account.sign_message(encode_defunct(primitive=msg))
    created = client.post("/v1/createApiKey", {
        "address": wallet_account.address, "publicKey": api_key,
        "apiWalletName": name, "validUntil": valid_until,
        "signature": {"r": hex(sig.r), "s": hex(sig.s), "v": hex(sig.v)}})
    for _ in range(60):
        keys = client.get(f"/v1/apiKeys?address={wallet_account.address}")["apiKeys"]
        if any(k["apiKey"] == api_key for k in keys):
            break
        time.sleep(1)
    return created
