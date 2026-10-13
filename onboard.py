#!/usr/bin/env python3
"""One-time setup: create an Arcus API key and register it to a wallet.

Usage:
  python onboard.py                          # fresh wallet + fresh key (testnet)
  ARCUS_WALLET_PK=0x... python onboard.py    # reuse your own wallet address
  ARCUS_BASE=https://api.arcus.xyz python onboard.py   # mainnet

Writes keys/arcus_key.json (chmod 600). Fund the account afterwards:
  testnet web app → connect wallet → "Testnet Deposit" (~$1,000 paper USDC),
  or an on-chain deposit (docs: /guides/fund-testnet-account).
A brand-new account returns 404 on GET /v1/account until its first deposit.
"""

import json
import os
import sys
from pathlib import Path

from eth_account import Account

sys.path.insert(0, str(Path(__file__).parent))
from arcus.auth import load_or_create_key, register_api_key
from arcus.client import ArcusClient

BASE = os.environ.get("ARCUS_BASE", "https://api.testnet.arcus.xyz")
KEY_FILE = Path(__file__).parent / "keys" / "arcus_key.json"


def main():
    client = ArcusClient(BASE)

    if KEY_FILE.exists():
        print(f"[!] {KEY_FILE} already exists — delete it to regenerate.")
        return 1

    priv, meta = load_or_create_key(KEY_FILE)

    pk = os.environ.get("ARCUS_WALLET_PK")
    wallet = Account.from_key(pk) if pk else Account.create()
    if not pk:
        print("Generated a throwaway wallet. To use YOUR wallet instead, set")
        print("ARCUS_WALLET_PK=<private key> and re-run BEFORE this step is done.")

    print(f"Registering API key for {wallet.address} at {BASE} ...")
    created = register_api_key(client, wallet, priv, name="bot")

    record = {
        **meta,
        "accountIndex": created.get("accountIndex", 0),
        "walletAddress": wallet.address,
        "walletPrivateKey": wallet.key.hex() if not pk else pk,
        "base": BASE,
    }
    KEY_FILE.parent.mkdir(exist_ok=True)
    KEY_FILE.write_text(json.dumps(record, indent=2))
    os.chmod(KEY_FILE, 0o600)

    print(f"\nDone. Key saved to {KEY_FILE}")
    print(f"  apiKey       : {record['apiKey']}")
    print(f"  accountIndex : {record['accountIndex']}")
    print(f"  wallet       : {record['walletAddress']}")
    print("\nNext: fund the account (Testnet Deposit button in the web app),")
    print("then run:  python bot/main.py --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
