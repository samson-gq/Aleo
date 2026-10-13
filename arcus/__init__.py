"""Arcus trading bot toolkit — REST + WebSocket clients per docs.arcus.xyz."""

from .client import ArcusClient, canonical, to_int
from .auth import (
    load_or_create_key,
    register_api_key,
    sign_typed_payload,
    sign_legacy,
)
from .ws import ArcusWS

__all__ = [
    "ArcusClient",
    "ArcusWS",
    "canonical",
    "to_int",
    "load_or_create_key",
    "register_api_key",
    "sign_typed_payload",
    "sign_legacy",
]

MAINNET_REST = "https://api.arcus.xyz"
TESTNET_REST = "https://api.testnet.arcus.xyz"
MAINNET_WS = "wss://api.arcus.xyz/v1/ws"
TESTNET_WS = "wss://api.testnet.arcus.xyz/v1/ws"
