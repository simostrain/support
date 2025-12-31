import os
import requests
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# SETTINGS
# ============================================================
BINANCE_API = "https://api.binance.com"
INTERVAL = "1h"
CANDLE_LIMIT = 250
PUMP_THRESHOLD = 3.0
RSI_PERIOD = 14

# Telegram (optional – script works without them)
PUMP_BOT_TOKEN = os.getenv("TELEGRAM_PUMP_BOT_TOKEN")
PUMP_CHAT_ID = os.getenv("TELEGRAM_PUMP_CHAT_ID")

BREAKOUT_BOT_TOKEN = os.getenv("TELEGRAM_BREAKOUT_BOT_TOKEN")
BREAKOUT_CHAT_ID = os.getenv("TELEGRAM_BREAKOUT_CHAT_ID")

CUSTOM_TICKERS = [
    "At","A2Z","ACE","ACH","ACT","ADA","ADX","AGLD","AIXBT","Algo","ALICE","ALPINE","ALT","AMP","ANKR","APE",
    "API3","APT","AR","ARB","ARDR","Ark","ARKM","ARPA","ASTR","Ata","ATOM","AVA","AVAX","AWE","AXL","BANANA",
    "BAND","BAT","BCH","BEAMX","BICO","BIO","Blur","BMT","Btc","CELO","Celr","CFX","CGPT","CHR","CHZ","CKB",
    "COOKIE","Cos","CTSI","CVC","Cyber","Dash","DATA","DCR","Dent","DeXe","DGB","DIA","DOGE","DOT","DUSK",
    "EDU","EGLD","ENJ","ENS","EPIC","ERA","ETC","ETH","FET","FIDA","FIL","fio","Flow","Flux","Gala","Gas",
    "GLM","GLMR","GMT","GPS","GRT","GTC","HBAR","HEI","HIGH","Hive","HOOK","HOT","HYPER","ICP","ICX","ID",
    "IMX","INIT","IO","IOST","IOTA","IOTX","IQ","JASMY","Kaia","KAITO","KSM","la","layer","LINK","LPT","LRC",
    "LSK","LTC","LUNA","MAGIC","MANA","Manta","Mask","MDT","ME","Metis","Mina","MOVR","MTL","NEAR","NEWT",
    "NFP","NIL","NKN","NTRN","OM","ONE","ONG","OP","ORDI","OXT","PARTI","PAXG","PHA","PHB","PIVX","Plume",
    "POL","POLYX","POND","Portal","POWR","Prom","PROVE","PUNDIX","Pyth","QKC","QNT","Qtum","RAD","RARE",
    "REI","Render","REQ","RIF","RLC","Ronin","ROSE","Rsr","RVN","Saga","SAHARA","SAND","SC","SCR","SCRT",
    "SEI","SFP","SHELL","Sign","SKL","Sol","SOPH","Ssv","Steem","Storj","STRAX","STX","Sui","SXP","SXT",
    "SYS","TAO","TFUEL","Theta","TIA","TNSR","TON","TOWNS","TRB","TRX","TWT","Uma","UTK","Vana","VANRY",
    "VET","VIC","VIRTUAL","VTHO","WAXP","WCT","win","WLD","Xai","XEC","XLM","XNO","XRP","XTZ","XVG","Zec",
    "ZEN","ZIL","ZK","ZRO","0G","2Z","C","D","ENSO","G","HOLO","KITE","LINEA","MIRA","OPEN","S","SAPIEN",
    "SOMI","W","WAL","XPL","ZBT","ZKC"
]

# ============================================================
# SESSION
# ============================================================
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=40, pool_maxsize=40)
session.mount("https://", adapter)

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(token, chat_id, msg):
    if not token or not chat_id:
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
        timeout=20
    )

# ============================================================
# BINANCE TIME
# ============================================================
def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=10).json()["serverTime"] / 1000
    except:
        return time.time()

# ============================================================
# RSI
# ============================================================
def calculate_rsi_with_full_history(closes, period=14):
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ============================================================
# SUPERTREND (SIMPLIFIED, STABLE)
# ============================================================
def calculate_supertrend(candles, idx, period=10, mult=3):
    if idx < period:
        return None

    trs = []
    for i in range(1, idx + 1):
        h = float(candles[i][2])
        l = float(candles[i][3])
        pc = float(candles[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    atr = sum(trs[-period:]) / period
    hl2 = (float(candles[idx][2]) + float(candles[idx][3])) / 2
    close = float(candles[idx][4])

    upper = hl2 + mult * atr
    return 1 if close > upper else -1

# ============================================================
# BINANCE DATA
# ============================================================
def get_usdt_pairs():
    info = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=30).json()
    valid = {s["symbol"] for s in info["symbols"] if s["quoteAsset"] == "USDT"}
    return [t + "USDT" for t in CUSTOM_TICKERS if t + "USDT" in valid]

def fetch_candles(symbol):
    url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
    data = session.get(url, timeout=20).json()

    if not data or isinstance(data, dict):
        print(f"❌ Candle error {symbol}: {data}")
        return None

    return data

# ============================================================
# DETECTION
# ============================================================
def detect_pumps(symbol, candles):
    found = []
    for i in range(1, len(candles)):
        prev_close = float(candles[i-1][4])
        close = float(candles[i][4])
        pct = (close - prev_close) / prev_close * 100

        if pct >= PUMP_THRESHOLD:
            closes = [float(c[4]) for c in candles[:i+1]]
            rsi = calculate_rsi_with_full_history(closes)
            hour = datetime.fromtimestamp(
                candles[i][0]/1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:00")

            found.append((symbol, pct, rsi, hour))
    return found

def detect_breakouts(symbol, candles):
    found = []
    for i in range(15, len(candles)):
        prev_dir = calculate_supertrend(candles, i-1)
        cur_dir = calculate_supertrend(candles, i)
        if prev_dir == -1 and cur_dir == 1:
            hour = datetime.fromtimestamp(
                candles[i][0]/1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:00")
            found.append((symbol, hour))
    return found

# ============================================================
# PROCESS SYMBOL (DEBUG!)
# ============================================================
def process_symbol(symbol):
    print(f"🔍 Scanning {symbol}")
    candles = fetch_candles(symbol)

    if not candles:
        return [], []

    print(f"📊 {symbol} candles: {len(candles)}")

    pumps = detect_pumps(symbol, candles)
    breakouts = detect_breakouts(symbol, candles)

    if pumps:
        print(f"🔥 {symbol} pumps: {len(pumps)}")
    if breakouts:
        print(f"🚀 {symbol} breakouts: {len(breakouts)}")

    return pumps, breakouts

# ============================================================
# MAIN
# ============================================================
def main():
    symbols = get_usdt_pairs()
    print(f"\nLoaded {len(symbols)} symbols\n")

    while True:
        print("\n================ NEW SCAN ================\n")

        all_pumps = []
        all_breakouts = []

        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(process_symbol, s) for s in symbols]
            for f in as_completed(futures):
                p, b = f.result()
                all_pumps.extend(p)
                all_breakouts.extend(b)

        if all_pumps:
            msg = "🔥 <b>PUMPS</b>\n\n"
            for s, pct, rsi, h in all_pumps:
                msg += f"<code>{s} {pct:.2f}% RSI:{rsi} @ {h}</code>\n"
            send_telegram(PUMP_BOT_TOKEN, PUMP_CHAT_ID, msg)

        if all_breakouts:
            msg = "🚀 <b>BREAKOUTS</b>\n\n"
            for s, h in all_breakouts:
                msg += f"<code>{s} @ {h}</code>\n"
            send_telegram(BREAKOUT_BOT_TOKEN, BREAKOUT_CHAT_ID, msg)

        server = get_binance_server_time()
        next_hour = (server // 3600 + 1) * 3600
        sleep_time = max(0, next_hour - server + 1)

        print(f"\n⏳ Scan done. Sleeping {int(sleep_time)} seconds...\n")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
