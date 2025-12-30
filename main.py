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
# Source - https://stackoverflow.com/a/78996666
# Posted by Muhammad Saqib Scientist
# Retrieved 2025-12-30, License - CC BY-SA 4.0
def calculate_atr_rma(candles, current_index, period=10):
    """Calculate ATR using RMA (same as Pine Script atr() function)"""
    if current_index < period:
        return None
    
    trs = []
    for i in range(1, current_index + 1):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    
    return atr

def calculate_supertrend(candles, current_index, atr_period=10, multiplier=3.0):
    """
    Calculate Supertrend - Direct translation from Pine Script by kivancOzbilgic
    Returns: (supertrend_value, direction, upper_band, lower_band)
    direction = 1 for uptrend, -1 for downtrend
    """
    if current_index < atr_period:
        return None, None, None, None
    
    up_list = []
    dn_list = []
    trend_list = []
    
    for idx in range(atr_period, current_index + 1):
        high = float(candles[idx][2])
        low = float(candles[idx][3])
        close = float(candles[idx][4])
        src = (high + low) / 2
        
        atr = calculate_atr_rma(candles, idx, atr_period)
        up = src - (multiplier * atr)
        up1 = up_list[-1] if len(up_list) > 0 else up
        prev_close = float(candles[idx-1][4]) if idx > 0 else close
        
        if prev_close > up1:
            up = max(up, up1)
        up_list.append(up)
        
        dn = src + (multiplier * atr)
        dn1 = dn_list[-1] if len(dn_list) > 0 else dn
        
        if prev_close < dn1:
            dn = min(dn, dn1)
        dn_list.append(dn)
        
        if idx == atr_period:
            trend = 1
        else:
            prev_trend = trend_list[-1]
            prev_up = up_list[-2]
            prev_dn = dn_list[-2]
            
            if prev_trend == -1 and close > prev_dn:
                trend = 1
            elif prev_trend == 1 and close < prev_up:
                trend = -1
            else:
                trend = prev_trend
        
        trend_list.append(trend)
    
    last_trend = trend_list[-1]
    last_up = up_list[-1]
    last_dn = dn_list[-1]
    
    if last_trend == 1:
        return last_up, last_trend, last_dn, last_up
    else:
        return last_dn, last_trend, last_dn, last_up

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

def fetch_breakout_candles(symbol, now_utc, start_time):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=100"
        candles = session.get(url, timeout=60).json()
        if not candles or isinstance(candles, dict):
            return []

        results = []
        
        for i in range(len(candles) - 1):
            c = candles[i]
            candle_time = datetime.fromtimestamp(c[0]/1000, tz=timezone.utc)
            
            if candle_time < start_time or candle_time >= now_utc - timedelta(hours=1):
                continue
            
            if i < 14:
                continue
            
            prev_close = float(candles[i - 1][4])
            open_p = float(c[1])
            high = float(c[2])
            low = float(c[3])
            close = float(c[4])
            volume = float(c[5])
            vol_usdt = open_p * volume

            pct = ((close - prev_close) / prev_close) * 100

            ma_start = max(0, i - 19)
            ma_vol = [
                float(candles[j][1]) * float(candles[j][5])
                for j in range(ma_start, i + 1)
            ]
            ma = sum(ma_vol) / len(ma_vol)
            vm = vol_usdt / ma if ma > 0 else 1.0

            all_closes = [float(candles[j][4]) for j in range(0, i + 1)]
            rsi = calculate_rsi_with_full_history(all_closes, RSI_PERIOD)

            # Current Supertrend
            st_value, direction, upper_band, lower_band = calculate_supertrend(candles, i)
            
            if direction is None or i == 0:
                continue
            
            # Previous Supertrend
            prev_st_value, prev_direction, prev_upper_band, prev_lower_band = calculate_supertrend(candles, i-1)
            
            if prev_direction is None:
                continue
            
            # Check if trend JUST CHANGED from downtrend to uptrend
            if prev_direction == -1 and direction == 1:
                hour = candle_time.strftime("%Y-%m-%d %H:00")
                
                # Old red line = last downtrend line (prev_st_value when it was downtrend)
                old_red_line = prev_st_value
                red_distance = ((close - old_red_line) / old_red_line) * 100
                
                # New green line = first uptrend line (current st_value)
                new_green_line = st_value
                green_distance = ((close - new_green_line) / new_green_line) * 100
                
                results.append((symbol, pct, close, vol_usdt, vm, rsi, direction,
                               old_red_line, red_distance, new_green_line, green_distance, hour))
        
        return results
    except Exception as e:
        print(f"{symbol} error:", e)
        return []

def check_breakouts(symbols):
    now_utc = datetime.now(timezone.utc)
    start_time = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    breakouts = []
    
    print(f"Checking from {start_time.strftime('%Y-%m-%d %H:%M')} to {now_utc.strftime('%Y-%m-%d %H:%M')} UTC")

    with ThreadPoolExecutor(max_workers=60) as ex:
        futures = [ex.submit(fetch_breakout_candles, s, now_utc, start_time) for s in symbols]
        for f in as_completed(futures):
            results = f.result()
            if results:
                breakouts.extend(results)
                for r in results:
                    print(f"  Found: {r[0]} at {r[11]} - broke from downtrend to uptrend")

    return breakouts

def format_breakout_report(fresh, duration):
    if not fresh:
        return None
    
    grouped = defaultdict(list)
    for p in fresh:
        grouped[p[11]].append(p)

    report = f"🚀 <b>TREND BREAKOUT ALERTS</b> 🚀\n"
    report += f"⏱ Scan: {duration:.2f}s\n\n"
    
    for h in sorted(grouped):
        items = sorted(grouped[h], key=lambda x: x[8], reverse=True)
        
        report += f"  ⏰ {h} UTC\n"
        
        for symbol, pct, close, vol_usdt, vm, rsi, direction, old_red_line, red_distance, new_green_line, green_distance, hour in items:
            sym = symbol.replace("USDT","")
            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            
            line1 = f"{sym:6s} {pct:5.2f} {rsi_str:>4s} {vm:4.1f} {format_volume(vol_usdt):4s}"
            line2 = f"       🔴Old: ${old_red_line:.5f} (+{red_distance:.2f}%)"
            line3 = f"       🟢New: ${new_green_line:.5f} (+{green_distance:.2f}%)"
            
            report += f"✅ <code>{line1}</code>\n"
            report += f"   <code>{line2}</code>\n"
            report += f"   <code>{line3}</code>\n\n"
        
    report += "💡 🔴Old = Last downtrend line (broke above it!)\n"
    report += "💡 🟢New = New uptrend line (support now)\n"
    
    return report

# ==== Main ====
def main():
    symbols = get_usdt_pairs()
    if not symbols:
        return

    print("Starting breakout scanner...")
    print(f"Monitoring {len(symbols)} pairs for trend reversals (downtrend → uptrend)")
    print("-" * 80)

    while True:
        start = time.time()
        breakouts = check_breakouts(symbols)
        duration = time.time() - start

        print(f"\nScan completed in {duration:.2f}s")
        print(f"Total breakouts found: {len(breakouts)}")

        fresh = []
        for b in breakouts:
            key = (b[0], b[11])
            if key not in reported:
                reported.add(key)
                fresh.append(b)
            else:
                print(f"  Skipping {b[0]} - already reported for {b[11]}")

        print(f"New breakouts (not yet reported): {len(fresh)}")

        if fresh:
            msg = format_breakout_report(fresh, duration)
            if msg:
                print("\n" + "="*80)
                print("SENDING TO TELEGRAM:")
                print("="*80)
                print(msg)
                print("="*80)
                send_telegram(msg[:4096])
        else:
            print(f"No new breakouts found.")

        server = get_binance_server_time()
        next_hour = (server // 3600 + 1) * 3600
        sleep_time = max(0, next_hour - server + 1)
        print(f"\nSleeping for {sleep_time:.0f}s until next hour...")
        print("-" * 80)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
