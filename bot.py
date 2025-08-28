
# bot.py
# Ready-to-deploy MEXC futures pump scanner with multi-WS connections, signal detection, charting and Telegram alerts.
# Requires environment variables: BOT_TOKEN, CHAT_ID
# Place pairs.json (provided) alongside this file. Designed for Railway deployment.

import os, sys, time, json, math, threading, traceback
from datetime import datetime, timezone
from queue import Queue, Empty
import requests, pandas as pd, numpy as np, websocket, mplfinance as mpf
from PIL import Image
from io import BytesIO

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not BOT_TOKEN or not CHAT_ID:
    print("❌ Please set BOT_TOKEN and CHAT_ID environment variables and restart.")
    # but continue; Telegram sending will fail until set

# Files and endpoints
PAIRS_FILE = "pairs.json"
CONFIG_FILE = "config.json"
CHART_FILE = "signal_chart.png"
CANDLES_DUMP = "candles_store_dump.json"

MEXC_WS_URL = "wss://contract.mexc.com/ws"
MEXC_REST_KLINES = "https://api.mexc.com/api/v3/klines"
MEXC_FUTURES_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"
BYBIT_SYMBOLS_URL = "https://api.bybit.com/v2/public/symbols"

# Load config and pairs
def load_config():
    try:
        with open(CONFIG_FILE,"r") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    # defaults
    defaults = {
        "price_change_percent": 8.0, "rsi_threshold":70.0, "volume_ratio":2.0,
        "timeframe_for_signal":"15m","timeframe_for_chart":"1h","chart_lookback_bars":336,
        "leverage_default":20,"commission_percent":0.005,"scan_interval_seconds":3,
        "subscribe_batch_size":40,"subscribe_batch_delay":0.8,"connections_count":4,
        "max_worker_threads":8,"persist_candles":True,"persist_interval_seconds":120,"max_candles_per_symbol":600
    }
    for k,v in defaults.items():
        cfg.setdefault(k,v)
    return cfg

def load_pairs():
    if os.path.exists("custom_pairs.json"):
        path = "custom_pairs.json"
    else:
        path = PAIRS_FILE
    try:
        with open(path,"r") as f:
            pairs = [p.upper().replace("_","").replace("-","") for p in json.load(f)]
            return pairs
    except Exception as e:
        print("❌ Cannot load pairs.json:", e)
        return ["BTCUSDT","ETHUSDT","BNBUSDT"]

# Shared state
candles_lock = threading.Lock()
candles_store = {}  # symbol -> list of rows {time_open (iso), open, high, low, close, volume}
signal_queue = Queue()
last_closed_ts = {}
shutdown_event = threading.Event()

# Persistence
def dump_candles_to_file():
    cfg = load_config()
    if not cfg.get("persist_candles",True):
        return
    try:
        with candles_lock:
            dump = {s: candles_store.get(s,[])[-cfg.get("max_candles_per_symbol",600):] for s in candles_store}
        with open(CANDLES_DUMP,"w") as f:
            json.dump(dump,f,default=str)
        print("💾 Persisted candles for", len(dump), "symbols")
    except Exception as e:
        print("❌ Persist error:", e)

def load_candles_from_file():
    if not os.path.exists(CANDLES_DUMP):
        return
    try:
        with open(CANDLES_DUMP,"r") as f:
            data = json.load(f)
        with candles_lock:
            for s,rows in data.items():
                candles_store[s]=rows
        print("♻️ Loaded persisted candles for", len(data), "symbols")
    except Exception as e:
        print("❌ Load persist error:", e)

# Utils
def symbol_ws_format(sym):
    if sym.endswith("USDT") and "_" not in sym:
        return sym.replace("USDT","_USDT")
    return sym

def _send_json(ws,obj):
    try:
        ws.send(json.dumps(obj))
    except Exception as e:
        print("❌ Send error:", e)

# Parse MEXC kline payload
def parse_kline_payload(msg):
    try:
        channel = msg.get("channel","")
        if not isinstance(channel,str) or not channel.startswith("push.kline"):
            return None
        symbol_ws = msg.get("symbol")
        if not symbol_ws:
            return None
        symbol = symbol_ws.replace("_","")
        k = msg.get("data",{})
        t = int(k.get("t"))
        o = float(k.get("o")); h = float(k.get("h")); l = float(k.get("l")); c = float(k.get("c")); v = float(k.get("v"))
        return symbol, t, o, h, l, c, v
    except Exception:
        return None

# WebSocket handler factory
def make_ws_handlers(conn_id, assigned_symbols):
    cfg = load_config()
    timeframe = cfg.get("timeframe_for_signal","15m")
    tf_map = {"1m":"Min1","3m":"Min3","5m":"Min5","15m":"Min15","30m":"Min30","1h":"Hour1"}
    tf_ws = tf_map.get(timeframe,"Min15")
    subs_done = {"ready":False}

    def on_open(ws):
        print(f"[WS{conn_id}] Opened, subscribing {len(assigned_symbols)} symbols...")
        batch_size = cfg.get("subscribe_batch_size",40)
        batch_delay = cfg.get("subscribe_batch_delay",0.8)
        try:
            for i in range(0, len(assigned_symbols), batch_size):
                batch = assigned_symbols[i:i+batch_size]
                for sym in batch:
                    sub = {"method":"sub.kline","params":[symbol_ws_format(sym), tf_ws],"id":int(time.time()*1000)%1000000}
                    _send_json(ws, sub)
                time.sleep(batch_delay)
            subs_done["ready"]=True
            print(f"[WS{conn_id}] Subscribed batches finished")
        except Exception as e:
            print(f"[WS{conn_id}] Subscribe error:", e)

    def on_error(ws, error):
        print(f"[WS{conn_id}] Error:", error)

    def on_close(ws, code, reason):
        print(f"[WS{conn_id}] Closed: code={code} reason={reason}")

    def on_message(ws, message):
        if shutdown_event.is_set():
            return
        try:
            data = json.loads(message)
        except Exception:
            return
        parsed = parse_kline_payload(data)
        if parsed is None:
            return
        symbol, t_ms, o, h, l, c, v = parsed
        ts = datetime.fromtimestamp(t_ms/1000.0, tz=timezone.utc).isoformat()
        row = {"time_open":ts,"open":o,"high":h,"low":l,"close":c,"volume":v}
        cfg_local = load_config()
        max_len = cfg_local.get("max_candles_per_symbol",600)
        with candles_lock:
            arr = candles_store.get(symbol)
            if arr is None:
                candles_store[symbol] = [row]
            else:
                if len(arr)>0 and arr[-1]["time_open"]==ts:
                    arr[-1]=row
                elif len(arr)>0 and arr[-1]["time_open"]>ts:
                    pass
                else:
                    arr.append(row)
                    if len(arr)>max_len:
                        candles_store[symbol]=arr[-max_len:]
        # closed candle detection
        tf = cfg_local.get("timeframe_for_signal","15m")
        if tf.endswith("m"):
            tf_minutes=int(tf.rstrip("m"))
        else:
            tf_minutes=60
        dt = datetime.fromisoformat(ts)
        if (dt.minute % tf_minutes == 0) and dt.second==0:
            last = last_closed_ts.get(symbol)
            if last != ts:
                last_closed_ts[symbol]=ts
                signal_queue.put((symbol,ts))

    return on_open, on_message, on_close, on_error

# WS thread
def ws_connection_thread(conn_id, assigned_symbols):
    reconnect_delay = 1
    while not shutdown_event.is_set():
        try:
            on_open, on_message, on_close, on_error = make_ws_handlers(conn_id, assigned_symbols)
            ws = websocket.WebSocketApp(MEXC_WS_URL, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            wst = threading.Thread(target=lambda: ws.run_forever(ping_interval=20,ping_timeout=10), daemon=True)
            wst.start()
            while wst.is_alive() and not shutdown_event.is_set():
                time.sleep(1.0)
            if shutdown_event.is_set():
                try: ws.close()
                except: pass
                break
            print(f"[WS{conn_id}] Connection ended, will reconnect")
        except Exception as e:
            print(f"[WS{conn_id}] Exception: {e}")
            traceback.print_exc()
        time.sleep(min(reconnect_delay,60))
        reconnect_delay = min(reconnect_delay*2,60)

# Signal analysis
def compute_rsi(series,period=14):
    delta = series.diff()
    gain = delta.where(delta>0,0.0)
    loss = -delta.where(delta<0,0.0)
    avg_gain = gain.rolling(window=period,min_periods=period).mean()
    avg_loss = loss.rolling(window=period,min_periods=period).mean()
    rs = avg_gain/(avg_loss+1e-9)
    rsi = 100-(100/(1+rs))
    return rsi

def get_recent_df_from_store(symbol,bars=20):
    with candles_lock:
        arr = candles_store.get(symbol,[])
        rows = arr[-(bars*4):]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time_open"] = pd.to_datetime(df["time_open"])
    df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
    df = df.sort_values("time_open").reset_index(drop=True)
    return df

def analyze_symbol_for_signal(symbol, ts_iso):
    cfg = load_config()
    price_thr = cfg.get("price_change_percent",8.0)
    rsi_thr = cfg.get("rsi_threshold",70.0)
    vol_thr = cfg.get("volume_ratio",2.0)
    tf_chart = cfg.get("timeframe_for_chart","1h")
    chart_bars = cfg.get("chart_lookback_bars",200)
    leverage_default = cfg.get("leverage_default",20)
    commission_pct = cfg.get("commission_percent",0.005)

    df20 = get_recent_df_from_store(symbol,bars=20)
    if df20.empty or len(df20)<6:
        df20 = get_historical_klines_rest(symbol,"15m",40)
        if df20.empty or len(df20)<6:
            return
    if len(df20)>20:
        df20 = df20.iloc[-20:].reset_index(drop=True)
    last = df20.iloc[-1]
    price_change = ((last["close"]-last["open"])/(last["open"]+1e-12))*100.0
    rsi_series = compute_rsi(df20["close"],period=14)
    rsi_val = float(rsi_series.iloc[-1]) if (not rsi_series.isna().iloc[-1]) else None
    if rsi_val is None or math.isnan(rsi_val):
        return
    vol_ratio = float(last["volume"]/(df20["volume"].mean()+1e-9))
    if price_change>=price_thr and rsi_val>=rsi_thr and vol_ratio>=vol_thr:
        print(f"🚨 [{symbol}] Signal! price_change={price_change:.2f}% rsi={rsi_val:.1f} volx{vol_ratio:.2f}")
        df_chart = get_chart_dataframe_for_symbol(symbol, tf_chart, chart_bars)
        build_chart_mplfinance(df_chart, symbol, tf_chart, CHART_FILE, rsi_val=rsi_val, last_price=last["close"])
        send_pump_signal(symbol, price_change, rsi_val, vol_ratio, last["close"], leverage_default, commission_pct)

# Chart helpers
def interval_map(tf):
    return tf

def get_chart_dataframe_for_symbol(symbol, timeframe, limit):
    df = get_recent_df_from_store(symbol,bars=limit*4)
    if df.empty:
        return get_historical_klines_rest(symbol, interval_map(timeframe), limit)
    if len(df)>=3:
        delta = (df["time_open"].diff().dropna().dt.total_seconds().median())
    else:
        delta = 900
    requested = timeframe
    if requested.lower()=="1h" or requested=="1H":
        if abs(delta-900)<30:
            df = df.set_index("time_open")
            df_agg = df.resample("1H").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
            return df_agg.iloc[-limit:].reset_index(drop=True)
    if requested.endswith("m"):
        minutes = int(requested.rstrip("m"))
        if abs(delta - minutes*60) < 30:
            return df.iloc[-limit:].reset_index(drop=True)
    return get_historical_klines_rest(symbol, interval_map(timeframe), limit)

def build_chart_mplfinance(df_chart, pair, timeframe, save_path, title_extra="", rsi_val=50, last_price=0.0):
    try:
        if df_chart is None or df_chart.empty or len(df_chart)<20:
            print("⚠️ Not enough data for chart", pair); return
        chart_data = df_chart.copy()
        if "time_open" in chart_data.columns:
            chart_data.set_index("time_open", inplace=True)
        chart_data.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"}, inplace=True)
        if not isinstance(chart_data.index, pd.DatetimeIndex):
            chart_data.index = pd.to_datetime(chart_data.index)
        berloga_style = mpf.make_mpf_style(base_mpf_style="binance", rc={"font.size":10}, marketcolors=mpf.make_marketcolors(up="green", down="red", wick="white", edge="inherit", volume="inherit"))
        rsi_period = 14
        delta = chart_data["Close"].diff(); gain = delta.where(delta>0,0); loss = -delta.where(delta<0,0)
        avg_gain = gain.rolling(rsi_period).mean(); avg_loss = loss.rolling(rsi_period).mean(); rs = avg_gain/(avg_loss+1e-9)
        rsi = 100-(100/(1+rs))
        apds = [mpf.make_addplot(rsi, panel=1, ylabel="RSI")]
        coin = pair.replace("USDT","")
        title = f"{coin}/USDT {timeframe} RSI:{rsi_val:.1f} price:{last_price:.6f}"
        mpf.plot(chart_data, type="candle", style=berloga_style, addplot=apds, volume=True, ylabel="Price", ylabel_lower="Volume", figratio=(16,9), figscale=1.2, title=title, tight_layout=True, savefig=save_path)
        print("✅ Chart saved", save_path)
    except Exception as e:
        print("❌ Chart build error", e)

# REST fallback
def get_historical_klines_rest(symbol, interval, limit):
    try:
        params = {"symbol":symbol,"interval":interval,"limit":limit}
        headers = {"User-Agent":"Mozilla/5.0"}
        r = requests.get(MEXC_REST_KLINES, params=params, headers=headers, timeout=10)
        if r.status_code==200:
            data = r.json()
            if isinstance(data,list) and len(data)>0:
                df = pd.DataFrame(data, columns=["time_open","open","high","low","close","volume","close_time","quote_volume","trades","taker_base_vol","taker_quote_vol","ignore"])
                df["time_open"]=pd.to_datetime(df["time_open"], unit="ms", utc=True)
                df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
                df = df[["time_open","open","high","low","close","volume"]]; return df
    except Exception as e:
        print("❌ REST klines error", symbol, e)
    return pd.DataFrame()

# Telegram send
def send_telegram_photo(photo_path, caption):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Telegram not configured"); return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path,"rb") as p:
            files={"photo":p}; data={"chat_id":CHAT_ID,"caption":caption,"parse_mode":"HTML"}
            resp = requests.post(url, files=files, data=data, timeout=20); resp.raise_for_status(); print("✅ Telegram sent")
    except Exception as e:
        print("❌ Telegram send error", e)

def is_on_mexc_futures(pair):
    try:
        r = requests.get(MEXC_FUTURES_DETAIL, timeout=8).json()
        if r.get("success"):
            for c in r.get("data",[]):
                if c.get("symbol")==pair.replace("USDT","_USDT") and c.get("displayNameEn","").endswith("PERPETUAL"):
                    return True
    except Exception:
        pass
    return False

def is_on_bybit(pair):
    try:
        r = requests.get(BYBIT_SYMBOLS_URL, timeout=8).json()
        if isinstance(r,dict) and "result" in r:
            for item in r["result"]:
                if item.get("symbol","").upper()==pair.upper():
                    return True
    except Exception:
        pass
    return False

def send_pump_signal(pair, price_change, rsi_val, vol_ratio, current_price, leverage, commission):
    mexc_available = "✅" if is_on_mexc_futures(pair) else "❌"
    bybit_available = "✅" if is_on_bybit(pair) else "❌"
    entry_price = current_price; stop_loss = current_price*0.95; tp1=current_price*1.10; tp2=current_price*1.20
    profit_potential = ((tp1-entry_price)/(entry_price+1e-12))*leverage*100 - (commission*100*2)
    caption = f"""🚀 <b>ПАМП СИГНАЛ!</b>

💰 <b>Пара:</b> #{pair.replace('USDT','')}/{pair[-4:]}
📈 <b>Изменение цены:</b> +{price_change:.2f}%
📊 <b>RSI:</b> {rsi_val:.1f}
📊 <b>Объём:</b> x{vol_ratio:.2f}
💵 <b>Цена:</b> {current_price:.8f} USDT

🎯 <b>ТОРГОВЫЕ ДАННЫЕ:</b>
📍 <b>Вход:</b> {entry_price:.8f} USDT
🛑 <b>Стоп-лосс:</b> {stop_loss:.8f} USDT (-5%)
🎯 <b>Take Profit 1:</b> {tp1:.8f} USDT (+10%)
🚀 <b>Take Profit 2:</b> {tp2:.8f} USDT (+20%)
⚡ <b>Плечо:</b> {leverage}x
💰 <b>Потенциал прибыли:</b> +{profit_potential:.1f}%

🏢 <b>ДОСТУПНОСТЬ НА БИРЖАХ:</b>
MEXC: {mexc_available} | ByBit: {bybit_available}

⏰ {datetime.now().strftime('%H:%M:%S UTC')}
🤖 Автоматический мониторинг MEXC"""
    if os.path.exists(CHART_FILE):
        send_telegram_photo(CHART_FILE, caption)
    else:
        if not BOT_TOKEN or not CHAT_ID:
            print("❌ Telegram not configured, would send:", caption)
            return
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        try:
            data={"chat_id":CHAT_ID,"text":caption,"parse_mode":"HTML"}
            requests.post(url,data=data,timeout=10)
        except Exception as e:
            print("❌ Telegram text send error", e)

# Workers and threads
def compute_rsi_series(series, period=14):
    return compute_rsi(series, period)

def signal_worker_main(worker_id):
    cfg = load_config()
    while not shutdown_event.is_set():
        try:
            item = signal_queue.get(timeout=1.0)
        except Empty:
            time.sleep(0.2); continue
        try:
            symbol, ts = item
            analyze_symbol_for_signal(symbol, ts)
        except Exception as e:
            print("Worker error:", e)
        finally:
            signal_queue.task_done()
        time.sleep(0.01)

def persist_thread():
    cfg = load_config()
    interval = cfg.get("persist_interval_seconds",120)
    while not shutdown_event.is_set():
        time.sleep(interval); dump_candles_to_file()

# Helpers to split and start connections
def split_list(lst, n):
    k,m = divmod(len(lst), n); parts=[]; i=0
    for j in range(n):
        size = k + (1 if j < m else 0)
        parts.append(lst[i:i+size]); i+=size
    return parts

def start_ws_connections(symbols):
    cfg = load_config(); conn_count = max(1,int(cfg.get("connections_count",4)))
    groups = split_list(symbols, conn_count); threads=[]
    for i,group in enumerate(groups):
        t = threading.Thread(target=ws_connection_thread, args=(i+1, group), daemon=True); t.start(); threads.append(t); time.sleep(0.5)
    return threads

def start_workers(max_workers):
    threads=[]
    for i in range(max_workers):
        t=threading.Thread(target=signal_worker_main, args=(i+1,), daemon=True); t.start(); threads.append(t)
    return threads

# Entrypoint
def main():
    print("🚀 Starting MEXC Multi-WS Pump Scanner")
    cfg = load_config()
    pairs = load_pairs()
    symbols = [s.upper().replace("_","").replace("-","") for s in pairs]
    print("Monitoring", len(symbols), "symbols across", cfg.get("connections_count",4), "connections")
    load_candles_from_file()
    if cfg.get("persist_candles", True):
        threading.Thread(target=persist_thread, daemon=True).start()
    start_ws_connections(symbols)
    start_workers(cfg.get("max_worker_threads",8))
    try:
        while True:
            time.sleep(60)
            with candles_lock:
                cached = len(candles_store)
            qsize = signal_queue.qsize()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Candles cached for {cached} symbols. Queue size: {qsize}")
    except KeyboardInterrupt:
        print("Stopping..."); shutdown_event.set(); dump_candles_to_file(); time.sleep(1); sys.exit(0)

if __name__=="__main__":
    main()
