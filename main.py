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
SUPPORT_MIN = 0.0  # Minimum distance from support (0%)
SUPPORT_MAX = 3.0  # Maximum distance from support (3%)
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

def fetch_support_touch(symbol):
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
        
        # Get previous candle for pump percentage calculation
        prev_close = float(candles[current_index - 1][4])
        
        open_p = float(c[1])
        high = float(c[2])
        low = float(c[3])
        close = float(c[4])
        volume = float(c[5])
        vol_usdt = open_p * volume

        # Calculate pump percentage (current close vs previous close)
        pct = ((close - prev_close) / prev_close) * 100

        # Calculate volume multiplier (20-period MA)
        ma_start = max(0, current_index - 19)
        ma_vol = [
            float(candles[j][1]) * float(candles[j][5])
            for j in range(ma_start, current_index + 1)
        ]
        ma = sum(ma_vol) / len(ma_vol)
        vm = vol_usdt / ma if ma > 0 else 1.0

        # Calculate RSI
        all_closes = [float(candles[j][4]) for j in range(0, current_index + 1)]
        rsi = calculate_rsi_with_full_history(all_closes, RSI_PERIOD)

        # Calculate Supertrend for CURRENT candle
        supertrend_value, direction, upper_band, lower_band = calculate_supertrend(candles, current_index)
        
        if direction is None or upper_band is None or lower_band is None:
            return None

        # Only interested in UPTREND coins
        if direction == -1:  # Uptrend
            # Calculate distance from close to support line (green line / lower_band)
            support_line = supertrend_value  # This is the lower_band when in uptrend
            distance_from_support = ((close - support_line) / support_line) * 100
            
            # Check if within range (close to support)
            if SUPPORT_MIN <= distance_from_support <= SUPPORT_MAX:
                hour = candle_time.strftime("%Y-%m-%d %H:00")
                
                # Calculate distance to resistance (upper band - where downtrend would start)
                distance_to_resistance = ((upper_band - close) / close) * 100
                
                return (symbol, pct, close, vol_usdt, vm, rsi, direction, 
                       support_line, distance_from_support, upper_band, distance_to_resistance, hour)
        
        return None
    except Exception as e:
        print(f"{symbol} error:", e)
        return None

def check_support_touches(symbols):
    touches = []

    with ThreadPoolExecutor(max_workers=60) as ex:
        for f in as_completed([ex.submit(fetch_support_touch, s) for s in symbols]):
            result = f.result()
            if result:
                touches.append(result)

    return touches

def format_support_report(fresh, duration):
    if not fresh:
        return None
    
    # Group by hour
    grouped = defaultdict(list)
    for p in fresh:
        grouped[p[11]].append(p)

    report = f"📍 <b>SUPPORT TOUCH ALERTS</b> 📍\n"
    report += f"⏱ Scan: {duration:.2f}s\n\n"
    
    for h in sorted(grouped):
        # Sort by distance from support (closest first = best entry)
        items = sorted(grouped[h], key=lambda x: x[8])
        
        report += f"  ⏰ {h} UTC\n"
        
        for symbol, pct, close, vol_usdt, vm, rsi, direction, support_line, distance_from_support, resistance_line, distance_to_resistance, hour in items:
            sym = symbol.replace("USDT","")
            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            
            # Line 1: Basic info
            line1 = f"{sym:6s} {pct:5.2f} {rsi_str:>4s} {vm:4.1f} {format_volume(vol_usdt):4s}"
            
            # Line 2: Support line and distance (how close to support)
            line2 = f"       🟢Sup: ${support_line:.5f} (+{distance_from_support:.2f}%)"
            
            # Line 3: Resistance line and potential gain
            line3 = f"       🔴Res: ${resistance_line:.5f} (🎯+{distance_to_resistance:.2f}%)"
            
            # Choose emoji based on how close to support
            if distance_from_support <= 1.0:
                emoji = "🎯"  # Very close - best entry!
            elif distance_from_support <= 2.0:
                emoji = "✅"  # Good entry
            else:
                emoji = "🟢"  # Okay entry
            
            report += f"{emoji} <code>{line1}</code>\n"
            report += f"   <code>{line2}</code>\n"
            report += f"   <code>{line3}</code>\n\n"
        
    report += "💡 🟢Sup = Support line (buy zone)\n"
    report += "💡 🔴Res = Resistance line (profit target)\n"
    report += "💡 Closer to support = Better entry!\n"
    
    return report

# ==== Main ====
def main():
    symbols = get_usdt_pairs()
    if not symbols:
        return

    while True:
        start = time.time()
        touches = check_support_touches(symbols)
        duration = time.time() - start

        # Filter out already reported
        fresh = []
        for t in touches:
            key = (t[0], t[11])  # symbol, hour
            if key not in reported:
                reported.add(key)
                fresh.append(t)

        if fresh:
            msg = format_support_report(fresh, duration)
            if msg:
                print(msg)
                send_telegram(msg[:4096])
        else:
            print(f"No support touch opportunities found. Scanned {len(symbols)} pairs in {duration:.2f}s")

        # Wait until next hour
        server = get_binance_server_time()
        next_hour = (server // 3600 + 1) * 3600
        sleep_time = max(0, next_hour - server + 1)
        print(f"Sleeping for {sleep_time:.0f}s until next hour...")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
