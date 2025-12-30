import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone

# ================= SETTINGS =================
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT"]

BINANCE_API = "https://api.binance.com"
INTERVAL = "1h"
LIMIT = 500          # IMPORTANT: enough history for ATR stability

ATR_LENGTH = 10
MULTIPLIER = 3.0
# ============================================

session = requests.Session()


def fetch_klines(symbol):
    url = f"{BINANCE_API}/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "limit": LIMIT
    }
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def klines_to_df(klines):
    df = pd.DataFrame(klines, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "num_trades",
        "taker_base_vol", "taker_quote_vol", "ignore"
    ])

    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)

    df["open"] = df["open"].astype("float64")
    df["high"] = df["high"].astype("float64")
    df["low"] = df["low"].astype("float64")
    df["close"] = df["close"].astype("float64")

    return df


def run_supertrend():
    print("=" * 80)
    print("SUPERTREND CHECK — BINANCE / TRADINGVIEW MATCH")
    print("=" * 80)

    for symbol in SYMBOLS:
        try:
            df = klines_to_df(fetch_klines(symbol))

            # === CALCULATE SUPERTREND ===
            st = ta.supertrend(
                high=df["high"],
                low=df["low"],
                close=df["close"],
                length=ATR_LENGTH,
                multiplier=MULTIPLIER
            )

            df = pd.concat([df, st], axis=1)
