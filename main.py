import os
import requests
import time
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== Settings ====
BINANCE_API = "https://api.binance.com"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RSI_PERIOD = 14
SUPPORT_MIN = 0.0  # Minimum distance from support (0%)
SUPPORT_MAX = 3.0  # Maximum distance from support (3%)
reported = set()  # avoid duplicate (symbol, hour)

CUSTOM_TICKERS = [
    "ADA","LINK","DOT","BTC","ETH","BNB","XRP","LTC","SOL","MANA","APE","ARB","ATOM","FIL"
]

# ==== Session ====
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session.mount("https://", adapter)

# ==== Telegram ====
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=60)
    except Exception as e:
        print("Telegram error:", e)

# ==== Utils ====
def format_volume(v):
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}"
    elif v >= 1_000:
        return f"{v/1_000:.2f}"
    else:
        return f"{v:.2f}"

def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=60).json()["serverTime"] / 1000
    except:
        return time.time()

# ==== RSI Calculation ====
def calculate_rsi_with_full_history(closes, period=14):
    if len(closes) < period + 1:
        return None
    
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    
    return round(rsi, 2)

# ==== Supertrend with pandas_ta ====
def calculate_supertrend_pandas(candles_df, length=10, multiplier=3.0):
    # Add Supertrend using pandas_ta
    st = candles_df.ta.supertrend(length=length, multiplier=multiplier)
    candles_df = candles_df.join(st)
    # Columns: 'SUPERT_10_3.0' (value), 'SUPERTd_10_3.0' (trend: 1=downtrend, -1=uptrend)
    return candles_df

# ==== Binance ==== 
def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=60).json()
        valid = {s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        pairs = [c for c in candidates if c in valid]
        print(f"Loaded {len(pairs)} valid USDT pairs.")
        return pairs
    except Exception as e:
        print("Exchange info error:", e)
        return []

def fetch_support_touch(symbol, now_utc, start_time):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=20"
        candles = session.get(url, timeout=60).json()
        if not candles or isinstance(candles, dict):
            return []

        # Convert to DataFrame for pandas_ta
        df = pd.DataFrame(candles, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_base_vol","taker_quote_vol","ignore"
        ])
        df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)

        # Add Supertrend
        df = calculate_supertrend_pandas(df, length=10, multiplier=3.0)

        results = []
        for i in range(RSI_PERIOD, len(df)-1):
            candle_time = datetime.fromtimestamp(df.loc[i, "open_time"]/1000, tz=timezone.utc)
            if candle_time < start_time or candle_time >= now_utc - timedelta(hours=1):
                continue

            close = df.loc[i, "close"]
            prev_close = df.loc[i-1, "close"]
            pct = ((close - prev_close)/prev_close)*100
            vol_usdt = df.loc[i, "open"] * df.loc[i, "volume"]

            ma_start = max(0, i-19)
            ma_vol = df.loc[ma_start:i, "open"]*df.loc[ma_start:i, "volume"]
            ma = ma_vol.mean()
            vm = vol_usdt / ma if ma>0 else 1.0

            # RSI
            all_closes = df.loc[:i, "close"].tolist()
            rsi = calculate_rsi_with_full_history(all_closes, RSI_PERIOD)

            st_value = df.loc[i, 'SUPERT_10_3.0']
            trend = df.loc[i, 'SUPERTd_10_3.0']
            upper_band = df.loc[i, 'SUPERT_10_3.0'] if trend==1 else df.loc[i, 'SUPERT_10_3.0']
            lower_band = df.loc[i, 'SUPERT_10_3.0'] if trend==-1 else df.loc[i, 'SUPERT_10_3.0']

            if trend == -1:  # Uptrend
                support_line = st_value
                distance_from_support = ((close-support_line)/support_line)*100
                if SUPPORT_MIN <= distance_from_support <= SUPPORT_MAX:
                    hour = candle_time.strftime("%Y-%m-%d %H:00")
                    distance_to_resistance = ((upper_band-close)/close)*100
                    results.append((symbol,pct,close,vol_usdt,vm,rsi,support_line,distance_from_support,upper_band,distance_to_resistance,hour))
        return results
    except Exception as e:
        print(f"{symbol} error:", e)
        return []

def check_support_touches(symbols):
    now_utc = datetime.now(timezone.utc)
    start_time = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    touches = []
    with ThreadPoolExecutor(max_workers=60) as ex:
        futures = [ex.submit(fetch_support_touch,s,now_utc,start_time) for s in symbols]
        for f in as_completed(futures):
            res = f.result()
            if res:
                touches.extend(res)
    return touches

# ==== Formatting report ====
def format_support_report(fresh,duration):
    if not fresh: return None
    grouped = defaultdict(list)
    for r in fresh: grouped[r[-1]].append(r)
    report = f"📍 SUPPORT TOUCH ALERTS 📍\n⏱ Scan: {duration:.2f}s\n\n"
    for hour in sorted(grouped):
        report += f"  ⏰ {hour} UTC\n"
        for s,pct,close,vol_usdt,vm,rsi,sl,ds,rl,dr,h in sorted(grouped[hour],key=lambda x:x[7]):
            sym = s.replace("USDT","")
            rsi_str = f"{rsi:.1f}" if rsi else "N/A"
            emoji = "🎯" if ds<=1.0 else "✅" if ds<=2.0 else "🟢"
            line1 = f"{sym:<6s} {pct:5.2f} {rsi_str:>4s} {vm:4.1f} {format_volume(vol_usdt):4s}"
            line2 = f"   🟢Sup: ${sl:.4f} (+{ds:.2f}%)"
            line3 = f"   🔴Res: ${rl:.4f} (🎯+{dr:.2f}%)"
            report += f"{emoji} {line1}\n{line2}\n{line3}\n\n"
    report += "💡 🟢Sup = Support line (buy zone)\n💡 🔴Res = Resistance line (profit target)\n💡 Closer to support = Better entry!\n"
    return report

# ==== Main loop ====
def main():
    symbols = get_usdt_pairs()
    if not symbols: return

    while True:
        start = time.time()
        touches = check_support_touches(symbols)
        duration = time.time() - start

        fresh = []
        for t in touches:
            key = (t[0],t[-1])
            if key not in reported:
                reported.add(key)
                fresh.append(t)

        if fresh:
            msg = format_support_report(fresh,duration)
            if msg:
                print(msg)
                send_telegram(msg[:4096])
        else:
            print(f"No support touch opportunities found. Scanned {len(symbols)} pairs in {duration:.2f}s")

        server = get_binance_server_time()
        next_hour = (server//3600+1)*3600
        sleep_time = max(0,next_hour-server+1)
        print(f"Sleeping for {sleep_time:.0f}s until next hour...")
        time.sleep(sleep_time)

if __name__=="__main__":
    main()
