import os
import requests
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== Settings ====
BINANCE_API = "https://api.binance.com"

# Telegram Bot 1 - For PUMP alerts
TELEGRAM_BOT_TOKEN_1 = os.getenv("TELEGRAM_BOT_TOKEN_1")
TELEGRAM_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1")

# Telegram Bot 2 - For BREAKOUT alerts
TELEGRAM_BOT_TOKEN_2 = os.getenv("TELEGRAM_BOT_TOKEN_2")
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")

PUMP_THRESHOLD = 3  # percent
RSI_PERIOD = 14
reported_pumps = set()
reported_breakouts = set()

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
def send_telegram(msg, bot_token, chat_id, alert_type, max_retries=3):
    """Send message to specific Telegram bot with retry logic"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=10)
            
            if response.status_code == 200:
                print(f"✓ {alert_type} alert sent")
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
        return None, None
    
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

# ==== STAGE 1: QUICK DETECTION (50 candles) ====
def quick_scan(symbol):
    """
    Stage 1: Quick scan with 50 candles to detect pump or breakout.
    Returns: ('pump', basic_data) or ('breakout', basic_data) or (None, None)
    """
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=50"
        candles = session.get(url, timeout=5).json()
        
        if not candles or isinstance(candles, dict) or len(candles) < 20:
            return None, None
        
        # Get last closed candle
        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        prev_candle = candles[last_idx - 1]
        
        candle_time = datetime.fromtimestamp(last_candle[0]/1000, tz=timezone.utc)
        hour = candle_time.strftime("%Y-%m-%d %H:00")
        
        prev_close = float(prev_candle[4])
        open_p = float(last_candle[1])
        close = float(last_candle[4])
        volume = float(last_candle[5])
        vol_usdt = open_p * volume
        pct = ((close - prev_close) / prev_close) * 100
        
        # Calculate basic metrics (we have enough data with 50 candles)
        # Volume multiplier (20-candle MA)
        ma_start = max(0, last_idx - 19)
        ma_vol = [float(candles[j][1]) * float(candles[j][5]) for j in range(ma_start, last_idx + 1)]
        ma = sum(ma_vol) / len(ma_vol)
        vm = vol_usdt / ma if ma > 0 else 1.0
        
        # RSI (we have 50 candles, enough for RSI-14)
        all_closes = [float(candles[j][4]) for j in range(0, last_idx + 1)]
        rsi = calculate_rsi(all_closes, RSI_PERIOD)
        
        # Check for PUMP
        is_pump = pct >= PUMP_THRESHOLD
        
        # Check for BREAKOUT (need supertrend)
        is_breakout = False
        old_red_line = None
        red_distance = None
        new_green_line = None
        green_distance = None
        
        st_result = calculate_supertrend(candles[:last_idx+1])
        if st_result:
            last_trend, prev_trend, last_up, last_dn, prev_dn = st_result
            if prev_trend == -1 and last_trend == 1:  # Trend reversal
                is_breakout = True
                old_red_line = prev_dn
                red_distance = ((close - old_red_line) / old_red_line) * 100
                new_green_line = last_up
                green_distance = ((close - new_green_line) / new_green_line) * 100
        
        # Return results
        basic_data = {
            'symbol': symbol,
            'hour': hour,
            'pct': pct,
            'close': close,
            'vol_usdt': vol_usdt,
            'vm': vm,
            'rsi': rsi,
            'candles_50': candles  # Keep candles for potential later use
        }
        
        if is_pump and is_breakout:
            # Both detected, return pump (will be caught by both handlers)
            return 'both', {**basic_data, 
                           'old_red_line': old_red_line,
                           'red_distance': red_distance,
                           'new_green_line': new_green_line,
                           'green_distance': green_distance}
        elif is_pump:
            return 'pump', basic_data
        elif is_breakout:
            return 'breakout', {**basic_data,
                               'old_red_line': old_red_line,
                               'red_distance': red_distance,
                               'new_green_line': new_green_line,
                               'green_distance': green_distance}
        
        return None, None
        
    except:
        return None, None

# ==== STAGE 2: DEEP ANALYSIS (250 candles for csince only) ====
def calculate_csince_pump(symbol, current_pct):
    """
    Stage 2: Fetch 250 candles ONLY to calculate candles since last pump.
    """
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=250"
        candles = session.get(url, timeout=5).json()
        
        if not candles or isinstance(candles, dict):
            return 250
        
        # Start from second-to-last (last closed candle)
        last_idx = len(candles) - 2
        
        # Look backwards for previous pump
        for i in range(last_idx - 1, max(0, last_idx - 509), -1):
            if i == 0:
                break
            prev_pct = ((float(candles[i][4]) - float(candles[i-1][4])) / float(candles[i-1][4])) * 100
            if prev_pct >= PUMP_THRESHOLD:
                return last_idx - i
        
        return 250
        
    except:
        return 250

def calculate_csince_breakout(symbol):
    """
    Stage 2: Fetch 250 candles ONLY to calculate candles since last breakout.
    """
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=250"
        candles = session.get(url, timeout=5).json()
        
        if not candles or isinstance(candles, dict):
            return 250
        
        last_idx = len(candles) - 2
        
        # Look backwards for previous breakout
        for look_back in range(1, min(250, last_idx)):
            check_idx = last_idx - look_back
            if check_idx < 15:
                break
            
            st_result = calculate_supertrend(candles[:check_idx+1])
            if st_result:
                last_trend, prev_trend, _, _, _ = st_result
                if prev_trend == -1 and last_trend == 1:
                    return look_back
        
        return 250
        
    except:
        return 250

# ==== MAIN SCANNING LOGIC ====
def scan_all_symbols(symbols):
    """
    Two-stage scanning:
    Stage 1: Quick scan all symbols with 50 candles
    Stage 2: Deep analysis only for detected pumps/breakouts (250 candles for csince)
    """
    pump_candidates = []
    breakout_candidates = []
    
    print(f"🔍 Stage 1: Quick scanning {len(symbols)} symbols with 50 candles...")
    stage1_start = time.time()
    
    # Stage 1: Quick scan
    with ThreadPoolExecutor(max_workers=150) as ex:
        futures = {ex.submit(quick_scan, s): s for s in symbols}
        
        for f in as_completed(futures):
            alert_type, data = f.result()
            
            if alert_type == 'pump':
                pump_candidates.append(data)
            elif alert_type == 'breakout':
                breakout_candidates.append(data)
            elif alert_type == 'both':
                pump_candidates.append(data)
                breakout_candidates.append(data)
    
    stage1_duration = time.time() - stage1_start
    print(f"✓ Stage 1 completed in {stage1_duration:.2f}s")
    print(f"  Found: {len(pump_candidates)} pumps, {len(breakout_candidates)} breakouts")
    
    # Stage 2: Deep analysis for csince
    pumps_final = []
    breakouts_final = []
    
    if pump_candidates or breakout_candidates:
        print(f"\n🔬 Stage 2: Deep analysis for {len(pump_candidates) + len(breakout_candidates)} coins...")
        stage2_start = time.time()
        
        # Calculate csince for pumps
        if pump_candidates:
            with ThreadPoolExecutor(max_workers=50) as ex:
                futures = {ex.submit(calculate_csince_pump, d['symbol'], d['pct']): d for d in pump_candidates}
                
                for f in as_completed(futures):
                    csince = f.result()
                    data = futures[f]
                    
                    pumps_final.append((
                        data['symbol'], data['pct'], data['close'], data['vol_usdt'],
                        data['vm'], data['rsi'], csince, data['hour']
                    ))
        
        # Calculate csince for breakouts
        if breakout_candidates:
            with ThreadPoolExecutor(max_workers=50) as ex:
                futures = {ex.submit(calculate_csince_breakout, d['symbol']): d for d in breakout_candidates}
                
                for f in as_completed(futures):
                    csince = f.result()
                    data = futures[f]
                    
                    breakouts_final.append((
                        data['symbol'], data['pct'], data['close'], data['vol_usdt'],
                        data['vm'], data['rsi'], 1,  # direction (always 1 for breakout up)
                        data['old_red_line'], data['red_distance'],
                        data['new_green_line'], data['green_distance'],
                        csince, data['hour']
                    ))
        
        stage2_duration = time.time() - stage2_start
        print(f"✓ Stage 2 completed in {stage2_duration:.2f}s")
    
    return pumps_final, breakouts_final

# ==== REPORTING ====
def format_pump_report(pumps, duration):
    if not pumps:
        return None
    
    grouped = defaultdict(list)
    for p in pumps:
        grouped[p[7]].append(p)
    
    report = f"💰 <b>PUMP ALERTS</b> 💰\n"
    report += f"⏱ Scan: {duration:.2f}s | Found: {len(pumps)}\n\n"
    
    for h in sorted(grouped, reverse=True):
        items = sorted(grouped[h], key=lambda x: x[3], reverse=True)
        report += f"⏰ {h} UTC\n"
        
        for symbol, pct, close, vol_usdt, vm, rsi, csince, hour in items:
            sym = symbol.replace("USDT","")
            rsi_str = f"{rsi:.1f}" if rsi else "N/A"
            csince_str = f"{csince:03d}"
            
            line = f"{sym:6s} {pct:5.2f} {rsi_str:>4s} {vm:4.1f} {format_volume(vol_usdt):4s} {csince_str}"
            
            if rsi:
                if rsi >= 66 and csince >= 20:
                    icon = "✅"
                elif rsi >= 66:
                    icon = "🔴"
                elif rsi >= 50:
                    icon = "🟢"
                else:
                    icon = "🟡"
            else:
                icon = "⚪"
            
            report += f"{icon} <code>{line}</code>\n"
        report += "\n"
    
    return report

def format_breakout_report(breakouts, duration):
    if not breakouts:
        return None
    
    grouped = defaultdict(list)
    for b in breakouts:
        grouped[b[12]].append(b)
    
    report = f"🚀 <b>TREND BREAKOUT ALERTS</b> 🚀\n"
    report += f"⏱ Scan: {duration:.2f}s | Found: {len(breakouts)}\n\n"
    
    for h in sorted(grouped, reverse=True):
        items = sorted(grouped[h], key=lambda x: x[8], reverse=True)
        report += f"⏰ {h} UTC\n"
        
        for symbol, pct, close, vol_usdt, vm, rsi, direction, old_red_line, red_distance, new_green_line, green_distance, csince, hour in items:
            sym = symbol.replace("USDT","")
            rsi_str = f"{rsi:.1f}" if rsi else "N/A"
            csince_str = f"{csince:03d}"
            
            line1 = f"{sym:6s} {pct:5.2f} {rsi_str:>4s} {vm:4.1f} {format_volume(vol_usdt):4s} {csince_str}"
            line2 = f"       🔴Old: ${old_red_line:.5f} (+{red_distance:.2f}%)"
            line3 = f"       🟢New: ${new_green_line:.5f} (+{green_distance:.2f}%)"
            
            report += f"<code>{line1}</code>\n"
            report += f"   <code>{line2}</code>\n"
            report += f"   <code>{line3}</code>\n"
        report += "\n"
    
    report += "💡 🔴Old = Last downtrend (broke above!)\n"
    report += "💡 🟢New = New uptrend (support)\n"
    
    return report

# ==== Main ====
def main():
    print("="*80)
    print("🚀 ULTRA-FAST CRYPTO SCANNER - TWO-STAGE ANALYSIS")
    print("="*80)
    print(f"⚡ Stage 1: Quick scan with 50 candles (ALL symbols)")
    print(f"🔬 Stage 2: Deep analysis with 250 candles (DETECTED coins only)")
    print(f"📊 PUMP → Bot 1 | 📈 BREAKOUT → Bot 2")
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
        pumps, breakouts = scan_all_symbols(symbols)
        total_duration = time.time() - total_start
        
        print(f"\n✓ Complete scan finished in {total_duration:.2f}s")
        
        # === FILTER NEW ALERTS ===
        fresh_pumps = [p for p in pumps if (p[0], p[7]) not in reported_pumps]
        fresh_breakouts = [b for b in breakouts if (b[0], b[12]) not in reported_breakouts]
        
        for p in fresh_pumps:
            reported_pumps.add((p[0], p[7]))
        for b in fresh_breakouts:
            reported_breakouts.add((b[0], b[12]))
        
        print(f"  New alerts: {len(fresh_pumps)} pumps, {len(fresh_breakouts)} breakouts")
        
        # === SEND ALERTS ===
        if fresh_pumps:
            msg = format_pump_report(fresh_pumps, total_duration)
            if msg:
                print("\n📤 Sending PUMP alert...")
                send_telegram(msg[:4096], TELEGRAM_BOT_TOKEN_1, TELEGRAM_CHAT_ID_1, "PUMP")
        
        if fresh_breakouts:
            msg = format_breakout_report(fresh_breakouts, total_duration)
            if msg:
                print("\n📤 Sending BREAKOUT alert...")
                send_telegram(msg[:4096], TELEGRAM_BOT_TOKEN_2, TELEGRAM_CHAT_ID_2, "BREAKOUT")
        
        # === WAIT FOR NEXT HOUR ===
        server_time = get_binance_server_time()
        next_hour = (server_time // 3600 + 1) * 3600
        sleep_time = max(60, next_hour - server_time + 5)
        
        print(f"\n😴 Sleeping {sleep_time:.0f}s until next hour...\n")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
