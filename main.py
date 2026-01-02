import os
import requests
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== Settings ====
BINANCE_API = "https://api.binance.com"

# Telegram Bot for BREAKOUT alerts
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_2")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID_2")

RSI_PERIOD = 14
reported_breakouts = set()

# Strength filters (set to 0 to disable filtering)
MIN_STRENGTH_SCORE = 0  # Minimum strength score (0-100). Recommended: 55+ for good signals, 65+ for strong only
MIN_CSINCE = 0          # Minimum candles since last breakout (0 = no filter, 25 = at least 1 day, 100 = 4+ days)
MIN_VOLUME_MULT = 0.0   # Minimum volume multiplier (0 = no filter, 1.5 = 50% above average, 2.0 = 2x average)

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
    """Send message to Telegram bot with retry logic"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=10)
            
            if response.status_code == 200:
                print(f"✓ BREAKOUT alert sent")
                return True
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
    
    return False

# ==== Utils ====
def format_volume(v):
    return f"{v/1_000_000:.2f}"

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
    
    last_trend = trend_list[-1]
    prev_trend = trend_list[-2] if len(trend_list) > 1 else last_trend
    last_up = up_list[-1]
    last_dn = dn_list[-1]
    prev_dn = dn_list[-2] if len(dn_list) > 1 else last_dn
    
    return (last_trend, prev_trend, last_up, last_dn, prev_dn)

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

# ==== STRENGTH SCORING ====
def calculate_strength_score(csince, vm, rsi, red_distance, pct):
    """
    Calculate breakout strength score (0-100) based on key factors:
    - csince: Time since last breakout (longer = stronger)
    - vm: Volume multiplier (higher = stronger)
    - rsi: Momentum (50-70 ideal, >70 overbought warning)
    - red_distance: Distance from old resistance (higher = cleaner break)
    - pct: Price change percentage (positive momentum)
    """
    score = 0
    
    # 1. Candles Since Last Breakout (0-30 points)
    # Longer consolidation = stronger breakout potential
    if csince >= 200:
        score += 30  # 8+ days
    elif csince >= 100:
        score += 25  # 4+ days
    elif csince >= 50:
        score += 20  # 2+ days
    elif csince >= 25:
        score += 15  # 1+ day
    elif csince >= 10:
        score += 10
    else:
        score += 5   # Too recent
    
    # 2. Volume Multiplier (0-25 points)
    # High volume confirms the breakout
    if vm >= 3.0:
        score += 25
    elif vm >= 2.0:
        score += 20
    elif vm >= 1.5:
        score += 15
    elif vm >= 1.0:
        score += 10
    else:
        score += 5
    
    # 3. RSI Analysis (0-25 points)
    # Sweet spot: 50-70 (momentum without overbought)
    if 55 <= rsi <= 65:
        score += 25  # Perfect zone
    elif 50 <= rsi <= 70:
        score += 20  # Good zone
    elif 45 <= rsi < 50:
        score += 15  # Acceptable
    elif 70 < rsi <= 75:
        score += 12  # Slight overbought warning
    elif rsi > 75:
        score += 5   # Overbought risk
    else:
        score += 10  # Below 45 (weak momentum)
    
    # 4. Distance from Old Resistance (0-15 points)
    # Clean break above resistance
    if red_distance >= 3.0:
        score += 15
    elif red_distance >= 2.0:
        score += 12
    elif red_distance >= 1.0:
        score += 10
    elif red_distance >= 0.5:
        score += 7
    else:
        score += 3   # Too close to resistance
    
    # 5. Price Change Momentum (0-5 points)
    if pct >= 3.0:
        score += 5
    elif pct >= 2.0:
        score += 4
    elif pct >= 1.0:
        score += 3
    elif pct >= 0:
        score += 2
    else:
        score += 0   # Negative momentum
    
    return min(100, score)

def get_strength_emoji(score):
    """Return emoji based on strength score"""
    if score >= 85:
        return "🔥"  # Exceptional
    elif score >= 75:
        return "⭐"  # Excellent
    elif score >= 65:
        return "✅"  # Strong
    elif score >= 55:
        return "🟢"  # Good
    elif score >= 45:
        return "🟡"  # Moderate
    else:
        return "⚪"  # Weak

# ==== STAGE 1: BREAKOUT DETECTION (500 candles) ====
def detect_breakout(symbol):
    """
    Stage 1: Detect breakout with 500 candles.
    Returns: basic_data if breakout detected, None otherwise
    """
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=500"
        candles = session.get(url, timeout=5).json()
        
        if not candles or isinstance(candles, dict) or len(candles) < 20:
            return None
        
        # Get last closed candle
        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        prev_candle = candles[last_idx - 1]
        
        candle_time = datetime.fromtimestamp(last_candle[0]/1000, tz=timezone.utc)
        hour = candle_time.strftime("%Y-%m-%d %H:00")
        
        prev_close = float(prev_candle[4])
        open_p = float(last_candle[1])
        close = float(last_candle[4])
        pct = ((close - prev_close) / prev_close) * 100
        
        # Calculate supertrend for breakout detection
        st_result = calculate_supertrend(candles[:last_idx+1])
        if not st_result:
            return None
        
        last_trend, prev_trend, last_up, last_dn, prev_dn = st_result
        
        # Check for breakout (trend reversal from red to green)
        if prev_trend == -1 and last_trend == 1:
            old_red_line = prev_dn
            red_distance = ((close - old_red_line) / old_red_line) * 100
            new_green_line = last_up
            green_distance = ((close - new_green_line) / new_green_line) * 100
            
            # Look backwards for previous breakout (csince calculation)
            csince = 500  # default
            for look_back in range(1, min(499, last_idx)):
                check_idx = last_idx - look_back
                if check_idx < 15:
                    break
                
                st_check = calculate_supertrend(candles[:check_idx+1])
                if st_check:
                    check_last_trend, check_prev_trend, _, _, _ = st_check
                    if check_prev_trend == -1 and check_last_trend == 1:
                        csince = look_back
                        break
            
            return {
                'symbol': symbol,
                'hour': hour,
                'pct': pct,
                'close': close,
                'old_red_line': old_red_line,
                'red_distance': red_distance,
                'new_green_line': new_green_line,
                'green_distance': green_distance,
                'csince': csince
            }
        
        return None
        
    except:
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
            return None, None
        
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
    Stage 1: Detect breakouts with 500 candles
    Stage 2: Calculate RSI and VM with 25 candles for detected breakouts
    """
    breakout_candidates = []
    
    print(f"🔍 Stage 1: Detecting breakouts with 500 candles...")
    stage1_start = time.time()
    
    # Stage 1: Detect breakouts
    with ThreadPoolExecutor(max_workers=150) as ex:
        futures = {ex.submit(detect_breakout, s): s for s in symbols}
        
        for f in as_completed(futures):
            data = f.result()
            if data:
                breakout_candidates.append(data)
    
    stage1_duration = time.time() - stage1_start
    print(f"✓ Stage 1 completed in {stage1_duration:.2f}s")
    print(f"  Found: {len(breakout_candidates)} breakouts")
    
    # Stage 2: Calculate RSI and VM for detected breakouts
    breakouts_final = []
    
    if breakout_candidates:
        print(f"\n🔬 Stage 2: Calculating RSI & VM for {len(breakout_candidates)} coins (25 candles)...")
        stage2_start = time.time()
        
        with ThreadPoolExecutor(max_workers=50) as ex:
            futures = {ex.submit(calculate_rsi_and_vm, d['symbol']): d for d in breakout_candidates}
            
            for f in as_completed(futures):
                rsi, vm, vol_usdt = f.result()
                data = futures[f]
                
                if rsi is not None and vm is not None:
                    # Calculate strength score (0-100)
                    strength_score = calculate_strength_score(
                        data['csince'], vm, rsi, data['red_distance'], data['pct']
                    )
                    
                    breakouts_final.append((
                        data['symbol'],
                        data['pct'],
                        data['close'],
                        vol_usdt,
                        vm,
                        rsi,
                        data['old_red_line'],
                        data['red_distance'],
                        data['new_green_line'],
                        data['green_distance'],
                        data['csince'],
                        data['hour'],
                        strength_score
                    ))
        
        stage2_duration = time.time() - stage2_start
        print(f"✓ Stage 2 completed in {stage2_duration:.2f}s")
        
        # Apply strength filters
        if MIN_STRENGTH_SCORE > 0 or MIN_CSINCE > 0 or MIN_VOLUME_MULT > 0:
            filtered = []
            for b in breakouts_final:
                # b[12] = strength_score, b[10] = csince, b[4] = vm
                if (b[12] >= MIN_STRENGTH_SCORE and 
                    b[10] >= MIN_CSINCE and 
                    b[4] >= MIN_VOLUME_MULT):
                    filtered.append(b)
            
            if len(filtered) < len(breakouts_final):
                print(f"  Filtered: {len(breakouts_final)} → {len(filtered)} (applied strength filters)")
                breakouts_final = filtered
    
    return breakouts_final

# ==== REPORTING ====
def format_breakout_report(breakouts, duration):
    if not breakouts:
        return None
    
    grouped = defaultdict(list)
    for b in breakouts:
        grouped[b[11]].append(b)
    
    report = f"🚀 <b>TREND BREAKOUT ALERTS</b> 🚀\n"
    report += f"⏱ Scan: {duration:.2f}s | Found: {len(breakouts)}\n\n"
    
    for h in sorted(grouped, reverse=True):
        # Sort by strength score (highest first), then by red_distance
        items = sorted(grouped[h], key=lambda x: (x[12], x[7]), reverse=True)
        report += f"⏰ {h} UTC\n"
        
        for symbol, pct, close, vol_usdt, vm, rsi, old_red_line, red_distance, new_green_line, green_distance, csince, hour, strength_score in items:
            sym = symbol.replace("USDT","")
            rsi_str = f"{rsi:.1f}" if rsi else "N/A"
            csince_str = f"{csince:03d}"
            strength_icon = get_strength_emoji(strength_score)
            
            line1 = f"{sym:6s} {pct:5.2f} {rsi_str:>4s} {vm:4.1f} {format_volume(vol_usdt):4s} {csince_str} [{strength_score:2d}]"
            line2 = f"       🔴Old: ${old_red_line:.5f} (+{red_distance:.2f}%)"
            line3 = f"       🟢New: ${new_green_line:.5f} (+{green_distance:.2f}%)"
            
            report += f"{strength_icon} <code>{line1}</code>\n"
            report += f"   <code>{line2}</code>\n"
            report += f"   <code>{line3}</code>\n"
        report += "\n"
    
    report += "💡 <b>Strength Score Guide:</b>\n"
    report += "🔥 85+ = Exceptional | ⭐ 75+ = Excellent | ✅ 65+ = Strong\n"
    report += "🟢 55+ = Good | 🟡 45+ = Moderate | ⚪ &lt;45 = Weak\n\n"
    report += "💡 🔴Old = Last downtrend (broke above!)\n"
    report += "💡 🟢New = New uptrend (support)\n"
    
    return report

# ==== Main ====
def main():
    print("="*80)
    print("🚀 BREAKOUT SCANNER - TWO-STAGE ANALYSIS")
    print("="*80)
    print(f"⚡ Stage 1: Detect breakouts with 500 candles (ALL symbols)")
    print(f"🔬 Stage 2: Calculate RSI & VM with 25 candles (DETECTED breakouts only)")
    
    # Show active filters
    filters_active = []
    if MIN_STRENGTH_SCORE > 0:
        filters_active.append(f"Min Score: {MIN_STRENGTH_SCORE}")
    if MIN_CSINCE > 0:
        filters_active.append(f"Min Csince: {MIN_CSINCE}")
    if MIN_VOLUME_MULT > 0:
        filters_active.append(f"Min VM: {MIN_VOLUME_MULT}x")
    
    if filters_active:
        print(f"🔍 Filters: {' | '.join(filters_active)}")
    else:
        print(f"🔍 Filters: NONE (showing all breakouts)")
    
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
        breakouts = scan_all_symbols(symbols)
        total_duration = time.time() - total_start
        
        print(f"\n✓ Complete scan finished in {total_duration:.2f}s")
        
        # === FILTER NEW ALERTS ===
        fresh_breakouts = [b for b in breakouts if (b[0], b[11]) not in reported_breakouts]
        
        for b in fresh_breakouts:
            reported_breakouts.add((b[0], b[11]))
        
        print(f"  New alerts: {len(fresh_breakouts)} breakouts")
        
        # === SEND ALERTS ===
        if fresh_breakouts:
            msg = format_breakout_report(fresh_breakouts, total_duration)
            if msg:
                print("\n📤 Sending BREAKOUT alert...")
                send_telegram(msg[:4096])
        
        # === WAIT FOR NEXT HOUR ===
        server_time = get_binance_server_time()
        next_hour = (server_time // 3600 + 1) * 3600
        sleep_time = max(60, next_hour - server_time + 5)
        
        print(f"\n😴 Sleeping {sleep_time:.0f}s until next hour...\n")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
