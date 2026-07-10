import os
print("DEBUG: starting engine_v1 import...")

import time
from typing import List, Dict, Any
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

print("DEBUG: imported libraries, now loading env...")

load_dotenv()

print("DEBUG: after load_dotenv")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
print("DEBUG: SUPABASE_URL:", SUPABASE_URL)
print("DEBUG: SUPABASE_KEY is set:", SUPABASE_KEY is not None)
POLY_TRADES_ENDPOINT = os.environ.get("POLYMARKET_TRADES_ENDPOINT", "https://data-api.polymarket.com/trades")
TRACKED_WALLETS = [w.strip() for w in os.environ.get("TRACKED_WALLETS", "").split(",") if w.strip()]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

WINDOW_MINUTES = 30
MIN_WALLETS_FOR_SIGNAL = 2  # adjust as you like


def fetch_trades_for_wallet(wallet: str, limit: int = 50) -> List[Dict[str, Any]]:
    params = {
        "user": wallet,
        "limit": limit,
        "order": "desc",
    }
    resp = requests.get(POLY_TRADES_ENDPOINT, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def upsert_market_from_trade(trade: Dict[str, Any]) -> str:
    condition_id = trade.get("conditionId")
    question = trade.get("title") or ""

    result = (
        supabase.table("markets")
        .upsert(
            {
                "polymarket_id": condition_id,
                "question": question,
            },
            on_conflict="polymarket_id",
        )
        .execute()
    )

    row = result.data[0]
    return row["id"]


def upsert_wallet(address: str) -> str:
    result = (
        supabase.table("wallets")
        .upsert(
            {
                "address": address,
            },
            on_conflict="address",
        )
        .execute()
    )
    row = result.data[0]
    return row["id"]


def insert_trade(wallet_id: str, market_id: str, trade: Dict[str, Any]) -> None:
    tx_hash = trade.get("transactionHash")
    outcome_raw = trade.get("outcome")  # 'Yes' / 'No' / team names / etc.
    side = outcome_raw.upper() if isinstance(outcome_raw, str) else None

    size = trade.get("size")
    price = trade.get("price")
    ts = trade.get("timestamp")  # seconds since epoch

    if not tx_hash:
        return

    traded_at = None
    if isinstance(ts, (int, float)):
        traded_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    existing = (
        supabase.table("trades")
        .select("id")
        .eq("tx_hash", tx_hash)
        .limit(1)
        .execute()
    )
    if existing.data:
        return

    supabase.table("trades").insert(
        {
            "wallet_id": wallet_id,
            "market_id": market_id,
            "side": side,
            "size": size,
            "price": price,
            "tx_hash": tx_hash,
            "traded_at": traded_at,
        }
    ).execute()


def sync_trades_once():
    if not TRACKED_WALLETS:
        print("No wallets configured in TRACKED_WALLETS")
        return

    for wallet in TRACKED_WALLETS:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Syncing trades for wallet {wallet}")
        trades = fetch_trades_for_wallet(wallet)
        print(f"Got {len(trades)} trades for {wallet}")
        wallet_id = upsert_wallet(wallet)
        for trade in trades:
            try:
                market_id = upsert_market_from_trade(trade)
                insert_trade(wallet_id, market_id, trade)
            except Exception as e:
                print(f"Error processing trade {trade.get('transactionHash')}: {e}")


def create_signals_from_recent_trades():
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=WINDOW_MINUTES)

    result = (
        supabase.table("trades")
        .select("id, wallet_id, market_id, side, traded_at")
        .gte("traded_at", window_start.isoformat())
        .execute()
    )

    trades = result.data or []
    print(f"Found {len(trades)} trades in the last {WINDOW_MINUTES} minutes")

    groups: Dict[tuple, set] = {}
    for t in trades:
        key = (t["market_id"], t["side"])
        wallet_set = groups.setdefault(key, set())
        wallet_set.add(t["wallet_id"])

    for (market_id, side), wallets in groups.items():
        wallet_count = len(wallets)
        if wallet_count < MIN_WALLETS_FOR_SIGNAL:
            continue

        existing = (
            supabase.table("signals")
            .select("id")
            .eq("market_id", market_id)
            .eq("side", side)
            .gte("triggered_at", window_start.isoformat())
            .execute()
        )
        if existing.data:
            continue

        total_score = wallet_count  # later: sum(wallet.score)

        print(f"Creating signal for market {market_id}, side {side}, wallets {wallet_count}")

        supabase.table("signals").insert(
            {
                "market_id": market_id,
                "side": side,
                "wallet_count": wallet_count,
                "total_score": total_score,
                "window_minutes": WINDOW_MINUTES,
                "outcome": "PENDING",
                "roi": None,
            }
        ).execute()


def main():
    print("DEBUG: entering main() with loop")
    while True:
        try:
            print("\n===== Engine tick =====")
            sync_trades_once()
            create_signals_from_recent_trades()
            print("DEBUG: finished one loop iteration")
        except Exception as e:
            print(f"Top-level error: {e}")
        time.sleep(60)


if __name__ == "__main__":
    main()