import os
import requests
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== Settings ====
BINANCE_API = "https://api.binance.com"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RSI_PERIOD = 14
BREAKOUT_MIN = 0.0  # Min distance for alert
BREAKOUT_MAX = 2.0  # Max distance for alert
reported = set()  # avoid duplicate (symbol, hour)

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
        return f"{v/1_000_000:.2f}"
    else:
        return f"{v/1_000_000:.2f}"

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

# ==== Supertrend Calculation ====
def calculate_supertrend(candles, current_index, atr_period=10, factor=3.0):
    if current_index < atr_period:
        return None, None, None, None
    
    # Calculate ATR
    atr_values = []
    for i in range(current_index - atr_period + 1, current_index + 1):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i-1][4]) if i > 0 else float(candles[i][1])
        
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        atr_values.append(tr)
    
    atr = sum(atr_values) / len(atr_values)
    
    # Calculate basic bands
    high = float(candles[current_index][2])
    low = float(candles[current_index][3])
    close = float(candles[current_index][4])
    hl2 = (high + low) / 2
    
    basic_upper = hl2 + (factor * atr)
    basic_lower = hl2 - (factor * atr)
    
    # Initialize or get previous supertrend
    if current_index == atr_period:
        final_upper = basic_upper
        final_lower = basic_lower
    else:
        prev_high = float(candles[current_index-1][2])
        prev_low = float(candles[current_index-1][3])
        prev_close = float(candles[current_index-1][4])
        prev_hl2 = (prev_high + prev_low) / 2
        
        prev_atr_values = []
        for i in range(current_index - atr_period, current_index):
            h = float(candles[i][2])
            l = float(candles[i][3])
            pc = float(candles[i-1][4]) if i > 0 else float(candles[i][1])
            tr = max(h - l, abs(h - pc), abs(l - pc))
            prev_atr_values.append(tr)
        prev_atr = sum(prev_atr_values) / len(prev_atr_values)
        
        prev_basic_upper = prev_hl2 + (factor * prev_atr)
        prev_basic_lower = prev_hl2 - (factor * prev_atr)
        
        final_upper = basic_upper if basic_upper < prev_basic_upper or prev_close > prev_basic_upper else prev_basic_upper
        final_lower = basic_lower if basic_lower > prev_basic_lower or prev_close < prev_basic_lower else prev_basic_lower
    
    # Current direction
    if close <= final_upper:
        direction = 1  # Downtrend
        supertrend = final_upper
    else:
        direction = -1  # Uptrend
        supertrend = final_lower
    
    return supertrend, direction, final_upper, final_lower

# ==== Binance ====
def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=60).json()
        valid = {s["symbol"] for s in data["symbols"]
                 if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        pairs = [c for c in candidates if c in valid]
        print(f"Loaded {len(pairs)} valid USDT pairs.")
        return pairs
    except Exception as e:
        print("Exchange info error:", e)
        return []

def fetch_breakout_candles(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=50"
        candles = session.get(url, timeout=60).json()
        if not candles or isinstance(candles, dict):
            return None

        # Get the latest completed candle (second to last)
        current_index = len(candles) - 2
        if current_index < 14:
            return None

        c = candles[current_index]
        candle_time = datetime.fromtimestamp(c[0]/1000, tz=timezone.utc)
        
        open_p = float(c[1])
        high = float(c[2])
        low = float(c[3])
        close = float(c[4])
        volume = float(c[5])
        vol_usdt = open_p * volume

        # Calculate RSI
        all_closes = [float(candles[j][4]) for j in range(0, current_index + 1)]
        rsi = calculate_rsi_with_full_history(all_closes, RSI_PERIOD)

        # Calculate Supertrend
        supertrend_value, direction, upper_band, lower_band = calculate_supertrend(candles, current_index)
        
        if direction is None or upper_band is None or lower_band is None:
            return None

        # Calculate breakout distance
        if direction == 1:  # Downtrend
            # Distance to lower band (breakout to uptrend)
            breakout_distance = ((close - lower_band) / lower_band) * 100
        else:  # Uptrend - we're not interested in these for breakout alerts
            return None

        # Only return if within breakout range
        if BREAKOUT_MIN <= breakout_distance <= BREAKOUT_MAX:
            hour = candle_time.strftime("%Y-%m-%d %H:00")
            return (symbol, close, vol_usdt, rsi, direction, breakout_distance, hour)
        
        return None
    except Exception as e:
        print(f"{symbol} error:", e)
        return None

def check_breakouts(symbols):
    breakouts = []

    with ThreadPoolExecutor(max_workers=60) as ex:
        for f in as_completed([ex.submit(fetch_breakout_candles, s) for s in symbols]):
            result = f.result()
            if result:
                breakouts.append(result)

    return breakouts

def format_breakout_report(fresh, duration):
    if not fresh:
        return None
    
    # Group by hour
    grouped = defaultdict(list)
    for p in fresh:
        grouped[p[6]].append(p)

    report = f"🚀 <b>BREAKOUT ALERTS</b> 🚀\n"
    report += f"⏱ Scan: {duration:.2f}s\n\n"
    
    for h in sorted(grouped):
        items = sorted(grouped[h], key=lambda x: x[5])  # Sort by breakout distance (closest first)
        
        report += f"  ⏰ {h} UTC\n"
        
        for symbol, close, vol_usdt, rsi, direction, breakout_distance, hour in items:
            sym = symbol.replace("USDT","")
            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            vol_str = format_volume(vol_usdt)
            
            # Format: Symbol Price RSI Volume Distance
            line = f"{sym:6s} ${close:8.4f} RSI:{rsi_str:>4s} Vol:{vol_str:>4s}M 🔴-{breakout_distance:.1f}%"
            
            report += f"🚀 <code>{line}</code>\n"
        
        report += "\n"
    
    report += "💡 These coins are in DOWNTREND but close to breaking into UPTREND!\n"
    
    return report

# ==== Main ====
def main():
    symbols = get_usdt_pairs()
    if not symbols:
        return

    while True:
        start = time.time()
        breakouts = check_breakouts(symbols)
        duration = time.time() - start

        # Filter out already reported
        fresh = []
        for b in breakouts:
            key = (b[0], b[6])  # symbol, hour
            if key not in reported:
                reported.add(key)
                fresh.append(b)

        if fresh:
            msg = format_breakout_report(fresh, duration)
            if msg:
                print(msg)
                send_telegram(msg[:4096])
        else:
            print(f"No breakout opportunities found. Scanned {len(symbols)} pairs in {duration:.2f}s")

        # Wait until next hour
        server = get_binance_server_time()
        next_hour = (server // 3600 + 1) * 3600
        sleep_time = max(0, next_hour - server + 1)
        print(f"Sleeping for {sleep_time:.0f}s until next hour...")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()