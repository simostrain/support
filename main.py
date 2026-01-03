import os
import requests
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== Settings ====
BINANCE_API = "https://api.binance.com"

# Telegram Bot for RETEST alerts
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RSI_PERIOD = 14
reported_retests = set()

# Retest settings
RETEST_PROXIMITY = 2.0  # السعر يجب يكون ضمن 2% من الخط الأخضر
MIN_CANDLES_AFTER_BREAKOUT = 5  # الاتجاه الصاعد يجب يكون موجود من 5 شمعات على الأقل

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
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=2)
session.mount("https://", adapter)

# ==== Telegram ====
def send_telegram(msg, max_retries=3):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=10)
            
            if response.status_code == 200:
                print(f"✓ RETEST alert sent")
                return True
            else:
                print(f"✗ Telegram error (status {response.status_code}): {response.text}")
        except Exception as e:
            print(f"✗ Telegram exception (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
    
    print("✗ Failed to send Telegram message after retries.")
    return False

# ==== Utils ====
def format_volume(v):
    """Format volume in K or M"""
    if v >= 1_000_000:
        return f"{v/1_000_000:.0f}M"
    else:
        return f"{v/1_000:.0f}K"

def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=5).json()["serverTime"] / 1000
    except:
        return time.time()

# ==== RSI Calculation ====
def calculate_rsi(closes, period=14):
    """Fast RSI calculation"""
    if len(closes) < period + 1:
        return None
    
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)

# ==== Supertrend Calculation ====
def calculate_atr(candles, period=10):
    """Calculate ATR using RMA"""
    if len(candles) < period + 1:
        return None
    
    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    
    return atr

def calculate_supertrend(candles, atr_period=10, multiplier=3.0):
    """Calculate supertrend, return last state and previous state"""
    if len(candles) < atr_period + 1:
        return None
    
    up_list = []
    dn_list = []
    trend_list = []
    
    for idx in range(atr_period, len(candles)):
        high = float(candles[idx][2])
        low = float(candles[idx][3])
        close = float(candles[idx][4])
        src = (high + low) / 2
        
        atr = calculate_atr(candles[:idx+1], atr_period)
        
        up = src - (multiplier * atr)
        up1 = up_list[-1] if len(up_list) > 0 else up
        prev_close = float(candles[idx-1][4])
        
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
    
    return {
        'up_list': up_list,
        'dn_list': dn_list,
        'trend_list': trend_list
    }

# ==== Binance ====
def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=10).json()
        valid = {s["symbol"] for s in data["symbols"]
                 if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        pairs = [c for c in candidates if c in valid]
        print(f"✓ Loaded {len(pairs)} valid USDT pairs")
        return pairs
    except Exception as e:
        print(f"✗ Exchange info error: {e}")
        return []

# ==== STAGE 1: RETEST DETECTION (50 candles) ====
def detect_retest(symbol):
    """
    Stage 1: Detect retest of support in EXISTING uptrend.
    Logic:
    1. Current trend must be green (1) - في اتجاه صاعد
    2. Must have been in uptrend for at least MIN_CANDLES_AFTER_BREAKOUT candles - الاتجاه مو جديد
    3. Current price must be within RETEST_PROXIMITY% of green support line - قريب من الدعم
    4. Price must have been SIGNIFICANTLY higher before (away from support) - كان بعيد ورجع
    5. Price is coming DOWN to retest (not just sitting on support) - راجع يختبر مو واقف
    Returns: basic_data if retest detected, None otherwise
    """
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=50"
        candles = session.get(url, timeout=5).json()
        
        if not candles or isinstance(candles, dict) or len(candles) < 30:
            return None
        
        # Get last closed candle
        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        
        candle_time = datetime.fromtimestamp(last_candle[0]/1000, tz=timezone.utc)
        time_str = candle_time.strftime("%Y-%m-%d %H:%M")
        
        close = float(last_candle[4])
        low = float(last_candle[3])
        
        # Calculate supertrend for all candles
        st_result = calculate_supertrend(candles[:last_idx+1])
        if not st_result:
            return None
        
        trend_list = st_result['trend_list']
        up_list = st_result['up_list']
        
        # 1. Current must be in uptrend (green)
        if trend_list[-1] != 1:
            return None
        
        # 2. Count how many consecutive green candles (uptrend duration)
        uptrend_candles = 0
        for i in range(len(trend_list) - 1, -1, -1):
            if trend_list[i] == 1:
                uptrend_candles += 1
            else:
                break
        
        # Must be in uptrend for at least MIN_CANDLES_AFTER_BREAKOUT
        if uptrend_candles < MIN_CANDLES_AFTER_BREAKOUT:
            return None
        
        # 3. Get current support line (green line)
        current_support = up_list[-1]
        
        # 4. Check if price is retesting support (within RETEST_PROXIMITY%)
        distance_from_support = ((close - current_support) / current_support) * 100
        
        # Price should be close to support (within RETEST_PROXIMITY%)
        # But NOT below it (price < support means broke down)
        if distance_from_support < 0 or distance_from_support > RETEST_PROXIMITY:
            return None
        
        # 5. Check if price was SIGNIFICANTLY higher before (to confirm pullback)
        # Look at last 3-8 candles to find the highest point
        highest_distance = 0
        highest_idx = -1
        check_range = min(8, len(candles) - 12)  # Look back 3-8 candles
        
        for i in range(last_idx - check_range, last_idx):
            if i < 11:  # Skip early candles (before supertrend stabilizes)
                continue
            check_close = float(candles[i][4])
            check_support = up_list[i - 11]  # Adjust for atr_period offset
            check_distance = ((check_close - check_support) / check_support) * 100
            
            if check_distance > highest_distance:
                highest_distance = check_distance
                highest_idx = i
        
        # Must have been at least 3% away from support (meaningful pullback)
        if highest_distance < 3.0:
            return None
        
        # 6. Confirm price is coming DOWN (not going up away from support)
        # Compare current distance with previous 2 candles
        recent_distances = []
        for i in range(max(0, last_idx - 2), last_idx + 1):
            if i < 11:
                continue
            c = float(candles[i][4])
            s = up_list[i - 11]
            d = ((c - s) / s) * 100
            recent_distances.append(d)
        
        # Price should be getting closer to support (distances decreasing)
        if len(recent_distances) >= 2 and recent_distances[-1] > recent_distances[0]:
            return None  # Price moving away from support, not retesting
        
        # Calculate % change from previous candle
        prev_close = float(candles[last_idx - 1][4])
        pct = ((close - prev_close) / prev_close) * 100
        
        # Calculate when the uptrend started
        uptrend_start_idx = len(trend_list) - uptrend_candles
        uptrend_start_candle_idx = uptrend_start_idx + 10  # Adjust for atr_period
        uptrend_start_time = datetime.fromtimestamp(candles[uptrend_start_candle_idx][0]/1000, tz=timezone.utc)
        
        return {
            'symbol': symbol,
            'time_str': time_str,
            'pct': pct,
            'close': close,
            'support_line': current_support,
            'distance_from_support': distance_from_support,
            'uptrend_candles': uptrend_candles,
            'highest_distance': highest_distance,
            'uptrend_start_time': uptrend_start_time.strftime("%Y-%m-%d %H:%M")
        }
        
    except Exception as e:
        return None

# ==== STAGE 2: CALCULATE RSI & VM (25 candles) ====
def calculate_rsi_and_vm(symbol):
    """
    Stage 2: Fetch 25 candles to calculate RSI and Volume Multiplier.
    """
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=25"
        candles = session.get(url, timeout=5).json()
        
        if not candles or isinstance(candles, dict) or len(candles) < 20:
            return None, None, None
        
        # Get last closed candle
        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        
        open_p = float(last_candle[1])
        volume = float(last_candle[5])
        vol_usdt = open_p * volume
        
        # RSI (need at least RSI_PERIOD + 1 candles)
        all_closes = [float(candles[j][4]) for j in range(0, last_idx + 1)]
        rsi = calculate_rsi(all_closes, RSI_PERIOD)
        
        # Volume Multiplier (20-candle MA)
        ma_start = max(0, last_idx - 19)
        ma_vol = [float(candles[j][1]) * float(candles[j][5]) for j in range(ma_start, last_idx + 1)]
        ma = sum(ma_vol) / len(ma_vol)
        vm = vol_usdt / ma if ma > 0 else 1.0
        
        return rsi, vm, vol_usdt
        
    except:
        return None, None, None

# ==== MAIN SCANNING LOGIC ====
def scan_all_symbols(symbols):
    """
    Two-stage scanning:
    Stage 1: Detect retests with 50 candles
    Stage 2: Calculate RSI and VM with 25 candles for detected retests
    """
    retest_candidates = []
    
    print(f"🔍 Stage 1: Detecting retests with 50 candles...")
    stage1_start = time.time()
    
    # Stage 1: Detect retests
    with ThreadPoolExecutor(max_workers=150) as ex:
        futures = {ex.submit(detect_retest, s): s for s in symbols}
        
        for f in as_completed(futures):
            data = f.result()
            if data:
                retest_candidates.append(data)
    
    stage1_duration = time.time() - stage1_start
    print(f"✓ Stage 1 completed in {stage1_duration:.2f}s")
    print(f"  Found: {len(retest_candidates)} retests")
    
    # Stage 2: Calculate RSI and VM for detected retests
    retests_final = []
    
    if retest_candidates:
        print(f"\n🔬 Stage 2: Calculating RSI & VM for {len(retest_candidates)} coins (25 candles)...")
        stage2_start = time.time()
        
        with ThreadPoolExecutor(max_workers=50) as ex:
            futures = {ex.submit(calculate_rsi_and_vm, d['symbol']): d for d in retest_candidates}
            
            for f in as_completed(futures):
                rsi, vm, vol_usdt = f.result()
                data = futures[f]
                
                if rsi is not None and vm is not None:
                    retests_final.append((
                        data['symbol'],
                        data['pct'],
                        data['close'],
                        vol_usdt,
                        vm,
                        rsi,
                        data['support_line'],
                        data['distance_from_support'],
                        data['uptrend_candles'],
                        data['highest_distance'],
                        data['uptrend_start_time'],
                        data['time_str']
                    ))
        
        stage2_duration = time.time() - stage2_start
        print(f"✓ Stage 2 completed in {stage2_duration:.2f}s")
    
    return retests_final

# ==== REPORTING ====
def format_retest_report(retests, duration):
    if not retests:
        return None
    
    grouped = defaultdict(list)
    for r in retests:
        grouped[r[11]].append(r)
    
    report = f"🎯 <b>SUPPORT RETEST ALERTS (1H)</b> 🎯\n"
    report += f"⏱ Scan: {duration:.2f}s | Found: {len(retests)}\n\n"
    
    for h in sorted(grouped, reverse=True):
        items = sorted(grouped[h], key=lambda x: x[7])  # Sort by distance (closest first)
        report += f"⏰ {h} UTC\n"
        
        for symbol, pct, close, vol_usdt, vm, rsi, support_line, distance, uptrend_candles, highest_distance, uptrend_start_time, time_str in items:
            sym = symbol.replace("USDT","")
            rsi_str = f"{rsi:.1f}" if rsi else "N/A"
            vol_str = format_volume(vol_usdt)
            
            line1 = f"{sym:8s}{pct:5.2f}% {rsi_str} {vm:.1f}x {vol_str}"
            line2 = f"          🟢Support: ${support_line:.5f} (+{distance:.2f}%)"
            line3 = f"          📈Uptrend: {uptrend_candles}h | Peak: +{highest_distance:.1f}%"
            
            report += f"<code>{line1}</code>\n"
            report += f"<code>{line2}</code>\n"
            report += f"<code>{line3}</code>\n"
        report += "\n"
    
    report += "💡 Coin in uptrend & retesting support!\n"
    report += "💡 Closer to support = stronger signal\n"
    report += "💡 Higher peak = stronger pullback confirmation\n"
    
    return report

# ==== Main ====
def main():
    print("="*80)
    print("🎯 RETEST SCANNER - TWO-STAGE ANALYSIS (1-HOUR TIMEFRAME)")
    print("="*80)
    print(f"⚡ Stage 1: Detect retests in EXISTING uptrends (50 candles)")
    print(f"🔬 Stage 2: Calculate RSI & VM (25 candles)")
    print(f"📏 Retest: within {RETEST_PROXIMITY}% of support")
    print(f"📈 Min uptrend: {MIN_CANDLES_AFTER_BREAKOUT} hours")
    print("="*80)
    
    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols loaded. Exiting.")
        return
    
    print(f"✓ Monitoring {len(symbols)} pairs\n")
    
    while True:
        now = datetime.now(timezone.utc)
        print(f"\n{'='*80}")
        print(f"🕐 Scan started: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"{'='*80}\n")
        
        # === TWO-STAGE SCAN ===
        total_start = time.time()
        retests = scan_all_symbols(symbols)
        total_duration = time.time() - total_start
        
        print(f"\n✓ Complete scan finished in {total_duration:.2f}s")
        
        # === FILTER NEW ALERTS ===
        fresh_retests = [r for r in retests if (r[0], r[11]) not in reported_retests]
        
        for r in fresh_retests:
            reported_retests.add((r[0], r[11]))
        
        print(f"  New alerts: {len(fresh_retests)} retests")
        
        # === SEND ALERTS ===
        if fresh_retests:
            msg = format_retest_report(fresh_retests, total_duration)
            if msg:
                print("\n📤 Sending RETEST alert...")
                print("\n" + "="*80)
                print(msg.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
                print("="*80)
                send_telegram(msg[:4096])
        
        # === WAIT FOR NEXT HOUR ===
        server_time = get_binance_server_time()
        next_hour = (server_time // 3600 + 1) * 3600
        sleep_time = max(60, next_hour - server_time + 5)
        
        print(f"\n😴 Sleeping {sleep_time:.0f}s until next hour...\n")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
