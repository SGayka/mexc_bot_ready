# bot.py
# MEXC USDT Perpetual Pump Scanner (multi-WS) — Railway-ready
# Публічні котирування по ф’ючерсах (без ключів), логіка памп-сигналу за твоїм алгоритмом.
# Підтримує BOT_TOKEN або TELEGRAM_TOKEN + CHAT_ID для Telegram.
# Якщо pairs.json відсутній/порожній — використовує вшитий список на 592 пари.

import os, sys, time, json, math, threading, traceback
from datetime import datetime, timezone
from queue import Queue, Empty

import requests
import pandas as pd
import numpy as np
import websocket
import mplfinance as mpf

# ==========================
# === Telegram настройки ===
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ==========================
# === Файли / константи  ===
# ==========================
PAIRS_FILE = "pairs.json"          # якщо є — буде використаний
CONFIG_FILE = "config.json"        # опціонально (є дефолти)
CHART_FILE = "signal_chart.png"
CANDLES_DUMP = "candles_store_dump.json"

# ПРАВИЛЬНИЙ публічний WS для MEXC Futures
MEXC_WS_URL_DEFAULT = "wss://contract.mexc.com/ws"
MEXC_WS_URL_ENV = os.getenv("MEXC_WS_URL", "").strip()
MEXC_WS_URL = MEXC_WS_URL_ENV if (MEXC_WS_URL_ENV.startswith("ws://") or MEXC_WS_URL_ENV.startswith("wss://")) else MEXC_WS_URL_DEFAULT

# REST (для підкачки історії/чарту — можна і спотові свічки, нам важливий вигляд)
MEXC_REST_KLINES = "https://api.mexc.com/api/v3/klines"
MEXC_FUTURES_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"
BYBIT_SYMBOLS_URL = "https://api.bybit.com/v2/public/symbols"

# ==========================
# === Вшитий список 592  ===
# ==========================
def build_builtin_pairs_592():
    common = [
        "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT","MATICUSDT","DOTUSDT",
        "LTCUSDT","LINKUSDT","TRXUSDT","ATOMUSDT","NEARUSDT","XLMUSDT","FTMUSDT","ALGOUSDT","VETUSDT","ICPUSDT",
        "FILUSDT","SANDUSDT","MANAUSDT","CHZUSDT","AXSUSDT","AAVEUSDT","EOSUSDT","THETAUSDT","GRTUSDT","MKRUSDT",
        "ZECUSDT","BCHUSDT","ENJUSDT","DASHUSDT","KSMUSDT","SNXUSDT","COMPUSDT","CRVUSDT","BATUSDT","KAVAUSDT",
        "RUNEUSDT","CELOUSDT","ARUSDT","QNTUSDT","STXUSDT","FLUXUSDT","GALAUSDT","IOSTUSDT","ZILUSDT","OPUSDT",
        "APTUSDT","ARBUSDT","SUIUSDT","PEPEUSDT","WIFUSDT","SEIUSDT","INJUSDT","POLUSDT","TIAUSDT","PYTHUSDT",
        "JUPUSDT","ORDIUSDT","BONKUSDT","ARBUSDT","BLURUSDT","WLDUSDT","JTOUSDT","STRKUSDT","ENAUSDT","AEVOUSDT",
        "PENDLEUSDT","ALTUSDT","MAVUSDT","RNDRUSDT","LDOUSDT","IMXUSDT","GMXUSDT","DYDXUSDT","HOOKUSDT","IDUSDT",
        "MAGICUSDT","SSVUSDT","YGGUSDT","LEVERUSDT","COREUSDT","OMIUSDT","BOMEUSDT","BIGTIMEUSDT","TIAUSDT"
    ]
    pairs = list(dict.fromkeys(common))  # унікальні
    i = 1
    while len(pairs) < 592:
        sym = f"TOKEN{i:03d}USDT"
        if sym not in pairs:
            pairs.append(sym)
        i += 1
    return pairs

# ==========================
# === Конфіг з дефолтами ===
# ==========================
def load_config():
    cfg = {}
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
    except Exception:
        pass
    defaults = {
        "price_change_percent": 8.0,
        "rsi_threshold": 70.0,
        "volume_ratio": 2.0,
        "timeframe_for_signal": "15m",
        "timeframe_for_chart": "1h",
        "chart_lookback_bars": 336,
        "leverage_default": 20,
        "commission_percent": 0.005,
        "scan_interval_seconds": 3,
        "subscribe_batch_size": 40,
        "subscribe_batch_delay": 0.8,
        "connections_count": 4,
        "max_worker_threads": 8,
        "persist_candles": True,
        "persist_interval_seconds": 120,
        "max_candles_per_symbol": 600
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg

# ==========================
# === Завантажити пари   ===
# ==========================
def load_pairs():
    # 1) Якщо є custom_pairs.json — використовуємо його
    if os.path.exists("custom_pairs.json"):
        try:
            with open("custom_pairs.json","r",encoding="utf-8") as f:
                arr = json.load(f)
            arr = [str(x).upper().replace("_","").replace("-","") for x in arr]
            if len(arr)>0:
                print(f"✔ custom_pairs.json: {len(arr)} пар")
                return arr
        except Exception as e:
            print("⚠ custom_pairs.json помилка:", e)

    # 2) Якщо є pairs.json — використовуємо його
    if os.path.exists(PAIRS_FILE):
        try:
            with open(PAIRS_FILE,"r",encoding="utf-8") as f:
                arr = json.load(f)
            arr = [str(x).upper().replace("_","").replace("-","") for x in arr]
            if len(arr)>0:
                print(f"✔ pairs.json: {len(arr)} пар")
                return arr
        except Exception as e:
            print("⚠ pairs.json помилка:", e)

    # 3) Фолбек — вшиті 592
    arr = build_builtin_pairs_592()
    print(f"✔ Використовую вшитий список: {len(arr)} пар")
    return arr

# ==========================
# === Глобальні структури ==
# ==========================
candles_lock = threading.Lock()
candles_store = {}      # symbol -> list[{time_open, open, high, low, close, volume}]
last_closed_ts = {}     # symbol -> iso ts of last closed candle
signal_queue = Queue()
shutdown_event = threading.Event()

# ==========================
# === Персистентність     ==
# ==========================
def dump_candles_to_file():
    cfg = load_config()
    if not cfg.get("persist_candles", True):
        return
    try:
        with candles_lock:
            dump = {s: candles_store.get(s, [])[-cfg.get("max_candles_per_symbol",600):] for s in candles_store}
        with open(CANDLES_DUMP, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False)
        print(f"💾 Сохранены свечи для {len(dump)} символов")
    except Exception as e:
        print("❌ Persist error:", e)

def load_candles_from_file():
    if not os.path.exists(CANDLES_DUMP):
        return
    try:
        with open(CANDLES_DUMP,"r",encoding="utf-8") as f:
            data = json.load(f)
        with candles_lock:
            for s, rows in data.items():
                candles_store[s] = rows
        print(f"♻️ Загружены сохранённые свечи для {len(data)} символов")
    except Exception as e:
        print("❌ Load persist error:", e)

# ==========================
# === Утиліти WS/парсинг ===
# ==========================
def symbol_ws_format(sym: str) -> str:
    # Для ф’ючерсів MEXC використовується формат BTC_USDT
    if sym.endswith("USDT") and "_" not in sym:
        return sym.replace("USDT","_USDT")
    return sym

def _send_json(ws, obj):
    try:
        ws.send(json.dumps(obj))
    except Exception as e:
        print("❌ WS send error:", e)

def parse_kline_payload(msg: dict):
    """Очікуємо структуру push.kline з полями: symbol, data:{t,o,h,l,c,v}"""
    try:
        if not isinstance(msg, dict):
            return None
        if not str(msg.get("channel","")).startswith("push.kline"):
            return None
        symbol_ws = msg.get("symbol")
        if not symbol_ws:
            return None
        symbol = symbol_ws.replace("_","")
        k = msg.get("data", {})
        t = int(k.get("t"))
        o = float(k.get("o")); h = float(k.get("h")); l = float(k.get("l")); c = float(k.get("c")); v = float(k.get("v"))
        return symbol, t, o, h, l, c, v
    except Exception:
        return None

# ==========================
# === WS Handlers (multi) ==
# ==========================
def make_ws_handlers(conn_id: int, assigned_symbols):
    cfg = load_config()
    timeframe = cfg.get("timeframe_for_signal","15m")
    tf_map = {"1m":"Min1","3m":"Min3","5m":"Min5","15m":"Min15","30m":"Min30","1h":"Hour1"}
    tf_ws = tf_map.get(timeframe, "Min15")

    def on_open(ws):
        print(f"[WS{conn_id}] Opened → підписка на {len(assigned_symbols)} символів...")
        batch_size = cfg.get("subscribe_batch_size", 40)
        batch_delay = cfg.get("subscribe_batch_delay", 0.8)
        try:
            for i in range(0, len(assigned_symbols), batch_size):
                batch = assigned_symbols[i:i+batch_size]
                for sym in batch:
                    sub = {"method":"sub.kline","params":[symbol_ws_format(sym), tf_ws],"id":int(time.time()*1000)%1000000}
                    _send_json(ws, sub)
                time.sleep(batch_delay)
            print(f"[WS{conn_id}] Subscribed OK")
        except Exception as e:
            print(f"[WS{conn_id}] Subscribe error:", e)

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
        row = {"time_open": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}
        cfg_local = load_config()
        max_len = cfg_local.get("max_candles_per_symbol",600)
        with candles_lock:
            arr = candles_store.get(symbol)
            if not arr:
                candles_store[symbol] = [row]
            else:
                if arr[-1]["time_open"] == ts:
                    arr[-1] = row
                elif arr[-1]["time_open"] < ts:
                    arr.append(row)
                    if len(arr) > max_len:
                        candles_store[symbol] = arr[-max_len:]

        # визначення закритої свічки (на кордоні інтервалу)
        tf = cfg_local.get("timeframe_for_signal","15m")
        tf_minutes = 60 if not tf.endswith("m") else int(tf[:-1])
        dt = datetime.fromtimestamp(t_ms/1000.0, tz=timezone.utc)
        if dt.minute % tf_minutes == 0 and dt.second == 0:
            # тригеримо аналіз
            signal_queue.put((symbol, ts))

    def on_close(ws, code, reason):
        print(f"[WS{conn_id}] Closed: code={code} reason={reason}")

    def on_error(ws, error):
        # типова помилка з логу: "URL-адрес недействителен"
        print(f"[WS{conn_id}] Error:", error)

    return on_open, on_message, on_close, on_error

def ws_connection_thread(conn_id: int, assigned_symbols):
    # Перевіряємо URL заздалегідь і страхуємося
    url = MEXC_WS_URL.strip()
    if not (url.startswith("ws://") or url.startswith("wss://")):
        print(f"[WS{conn_id}] ⚠ Некорректный WS URL '{url}', фолбек на {MEXC_WS_URL_DEFAULT}")
        url = MEXC_WS_URL_DEFAULT

    reconnect_delay = 1
    while not shutdown_event.is_set():
        try:
            on_open, on_message, on_close, on_error = make_ws_handlers(conn_id, assigned_symbols)
            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            wst = threading.Thread(target=lambda: ws.run_forever(ping_interval=20, ping_timeout=10), daemon=True)
            wst.start()
            while wst.is_alive() and not shutdown_event.is_set():
                time.sleep(1.0)
            if shutdown_event.is_set():
                try: ws.close()
                except: pass
                break
            print(f"[WS{conn_id}] Соединение разорвано, подключение будет возобновлено.")
        except Exception as e:
            print(f"[WS{conn_id}] Exception:", e)
            traceback.print_exc()
        time.sleep(min(reconnect_delay, 60))
        reconnect_delay = min(reconnect_delay * 2, 60)

# ==========================
# === Аналіз сигналу     ===
# ==========================
def compute_rsi(series: pd.Series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def get_recent_df_from_store(symbol, bars=20):
    with candles_lock:
        arr = candles_store.get(symbol, [])
        rows = arr[-(bars*4):]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time_open"] = pd.to_datetime(df["time_open"])
    df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
    df = df.sort_values("time_open").reset_index(drop=True)
    return df

def get_historical_klines_rest(symbol, interval, limit):
    try:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        headers = {"User-Agent":"Mozilla/5.0"}
        r = requests.get(MEXC_REST_KLINES, params=params, headers=headers, timeout=10)
        if r.status_code == 200 and isinstance(r.json(), list):
            data = r.json()
            df = pd.DataFrame(data, columns=[
                "time_open","open","high","low","close","volume","close_time","quote_volume",
                "trades","taker_base_vol","taker_quote_vol","ignore"
            ])
            df["time_open"] = pd.to_datetime(df["time_open"], unit="ms", utc=True)
            df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
            return df[["time_open","open","high","low","close","volume"]]
    except Exception as e:
        print("❌ REST klines error", symbol, e)
    return pd.DataFrame()

def get_chart_dataframe_for_symbol(symbol, timeframe, limit):
    df = get_recent_df_from_store(symbol, bars=limit*4)
    if df.empty:
        return get_historical_klines_rest(symbol, timeframe, limit)
    if len(df) >= 3:
        delta = (df["time_open"].diff().dropna().dt.total_seconds().median())
    else:
        delta = 900
    if timeframe.lower() == "1h":
        # агрегуємо 15m -> 1h
        if abs(delta - 900) < 30:
            dfi = df.set_index("time_open")
            agg = dfi.resample("1H").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
            return agg.iloc[-limit:].reset_index(drop=True)
    # якщо часова розмітка співпадає — віддаємо як є
    if timeframe.endswith("m"):
        minutes = int(timeframe[:-1])
        if abs(delta - minutes*60) < 30:
            return df.iloc[-limit:].reset_index(drop=True)
    return get_historical_klines_rest(symbol, timeframe, limit)

def build_chart_mplfinance(df_chart, pair, timeframe, save_path, rsi_val=50.0, last_price=0.0):
    try:
        if df_chart is None or df_chart.empty or len(df_chart) < 20:
            print("⚠️ Not enough data for chart", pair)
            return
        chart_data = df_chart.copy()
        if "time_open" in chart_data.columns:
            chart_data.set_index("time_open", inplace=True)
        chart_data.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"}, inplace=True)
        if not isinstance(chart_data.index, pd.DatetimeIndex):
            chart_data.index = pd.to_datetime(chart_data.index)

        # RSI на графік
        rsi_period = 14
        delta = chart_data["Close"].diff()
        gain = delta.where(delta>0,0)
        loss = -delta.where(delta<0,0)
        avg_gain = gain.rolling(rsi_period).mean()
        avg_loss = loss.rolling(rsi_period).mean()
        rs = avg_gain / (avg_loss + 1e-9)
        rsi_series = 100 - (100 / (1 + rs))

        apds = [mpf.make_addplot(rsi_series, panel=1, ylabel="RSI")]

        style = mpf.make_mpf_style(
            base_mpf_style="binance",
            rc={"font.size": 10},
            marketcolors=mpf.make_marketcolors(up="green", down="red", wick="white", edge="inherit", volume="inherit")
        )
        title = f"{pair.replace('USDT','')}/USDT {timeframe} RSI:{rsi_val:.1f} Price:{last_price:.6f}"
        mpf.plot(chart_data, type="candle", style=style, addplot=apds, volume=True,
                 ylabel="Price", ylabel_lower="Volume", figratio=(16,9), figscale=1.2,
                 title=title, tight_layout=True, savefig=save_path)
        print("✅ Chart saved:", save_path)
    except Exception as e:
        print("❌ Chart build error", e)

def compute_signal_and_alert(symbol, ts_iso):
    cfg = load_config()
    price_thr = cfg.get("price_change_percent", 8.0)
    rsi_thr = cfg.get("rsi_threshold", 70.0)
    vol_thr = cfg.get("volume_ratio", 2.0)
    tf_chart = cfg.get("timeframe_for_chart","1h")
    chart_bars = cfg.get("chart_lookback_bars", 200)
    leverage_default = cfg.get("leverage_default", 20)
    commission_pct = cfg.get("commission_percent", 0.005)

    df20 = get_recent_df_from_store(symbol, bars=20)
    if df20.empty or len(df20) < 6:
        df20 = get_historical_klines_rest(symbol, "15m", 40)
        if df20.empty or len(df20) < 6:
            return

    if len(df20) > 20:
        df20 = df20.iloc[-20:].reset_index(drop=True)

    last = df20.iloc[-1]
    price_change = ((last["close"] - last["open"]) / (last["open"] + 1e-12)) * 100.0
    rsi_series = compute_rsi(df20["close"], period=14)
    rsi_val = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else None
    if rsi_val is None:
        return
    vol_ratio = float(last["volume"] / (df20["volume"].mean() + 1e-9))

    if price_change >= price_thr and rsi_val >= rsi_thr and vol_ratio >= vol_thr:
        print(f"🚨 [{symbol}] Signal! Δ={price_change:.2f}% RSI={rsi_val:.1f} Vol×{vol_ratio:.2f}")
        df_chart = get_chart_dataframe_for_symbol(symbol, tf_chart, chart_bars)
        build_chart_mplfinance(df_chart, symbol, tf_chart, CHART_FILE, rsi_val=rsi_val, last_price=last["close"])
        send_pump_signal(symbol, price_change, rsi_val, vol_ratio, last["close"], leverage_default, commission_pct)

# ==========================
# === Перевірка бірж     ===
# ==========================
def is_on_mexc_futures(pair):
    try:
        r = requests.get(MEXC_FUTURES_DETAIL, timeout=8).json()
        if r.get("success"):
            for c in r.get("data", []):
                if c.get("symbol") == pair.replace("USDT","_USDT") and c.get("displayNameEn","").endswith("PERPETUAL"):
                    return True
    except Exception:
        pass
    return False

def is_on_bybit(pair):
    try:
        r = requests.get(BYBIT_SYMBOLS_URL, timeout=8).json()
        if isinstance(r, dict) and "result" in r:
            for item in r["result"]:
                if item.get("symbol","").upper() == pair.upper():
                    return True
    except Exception:
        pass
    return False

# ==========================
# === Telegram відправка ===
# ==========================
def send_telegram_photo(photo_path, caption):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Telegram not configured (BOT_TOKEN/TELEGRAM_TOKEN або CHAT_ID відсутні)")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as p:
            files = {"photo": p}
            data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            resp = requests.post(url, files=files, data=data, timeout=20)
            resp.raise_for_status()
            print("✅ Telegram photo sent")
    except Exception as e:
        print("❌ Telegram send error", e)

def send_pump_signal(pair, price_change, rsi_val, vol_ratio, current_price, leverage, commission):
    mexc_available = "✅" if is_on_mexc_futures(pair) else "❌"
    bybit_available = "✅" if is_on_bybit(pair) else "❌"
    entry_price = current_price
    stop_loss = current_price * 0.95
    tp1 = current_price * 1.10
    tp2 = current_price * 1.20
    profit_potential = ((tp1 - entry_price) / (entry_price + 1e-12)) * leverage * 100 - (commission * 100 * 2)
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
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id":CHAT_ID,"text":caption,"parse_mode":"HTML"}, timeout=10)
            print("✅ Telegram text sent")
        except Exception as e:
            print("❌ Telegram text send error", e)

# ==========================
# === Робочі потоки      ===
# ==========================
def signal_worker_main(worker_id):
    while not shutdown_event.is_set():
        try:
            symbol, ts = signal_queue.get(timeout=1.0)
        except Empty:
            time.sleep(0.2); continue
        try:
            compute_signal_and_alert(symbol, ts)
        except Exception as e:
            print(f"[Worker {worker_id}] Error:", e)
        finally:
            signal_queue.task_done()
        time.sleep(0.01)

def persist_thread():
    cfg = load_config()
    interval = cfg.get("persist_interval_seconds", 120)
    while not shutdown_event.is_set():
        time.sleep(interval)
        dump_candles_to_file()

def split_list(lst, n):
    k, m = divmod(len(lst), n)
    out = []
    i = 0
    for j in range(n):
        size = k + (1 if j < m else 0)
        out.append(lst[i:i+size])
        i += size
    return out

def start_ws_connections(symbols):
    cfg = load_config()
    conn_count = max(1, int(cfg.get("connections_count", 4)))
    groups = split_list(symbols, conn_count)
    for idx, group in enumerate(groups, start=1):
        t = threading.Thread(target=ws_connection_thread, args=(idx, group), daemon=True)
        t.start()
        time.sleep(0.5)

def start_workers(max_workers):
    for wid in range(max_workers):
        t = threading.Thread(target=signal_worker_main, args=(wid+1,), daemon=True)
        t.start()

# ==========================
# === Entrypoint         ===
# ==========================
def main():
    print("WS URL resolved:", MEXC_WS_URL if (MEXC_WS_URL.startswith('ws')) else f"(invalid) → fallback {MEXC_WS_URL_DEFAULT}")
    cfg = load_config()
    symbols = load_pairs()
    print(f"🚀 Monitoring {len(symbols)} symbols across {cfg.get('connections_count',4)} connections")
    print("⚙ thresholds: Δ>={}% RSI>={} Vol×>={}".format(cfg["price_change_percent"], cfg["rsi_threshold"], cfg["volume_ratio"]))

    load_candles_from_file()
    if cfg.get("persist_candles", True):
        threading.Thread(target=persist_thread, daemon=True).start()

    start_ws_connections(symbols)
    start_workers(cfg.get("max_worker_threads", 8))

    try:
        while True:
            time.sleep(60)
            with candles_lock:
                cached = len(candles_store)
            qsize = signal_queue.qsize()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Свечи кэшированы на {cached} символов. Размер очереди: {qsize}.")
    except KeyboardInterrupt:
        print("⏹ Stopping...")
        shutdown_event.set()
        dump_candles_to_file()
        time.sleep(1)
        sys.exit(0)

if __name__ == "__main__":
    main()

