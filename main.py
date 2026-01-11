import os
import requests
import time
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ==== Settings ====
BINANCE_API = "https://api.binance.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MAX_CANDLE_MOVE_1H = 1.05     # ±1.05% per 1h candle (vs previous close)
VOL_MULT_THRESHOLD = 1.5       # volume multiplier
MIN_MOMENTUM_PCT = 1.0        # min 15m move for momentum (1%)

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
    "SOMI","W","WAL","XPL","ZBT","ZKC","BREV","ZKP"
]

LOG_FILE = Path("/tmp/two_stage_log.json")
reported_signals = set()
stable_coins_cache = []

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=2)
session.mount("https://", adapter)

def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=5).json()["serverTime"] / 1000
    except:
        return time.time()

def log_signal_to_file(signal_data):
    log_entry = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'data': signal_data
    }
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    except Exception:
        pass

def send_telegram(msg, max_retries=3):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured!")
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
                return True
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(2)
    return False

def is_price_stable_6h(candles_1h):
    """Check last 6 hourly candles: each vs previous close within ±1.05%"""
    if len(candles_1h) < 7:
        return False
    for i in range(-6, 0):
        prev_close = float(candles_1h[i-1][4])
        close = float(candles_1h[i][4])
        if prev_close == 0:
            return False
        move_pct = abs((close - prev_close) / prev_close) * 100
        if move_pct > MAX_CANDLE_MOVE_1H:
            return False
    return True

def scan_stable_coins_hourly(symbols):
    stable = []
    print("🕒 Hourly scan: checking 6h price stability...")
    with ThreadPoolExecutor(max_workers=80) as ex:
        futures = {ex.submit(_check_stability, s): s for s in symbols}
        for f in as_completed(futures):
            result = f.result()
            if result:
                stable.append(result)
    print(f"✅ Found {len(stable)} stable coins")
    return stable

def _check_stability(symbol):
    try:
        candles_1h = session.get(f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=12", timeout=5).json()
        if candles_1h and len(candles_1h) >= 7 and is_price_stable_6h(candles_1h):
            max_move = 0
            for i in range(-6, 0):
                prev_close = float(candles_1h[i-1][4])
                close = float(candles_1h[i][4])
                move = abs((close - prev_close) / prev_close) * 100
                if move > max_move: max_move = move
            return {'symbol': symbol, 'max_1h_move_6h': max_move}
    except Exception:
        pass
    return None

def detect_15m_signals(symbols_with_stability):
    candidates = []
    symbol_to_max_move = {item['symbol']: item['max_1h_move_6h'] for item in symbols_with_stability}
    symbols = [item['symbol'] for item in symbols_with_stability]

    with ThreadPoolExecutor(max_workers=80) as ex:
        futures = {ex.submit(_check_15m_conditions, s, symbol_to_max_move[s]): s for s in symbols}
        for f in as_completed(futures):
            result = f.result()
            if result:
                candidates.append(result)
    return candidates

def _check_15m_conditions(symbol, max_1h_move_6h):
    try:
        candles_15m = session.get(f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=20", timeout=5).json()
        if not candles_15m or len(candles_15m) < 13:
            return None

        current_close = float(candles_15m[-2][4])
        prev_close = float(candles_15m[-3][4])
        current_volume = float(candles_15m[-2][5])
        candle_time = datetime.fromtimestamp(candles_15m[-2][0]/1000, tz=timezone.utc)
        time_str = candle_time.strftime("%H:%M")

        pct_move = ((current_close - prev_close) / prev_close) * 100

        volumes = [float(c[5]) for c in candles_15m]
        avg_vol = sum(volumes[-13:-5]) / 8  # candles [-13] to [-6] → 8 candles
        vol_ratio = current_volume / avg_vol if avg_vol > 0 else 0

        # 🔸 Condition B: Momentum Kick (≥1%)
        if pct_move >= MIN_MOMENTUM_PCT:
            return {
                'type': 'momentum',
                'symbol': symbol,
                'price': current_close,
                'pct_15m': pct_move,
                'vol_ratio': vol_ratio,
                'time_str': time_str
            }

        # 🔹 Condition A: Accumulation (last 2 candles ≥1.5x volume)
        last_4_vols = volumes[-4:]  # [-4, -3, -2, -1]
        # We care about last 2 CLOSED candles: [-3] and [-2] → last_4_vols[-2:]
        valid_volume = (
            vol_ratio >= VOL_MULT_THRESHOLD and
            all(v / avg_vol >= VOL_MULT_THRESHOLD for v in last_4_vols[-2:])  # ← CHANGED TO [-2:]
        )
        if valid_volume:
            return {
                'type': 'accumulation',
                'symbol': symbol,
                'price': current_close,
                'pct_15m': pct_move,
                'vol_ratio': vol_ratio,
                'max_1h_move_6h': max_1h_move_6h,
                'time_str': time_str
            }

    except Exception:
        pass
    return None

def format_alert(signal):
    sym = signal['symbol'].replace("USDT", "")
    price = signal['price']
    time_str = signal['time_str']
    vol_ratio = signal['vol_ratio']
    pct_15m = signal['pct_15m']
    
    if signal['type'] == "momentum":
        msg = f"🚀 <b>MOMENTUM KICK</b> 🚀\n"
        msg += f"Symbol: <b>{sym}</b>\n"
        msg += f"Price: ${price:.5f}\n"
        msg += f"Time: {time_str} UTC\n"
        msg += f"15m Move: +{pct_15m:.2f}%\n"
        msg += f"15m Vol: {vol_ratio:.1f}x average\n\n"
        msg += f"🔥 Early move after quiet period!"
    else:  # accumulation
        max_move = signal['max_1h_move_6h']
        msg = f"🔍 <b>ACCUMULATION ALERT</b> 🔍\n"
        msg += f"Symbol: <b>{sym}</b>\n"
        msg += f"Price: ${price:.5f}\n"
        msg += f"Time: {time_str} UTC\n"
        msg += f"15m Move: +{pct_15m:.2f}%\n"
        msg += f"15m Vol: {vol_ratio:.1f}x average\n"
        msg += f"Max 1h move (last 6h): ±{max_move:.2f}%\n\n"
        msg += f"⚠️ Strong volume after quiet period!"

    return msg

def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=10).json()
        valid = {s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        return [c for c in candidates if c in valid]
    except:
        return []

def main():
    print("="*60)
    print("🎯 FINAL SCANNER: LAST 2 CANDLES VOLUME, 6H STABILITY")
    print("="*60)
    print(f"📊 Accumulation: last 2 closed candles ≥1.5x vol")
    print(f"📊 Momentum: ≥1.0% move on 15m")
    print(f"📊 Stability: 6h, ±1.05% per hour vs prev close")
    print("="*60)

    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols loaded")
        return

    print(f"✓ Monitoring {len(symbols)} pairs\n")

    next_hourly_scan = 0

    while True:
        server_time = get_binance_server_time()
        current_hour = int(server_time // 3600)

        if current_hour >= next_hourly_scan:
            stable_coins_cache[:] = scan_stable_coins_hourly(symbols)
            next_hourly_scan = current_hour + 1

        if stable_coins_cache:
            signals = detect_15m_signals(stable_coins_cache)
            fresh_signals = []

            for sig in signals:
                key = (sig['symbol'], sig['type'], sig['time_str'])
                if key not in reported_signals:
                    reported_signals.add(key)
                    fresh_signals.append(sig)
                    log_signal_to_file(sig)

            if fresh_signals:
                for sig in fresh_signals:
                    msg = format_alert(sig)
                    send_telegram(msg)

        next_15m = (server_time // 900 + 1) * 900
        sleep_time = max(30, next_15m - server_time + 2)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
