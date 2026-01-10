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

# Your exact thresholds
MAX_CANDLE_MOVE_1H = 1.0      # ±1% per 1h candle (for both conditions)
VOL_MULT_THRESHOLD = 1.5      # for accumulation
MIN_MOMENTUM_PCT = 0.8        # for momentum kick

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

LOG_FILE = Path("/tmp/accumulation_log.json")
reported_signals = set()

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
    """Check if last 6 hourly candles each moved within ±1%"""
    if len(candles_1h) < 6:
        return False
    for i in range(-6, 0):
        c = candles_1h[i]
        open_p = float(c[1])
        close = float(c[4])
        if open_p == 0:
            return False
        move_pct = abs((close - open_p) / open_p) * 100
        if move_pct > MAX_CANDLE_MOVE_1H:
            return False
    return True

def has_volume_spike_15m(candles_15m):
    """Check volume spike: current ≥1.5x avg, last 3 ≥1.5x avg"""
    if len(candles_15m) < 12:
        return False
    volumes = [float(c[5]) for c in candles_15m]
    avg_vol = sum(volumes[-12:-4]) / 8  # avg of 8 candles before last 4
    if avg_vol == 0:
        return False
    last_4_vols = volumes[-4:]
    current_vol = last_4_vols[-1]
    if current_vol < VOL_MULT_THRESHOLD * avg_vol:
        return False
    for vol in last_4_vols[-3:]:
        if vol < VOL_MULT_THRESHOLD * avg_vol:
            return False
    return True

def detect_opportunity(symbol):
    try:
        # Fetch data
        candles_1h = session.get(f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=12", timeout=5).json()
        candles_15m = session.get(f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=20", timeout=5).json()
        
        if not candles_1h or len(candles_1h) < 6 or not candles_15m or len(candles_15m) < 12:
            return None

        # Check price stability first (required for both conditions)
        if not is_price_stable_6h(candles_1h):
            return None

        current_15m = candles_15m[-2]  # last fully closed candle
        open_15m = float(current_15m[1])
        close_15m = float(current_15m[4])
        pct_15m = ((close_15m - open_15m) / open_15m) * 100

        signal_type = None
        details = {}

        # 🔸 Condition B: Momentum Kick (≥0.8% move)
        if pct_15m >= MIN_MOMENTUM_PCT:
            signal_type = "momentum"
            details = {'pct_15m': pct_15m}

        # 🔹 Condition A: Accumulation (volume spike)
        elif has_volume_spike_15m(candles_15m):
            signal_type = "accumulation"
            avg_vol_8 = sum(float(c[5]) for c in candles_15m[-12:-4]) / 8
            current_vol = float(candles_15m[-2][5])
            vol_ratio = current_vol / avg_vol_8 if avg_vol_8 > 0 else 0
            # Calculate max 1h move in last 6h
            max_move = 0
            for i in range(-6, 0):
                c = candles_1h[i]
                o = float(c[1]); cl = float(c[4])
                move = abs((cl - o) / o) * 100
                if move > max_move: max_move = move
            details = {'vol_ratio': vol_ratio, 'max_1h_move_6h': max_move}

        if signal_type:
            return {
                'symbol': symbol,
                'price': close_15m,
                'type': signal_type,
                'details': details
            }

    except Exception:
        return None

def scan_all_symbols(symbols):
    candidates = []
    with ThreadPoolExecutor(max_workers=80) as ex:
        futures = {ex.submit(detect_opportunity, s): s for s in symbols}
        for f in as_completed(futures):
            result = f.result()
            if result:
                candidates.append(result)
    return candidates

def format_alert(signal):
    sym = signal['symbol'].replace("USDT", "")
    price = signal['price']
    
    if signal['type'] == "momentum":
        pct = signal['details']['pct_15m']
        msg = f"🚀 <b>MOMENTUM KICK</b> 🚀\n"
        msg += f"Symbol: <b>{sym}</b>\n"
        msg += f"Price: ${price:.5f}\n"
        msg += f"15m Move: +{pct:.2f}%\n\n"
        msg += f"🔥 Early move after quiet period!"
    else:  # accumulation
        vol_ratio = signal['details']['vol_ratio']
        max_move = signal['details']['max_1h_move_6h']
        msg = f"🔍 <b>ACCUMULATION ALERT</b> 🔍\n"
        msg += f"Symbol: <b>{sym}</b>\n"
        msg += f"Price: ${price:.5f}\n"
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
    print("🎯 FINAL ACCUMULATION + MOMENTUM SCANNER")
    print("="*60)
    print(f"📊 BOTH CONDITIONS REQUIRE:")
    print(f"   • Last 6h: each 1h candle ∈ [−1%, +1%]")
    print(f"🔹 Accumulation: Volume ≥1.5x (last 3 candles too)")
    print(f"🔸 Momentum: 15m move ≥ +0.8%")
    print("="*60)

    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols loaded")
        return

    print(f"✓ Monitoring {len(symbols)} pairs\n")

    while True:
        signals = scan_all_symbols(symbols)
        fresh_signals = []

        for sig in signals:
            key = (sig['symbol'], sig['type'], round(sig['price'], 4))
            if key not in reported_signals:
                reported_signals.add(key)
                fresh_signals.append(sig)
                log_signal_to_file(sig)

        if fresh_signals:
            for sig in fresh_signals:
                msg = format_alert(sig)
                send_telegram(msg)

        server_time = get_binance_server_time()
        next_interval = (server_time // 900 + 1) * 900
        time.sleep(max(30, next_interval - server_time + 2))

if __name__ == "__main__":
    main()
