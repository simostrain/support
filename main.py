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
SUPPORT_MAX = 0.5  # Maximum distance from support (0.5%)
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
        return f"{v/1_000_000:.2f}M"
    elif v >= 1_000:
        return f"{v/1_000:.2f}K"
    else:
        return str(v)

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
    gains = [max(c,0) for c in changes]
    losses = [max(-c,0) for c in changes]
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain*(period-1)+gains[i])/period
        avg_loss = (avg_loss*(period-1)+losses[i])/period
    if avg_loss == 0: return 100.0
    rs = avg_gain/avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi,2)

# ==== Supertrend Calculation ====
def calculate_supertrend(candles, current_index, atr_period=10, factor=3.0):
    if current_index < atr_period: return None, None, None, None
    final_upper_band = []
    final_lower_band = []
    supertrend = []
    trend = []
    for idx in range(atr_period, current_index+1):
        atr_values = []
        for i in range(idx-atr_period+1, idx+1):
            high=float(candles[i][2])
            low=float(candles[i][3])
            prev_close=float(candles[i-1][4]) if i>0 else float(candles[i][1])
            tr=max(high-low,abs(high-prev_close),abs(low-prev_close))
            atr_values.append(tr)
        atr=sum(atr_values)/len(atr_values)
        high=float(candles[idx][2])
        low=float(candles[idx][3])
        close=float(candles[idx][4])
        hl2=(high+low)/2
        basic_upper_band=hl2+(factor*atr)
        basic_lower_band=hl2-(factor*atr)
        if idx==atr_period:
            final_ub=basic_upper_band
            final_lb=basic_lower_band
        else:
            prev_close=float(candles[idx-1][4])
            final_ub = basic_upper_band if basic_upper_band<final_upper_band[-1] or prev_close>final_upper_band[-1] else final_upper_band[-1]
            final_lb = basic_lower_band if basic_lower_band>final_lower_band[-1] or prev_close<final_lower_band[-1] else final_lower_band[-1]
        final_upper_band.append(final_ub)
        final_lower_band.append(final_lb)
        if idx==atr_period:
            current_trend=1 if close<=final_ub else -1
            st=final_ub if current_trend==1 else final_lb
        else:
            prev_trend=trend[-1]
            if prev_trend==-1:
                current_trend=1 if close<=final_lb else -1
                st=final_ub if current_trend==1 else final_lb
            else:
                current_trend=-1 if close>final_ub else 1
                st=final_lb if current_trend==-1 else final_ub
        trend.append(current_trend)
        supertrend.append(st)
    return supertrend[-1], trend[-1], final_upper_band[-1], final_lower_band[-1]

# ==== Binance ====
def get_usdt_pairs():
    candidates = [t.upper()+"USDT" for t in CUSTOM_TICKERS]
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=60).json()
        valid = {s["symbol"] for s in data["symbols"] if s["quoteAsset"]=="USDT" and s["status"]=="TRADING"}
        pairs = [c for c in candidates if c in valid]
        print(f"Loaded {len(pairs)} valid USDT pairs.")
        return pairs
    except Exception as e:
        print("Exchange info error:", e)
        return []

def fetch_support_touch(symbol, now_utc, start_time):
    try:
        url=f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=20"
        candles=session.get(url,timeout=60).json()
        if not candles or isinstance(candles,dict): return []
        results=[]
        for i in range(len(candles)-1):
            c=candles[i]
            candle_time=datetime.fromtimestamp(c[0]/1000,tz=timezone.utc)
            if candle_time<start_time or candle_time>=now_utc-timedelta(hours=1): continue
            if i<14: continue
            prev_close=float(candles[i-1][4])
            open_p=float(c[1])
            high=float(c[2])
            low=float(c[3])
            close=float(c[4])
            volume=float(c[5])
            vol_usdt=open_p*volume
            pct=((close-prev_close)/prev_close)*100
            ma_start=max(0,i-19)
            ma_vol=[float(candles[j][1])*float(candles[j][5]) for j in range(ma_start,i+1)]
            ma=sum(ma_vol)/len(ma_vol)
            vm=vol_usdt/ma if ma>0 else 1.0
            all_closes=[float(candles[j][4]) for j in range(0,i+1)]
            rsi=calculate_rsi_with_full_history(all_closes,RSI_PERIOD)
            supertrend_value,direction,upper_band,lower_band=calculate_supertrend(candles,i)
            if direction is None: continue
            if direction==-1:
                support_line=supertrend_value
                distance_from_support=((close-support_line)/support_line)*100
                if SUPPORT_MIN<=distance_from_support<=SUPPORT_MAX:
                    hour=candle_time.strftime("%Y-%m-%d %H:00")
                    distance_to_resistance=((upper_band-close)/close)*100
                    results.append((symbol,pct,close,vol_usdt,vm,rsi,direction,
                                    support_line,distance_from_support,upper_band,distance_to_resistance,hour))
        return results
    except Exception as e:
        print(f"{symbol} error:",e)
        return []

def check_support_touches(symbols):
    now_utc=datetime.now(timezone.utc)
    start_time=now_utc.replace(hour=0,minute=0,second=0,microsecond=0)
    touches=[]
    with ThreadPoolExecutor(max_workers=30) as ex:
        futures=[ex.submit(fetch_support_touch,s,now_utc,start_time) for s in symbols]
        for f in as_completed(futures):
            results=f.result()
            if results: touches.extend(results)
    return touches

def format_support_report(fresh,duration):
    if not fresh: return None
    # Keep only top 10 overall
    top_items=sorted(fresh,key=lambda x:x[8])[:10]
    report=f"📍 <b>SUPPORT TOUCH ALERTS - TOP 10</b> 📍\n"
    report+=f"⏱ Scan: {duration:.2f}s\n\n"
    for symbol,pct,close,vol_usdt,vm,rsi,direction,support_line,distance_from_support,resistance_line,distance_to_resistance,hour in top_items:
        sym=symbol.replace("USDT","")
        rsi_str=f"{rsi:.1f}" if rsi is not None else "N/A"
        line1=f"{sym:6s} {pct:5.2f} {rsi_str:>4s} {vm:4.1f} {format_volume(vol_usdt):6s}"
        line2=f"       🟢Sup: ${support_line:.5f} (+{distance_from_support:.2f}%)"
        line3=f"       🔴Res: ${resistance_line:.5f} (🎯+{distance_to_resistance:.2f}%)"
        if distance_from_support<=1.0: emoji="🎯"
        elif distance_from_support<=2.0: emoji="✅"
        else: emoji="🟢"
        report+=f"{emoji} <code>{line1}</code>\n"
        report+=f"   <code>{line2}</code>\n"
        report+=f"   <code>{line3}</code>\n\n"
    report+="💡 🟢Sup = Support line (buy zone)\n"
    report+="💡 🔴Res = Resistance line (profit target)\n"
    report+="💡 Closer to support = Better entry!\n"
    return report

# ==== Main ====
def main():
    symbols=get_usdt_pairs()
    if not symbols: return
    while True:
        start=time.time()
        touches=check_support_touches(symbols)
        duration=time.time()-start
        fresh=[]
        for t in touches:
            key=(t[0],t[11])
            if key not in reported:
                reported.add(key)
                fresh.append(t)
        if fresh:
            msg=format_support_report(fresh,duration)
            if msg:
                print(msg)
                send_telegram(msg[:4096])
        else:
            print(f"No support touch opportunities. Scanned {len(symbols)} pairs in {duration:.2f}s")
        server=get_binance_server_time()
        next_hour=(server//3600+1)*3600
        sleep_time=max(0,next_hour-server+1)
        print(f"Sleeping for {sleep_time:.0f}s until next hour...")
        time.sleep(sleep_time)

if __name__=="__main__":
    main()
