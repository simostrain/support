import os
import requests
import time
import math
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from pathlib import Path

# ==== Settings ====
BINANCE_API = "https://api.binance.com"

# Telegram Bot for alerts
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RSI_PERIOD = 14
reported_signals = set()  # Track both breakouts and retests

# Strength filters (set to 0 to disable filtering)
MIN_STRENGTH_SCORE = 0  # Minimum indicator strength score (0-10). Recommended: 5.5+ for good signals, 6.5+ for strong only
MIN_CSINCE = 0          # Minimum candles since last breakout (0 = no filter, 25 = at least 1 day, 100 = 4+ days)
MIN_VOLUME_MULT = 0.0   # Minimum volume multiplier (0 = no filter, 1.5 = 50% above average, 2.0 = 2x average)

# Retest settings (from indicator)
VOL_MULT_RETEST = 1.5      # Volume multiplier for retest confirmation
VOL_LEN = 20               # Volume SMA length
ATR_MOVE_MULT = 0.5        # Minimum move (ATR multiplier)
VOL_LOOKBACK = 10          # Volume confirmation window (bars)
RETEST_TIMING_MAX = 15     # Max bars after breakout for valid retest

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

# Log file path (use /tmp for Railway or adjust for your environment)
LOG_FILE = Path("/tmp/signal_log.json")

# ==== Session ====
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=2)
session.mount("https://", adapter)

# ==== Logging ====
def log_signal_to_file(signal_data, signal_type):
    """Log signals to a JSON file as backup"""
    log_entry = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'type': signal_type,
        'data': signal_data
    }
    
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
        print(f"  📝 Logged {signal_type} to file")
    except Exception as e:
        print(f"  ⚠️ Failed to log to file: {e}")

# ==== Telegram ====
def send_telegram(msg, max_retries=3):
    """Send message to Telegram bot with retry logic"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials not set!")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=10)
            
            if response.status_code == 200:
                print(f"  ✅ Alert sent to Telegram (attempt {attempt + 1})")
                return True
            else:
                print(f"  ⚠️ Telegram API returned status {response.status_code} (attempt {attempt + 1})")
        except Exception as e:
            print(f"  ⚠️ Telegram error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
    
    print(f"  ❌ Failed to send to Telegram after {max_retries} attempts")
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
    
    return {
        'trend_list': trend_list,
        'up_list': up_list,
        'dn_list': dn_list,
        'last_trend': last_trend,
        'prev_trend': prev_trend,
        'last_up': last_up,
        'last_dn': last_dn,
        'prev_dn': prev_dn
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

# ==== STRENGTH SCORING (from indicator) ====
def calculate_strength_score_indicator(volume, vol_sma, close, supertrend, atr):
    """
    Calculate strength score based on TradingView indicator logic:
    strengthScore = log(volRatio + 1) * momentum
    where momentum = |close - supertrend| / atr
    """
    if vol_sma <= 0 or atr <= 0:
        return 0.0
    
    vol_ratio = volume / vol_sma
    momentum = abs(close - supertrend) / atr
    
    # Exact formula from indicator
    strength_score = math.log(vol_ratio + 1) * momentum
    
    # Cap at 10 (matching indicator)
    strength_score = min(strength_score, 10.0)
    
    return strength_score  # Return full precision, not rounded

def get_strength_emoji(score):
    """Return emoji based on indicator strength score (0-10)"""
    if score >= 8.5:
        return "🔥"
    elif score >= 7.5:
        return "⭐"
    elif score >= 6.5:
        return "✅"
    elif score >= 5.5:
        return "🟢"
    elif score >= 4.5:
        return "🟡"
    else:
        return "⚪"

# ==== SIGNAL DETECTION ====
def detect_signals(symbol):
    """
    Detect both breakouts and retests based on TradingView indicator logic
    Returns: dict with 'breakout' and/or 'retest' data
    """
    try:
        # Fetch enough candles for analysis
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=100"
        candles = session.get(url, timeout=5).json()
        
        if not candles or isinstance(candles, dict) or len(candles) < 30:
            return None
        
        # Get last closed candle
        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        prev_candle = candles[last_idx - 1]
        
        candle_time = datetime.fromtimestamp(last_candle[0]/1000, tz=timezone.utc)
        hour = candle_time.strftime("%Y-%m-%d %H:00")
        
        prev_close = float(prev_candle[4])
        open_p = float(last_candle[1])
        high = float(last_candle[2])
        low = float(last_candle[3])
        close = float(last_candle[4])
        volume = float(last_candle[5])  # Base volume (e.g., BTC, not USDT)
        vol_usdt = open_p * volume      # USDT volume (for display only)
        
        pct = ((close - prev_close) / prev_close) * 100
        
        # Calculate supertrend
        st_result = calculate_supertrend(candles[:last_idx+1])
        if not st_result:
            return None
        
        last_trend = st_result['last_trend']
        prev_trend = st_result['prev_trend']
        last_up = st_result['last_up']
        last_dn = st_result['last_dn']
        prev_dn = st_result['prev_dn']
        trend_list = st_result['trend_list']
        
        # Calculate ATR for current candle
        atr = calculate_atr(candles[:last_idx+1], 10)
        
        # Calculate volume SMA (using BASE volume, not USDT)
        vol_ma_start = max(0, last_idx - VOL_LEN + 1)
        vol_ma_data = [float(candles[j][5]) for j in range(vol_ma_start, last_idx + 1)]
        vol_sma = sum(vol_ma_data) / len(vol_ma_data) if vol_ma_data else volume
        
        # Calculate indicator strength score (using BASE volume)
        current_supertrend = last_up if last_trend == 1 else last_dn
        indicator_strength = calculate_strength_score_indicator(volume, vol_sma, close, current_supertrend, atr)
        
        results = {}
        
        # ==== BREAKOUT DETECTION ====
        # Bullish flip: prev_trend was -1 (red/down), current is 1 (green/up)
        bullish_flip = (prev_trend == -1 and last_trend == 1)
        
        # Move confirmation: |close - prev_close| >= atr * ATR_MOVE_MULT
        move_confirmation = abs(close - prev_close) >= (atr * ATR_MOVE_MULT)
        
        if bullish_flip and move_confirmation:
            old_red_line = prev_dn
            red_distance = ((close - old_red_line) / old_red_line) * 100
            new_green_line = last_up
            green_distance = ((close - new_green_line) / new_green_line) * 100
            
            # Look backwards for previous breakout (csince calculation)
            csince = 500
            for look_back in range(1, min(499, last_idx)):
                check_idx = last_idx - look_back
                if check_idx < 15:
                    break
                
                st_check = calculate_supertrend(candles[:check_idx+1])
                if st_check:
                    check_last_trend = st_check['last_trend']
                    check_prev_trend = st_check['prev_trend']
                    if check_prev_trend == -1 and check_last_trend == 1:
                        csince = look_back
                        break
            
            # Volume confirmation (using base volume)
            vol_confirmed = volume > (vol_sma * VOL_MULT_RETEST)
            
            if vol_confirmed:
                results['breakout'] = {
                    'symbol': symbol,
                    'hour': hour,
                    'pct': pct,
                    'close': close,
                    'old_red_line': old_red_line,
                    'red_distance': red_distance,
                    'new_green_line': new_green_line,
                    'green_distance': green_distance,
                    'csince': csince,
                    'vol_usdt': vol_usdt,
                    'vm': volume / vol_sma if vol_sma > 0 else 1.0,  # Fixed: use base volume for VM
                    'indicator_strength': indicator_strength
                }
        
        # ==== RETEST DETECTION ====
        in_uptrend = last_trend == 1
        
        if in_uptrend:
            # Find when uptrend started (trend start bar)
            trend_start_idx = None
            for i in range(len(trend_list) - 1, 0, -1):
                if trend_list[i] == 1 and trend_list[i-1] == -1:
                    trend_start_idx = i
                    break
            
            if trend_start_idx is not None:
                bars_in_trend = len(trend_list) - trend_start_idx - 1
                valid_timing = bars_in_trend <= RETEST_TIMING_MAX
                
                # Retest conditions
                support_tested = low <= last_up  # low touched or went below supertrend
                support_held = close > last_up   # but close stayed above
                bullish_candle = close > open_p  # green candle
                
                if support_tested and support_held and bullish_candle and valid_timing:
                    # Volume confirmation (using base volume)
                    vol_confirmed = volume > (vol_sma * VOL_MULT_RETEST)
                    
                    if vol_confirmed:
                        results['retest'] = {
                            'symbol': symbol,
                            'hour': hour,
                            'pct': pct,
                            'close': close,
                            'supertrend': last_up,
                            'bars_in_trend': bars_in_trend,
                            'vol_usdt': vol_usdt,
                            'vm': volume / vol_sma if vol_sma > 0 else 1.0,  # Fixed: use base volume for VM
                            'indicator_strength': indicator_strength,
                            'support_distance': ((close - last_up) / last_up) * 100
                        }
        
        return results if results else None
        
    except Exception as e:
        return None

# ==== CALCULATE RSI FOR SIGNALS ====
def calculate_rsi_for_signal(symbol):
    """Calculate RSI for a detected signal"""
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=25"
        candles = session.get(url, timeout=5).json()
        
        if not candles or isinstance(candles, dict) or len(candles) < 20:
            return None
        
        last_idx = len(candles) - 2
        all_closes = [float(candles[j][4]) for j in range(0, last_idx + 1)]
        rsi = calculate_rsi(all_closes, RSI_PERIOD)
        
        return rsi
        
    except:
        return None

# ==== MAIN SCANNING LOGIC ====
def scan_all_symbols(symbols):
    """
    Scan for both breakouts and retests
    """
    signal_candidates = []
    
    print(f"🔍 Scanning for breakouts and retests...")
    scan_start = time.time()
    
    # Scan for signals
    with ThreadPoolExecutor(max_workers=150) as ex:
        futures = {ex.submit(detect_signals, s): s for s in symbols}
        
        for f in as_completed(futures):
            result = f.result()
            if result:
                signal_candidates.append(result)
    
    scan_duration = time.time() - scan_start
    
    breakout_count = sum(1 for r in signal_candidates if 'breakout' in r)
    retest_count = sum(1 for r in signal_candidates if 'retest' in r)
    
    print(f"✓ Scan completed in {scan_duration:.2f}s")
    print(f"  Found: {breakout_count} breakouts, {retest_count} retests")
    
    # Calculate RSI for detected signals
    final_signals = {'breakouts': [], 'retests': []}
    
    if signal_candidates:
        print(f"\n🔬 Calculating RSI for detected signals...")
        rsi_start = time.time()
        
        with ThreadPoolExecutor(max_workers=50) as ex:
            futures = {ex.submit(calculate_rsi_for_signal, list(result.values())[0]['symbol']): result 
                      for result in signal_candidates}
            
            for f in as_completed(futures):
                rsi = f.result()
                result = futures[f]
                
                if 'breakout' in result:
                    data = result['breakout']
                    if rsi is not None:
                        final_signals['breakouts'].append({
                            **data,
                            'rsi': rsi
                        })
                
                if 'retest' in result:
                    data = result['retest']
                    if rsi is not None:
                        final_signals['retests'].append({
                            **data,
                            'rsi': rsi
                        })
        
        rsi_duration = time.time() - rsi_start
        print(f"✓ RSI calculation completed in {rsi_duration:.2f}s")
        
        # Apply filters to breakouts
        if MIN_STRENGTH_SCORE > 0 or MIN_CSINCE > 0 or MIN_VOLUME_MULT > 0:
            filtered_breakouts = []
            for b in final_signals['breakouts']:
                if (b['indicator_strength'] >= MIN_STRENGTH_SCORE and 
                    b['csince'] >= MIN_CSINCE and 
                    b['vm'] >= MIN_VOLUME_MULT):
                    filtered_breakouts.append(b)
            
            if len(filtered_breakouts) < len(final_signals['breakouts']):
                print(f"  Filtered breakouts: {len(final_signals['breakouts'])} → {len(filtered_breakouts)}")
                final_signals['breakouts'] = filtered_breakouts
    
    return final_signals

# ==== REPORTING ====
def format_signal_report(signals, duration):
    breakouts = signals['breakouts']
    retests = signals['retests']
    
    if not breakouts and not retests:
        return None
    
    report = f"🚀 <b>SUPERTREND SIGNALS</b> 🚀\n"
    report += f"⏱ Scan: {duration:.2f}s | B: {len(breakouts)} | R: {len(retests)}\n\n"
    
    # Group by hour
    grouped_breakouts = defaultdict(list)
    grouped_retests = defaultdict(list)
    
    for b in breakouts:
        grouped_breakouts[b['hour']].append(b)
    
    for r in retests:
        grouped_retests[r['hour']].append(r)
    
    all_hours = sorted(set(list(grouped_breakouts.keys()) + list(grouped_retests.keys())), reverse=True)
    
    for hour in all_hours:
        report += f"⏰ {hour} UTC\n"
        
        # Breakouts for this hour
        if hour in grouped_breakouts:
            items = sorted(grouped_breakouts[hour], key=lambda x: x['indicator_strength'], reverse=True)
            report += f"\n🟢 <b>BREAKOUTS</b>\n"
            
            for b in items:
                sym = b['symbol'].replace("USDT", "")
                rsi_str = f"{b['rsi']:.1f}"
                csince_str = f"{b['csince']:03d}"
                ind_str = f"{b['indicator_strength']:.2f}"
                
                line1 = f"{sym:6s} {b['pct']:5.2f}% {rsi_str:>4s} {b['vm']:4.1f}x {format_volume(b['vol_usdt']):4s}M {csince_str} {ind_str}"
                line2 = f"       🔴Old: ${b['old_red_line']:.5f} (+{b['red_distance']:.2f}%)"
                line3 = f"       🟢New: ${b['new_green_line']:.5f} (+{b['green_distance']:.2f}%)"
                
                report += f"<code>{line1}</code>\n"
                report += f"   <code>{line2}</code>\n"
                report += f"   <code>{line3}</code>\n"
        
        # Retests for this hour
        if hour in grouped_retests:
            items = sorted(grouped_retests[hour], key=lambda x: x['indicator_strength'], reverse=True)
            report += f"\n🔵 <b>RETESTS</b>\n"
            
            for r in items:
                sym = r['symbol'].replace("USDT", "")
                rsi_str = f"{r['rsi']:.1f}"
                ind_str = f"{r['indicator_strength']:.2f}"
                bars_str = f"{r['bars_in_trend']:02d}"
                
                line1 = f"{sym:6s} {r['pct']:5.2f}% {rsi_str:>4s} {r['vm']:4.1f}x {format_volume(r['vol_usdt']):4s}M {bars_str} {ind_str}"
                line2 = f"       🟢ST: ${r['supertrend']:.5f} (+{r['support_distance']:.2f}%)"
                
                report += f"<code>{line1}</code>\n"
                report += f"   <code>{line2}</code>\n"
        
        report += "\n"
    
    report += "💡 <b>Legend:</b>\n"
    report += "Format: SYMBOL %CHG RSI VMx VolM CSINCE/BARS STRENGTH\n"
    report += "B = Breakout (🟢) | R = Retest (🔵)\n"
    report += "Strength = Indicator Score (0-10) | VM = Volume Multiplier\n"
    report += "CSINCE = Candles since last breakout | BARS = Bars since trend start\n"
    
    return report

# ==== Main ====
def main():
    print("="*80)
    print("🚀 SUPERTREND BREAKOUT + RETEST SCANNER (FIXED VERSION)")
    print("="*80)
    print(f"⚡ Detecting: Breakouts + Retests (TradingView Indicator Logic)")
    
    # Show active filters
    filters_active = []
    if MIN_STRENGTH_SCORE > 0:
        filters_active.append(f"Min Score: {MIN_STRENGTH_SCORE}")
    if MIN_CSINCE > 0:
        filters_active.append(f"Min Csince: {MIN_CSINCE}")
    if MIN_VOLUME_MULT > 0:
        filters_active.append(f"Min VM: {MIN_VOLUME_MULT}x")
    
    if filters_active:
        print(f"🔍 Breakout Filters: {' | '.join(filters_active)}")
    else:
        print(f"🔍 Breakout Filters: NONE (showing all)")
    
    print(f"📊 Retest Settings: VM={VOL_MULT_RETEST}x, MaxBars={RETEST_TIMING_MAX}")
    print(f"📝 Signal Log File: {LOG_FILE}")
    print("="*80)
    
    # Check Telegram credentials
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ WARNING: Telegram credentials not configured!")
        print("   Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables")
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
        
        # Scan for signals
        total_start = time.time()
        signals = scan_all_symbols(symbols)
        total_duration = time.time() - total_start
        
        print(f"\n✓ Complete scan finished in {total_duration:.2f}s")
        print(f"  Total detected: {len(signals['breakouts'])} breakouts, {len(signals['retests'])} retests")
        
        # Filter new signals FIRST (before any messaging)
        fresh_signals = {'breakouts': [], 'retests': []}
        
        for b in signals['breakouts']:
            signal_key = ('B', b['symbol'], b['hour'])
            if signal_key not in reported_signals:
                reported_signals.add(signal_key)
                fresh_signals['breakouts'].append(b)
                # Log to file
                log_signal_to_file(b, 'breakout')
        
        for r in signals['retests']:
            signal_key = ('R', r['symbol'], r['hour'])
            if signal_key not in reported_signals:
                reported_signals.add(signal_key)
                fresh_signals['retests'].append(r)
                # Log to file
                log_signal_to_file(r, 'retest')
        
        fresh_count = len(fresh_signals['breakouts']) + len(fresh_signals['retests'])
        
        # Report status
        if fresh_count > 0:
            print(f"\n🆕 New signals detected:")
            print(f"   • {len(fresh_signals['breakouts'])} breakout(s)")
            print(f"   • {len(fresh_signals['retests'])} retest(s)")
            
            # Generate report
            msg = format_signal_report(fresh_signals, total_duration)
            if msg:
                print(f"\n📤 Sending {fresh_count} alert(s) to Telegram...")
                success = send_telegram(msg[:4096])
                
                if not success:
                    print("  ❌ CRITICAL: Failed to send alert after retries!")
                    print("  🔄 Removing signals from cache to retry next scan...")
                    # Remove failed signals so they get retried
                    for b in fresh_signals['breakouts']:
                        reported_signals.discard(('B', b['symbol'], b['hour']))
                    for r in fresh_signals['retests']:
                        reported_signals.discard(('R', r['symbol'], r['hour']))
        else:
            print(f"\n  ℹ️ No new signals to report (all previously detected)")
        
        # Wait for next hour
        server_time = get_binance_server_time()
        next_hour = (server_time // 3600 + 1) * 3600
        sleep_time = max(60, next_hour - server_time + 5)
        
        print(f"\n😴 Sleeping {sleep_time:.0f}s until next hour (next scan ~{datetime.fromtimestamp(next_hour, tz=timezone.utc).strftime('%H:%M:%S')} UTC)...")
        print(f"{'='*80}\n")
        
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
