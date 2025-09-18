#!/usr/bin/env python3
# === project2_realtime_pumpbot.py ===
# Python 3.10+ / Replit-ready
# Мета: моніторинг усіх USDT пар, відправка сигналів у Telegram

import pandas as pd
import requests
import ta
from datetime import datetime, timedelta, timezone
import time
import json
import logging
import os
import numpy as np
# from telethon import TelegramClient  # Відключено для стабільності
import matplotlib
matplotlib.use('Agg')  # Безголовий режим для серверного середовища
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import mplfinance as mpf
from io import BytesIO

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('pump_bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# === Telegram API дані ===
# API_ID = "22404778"  # Видалено для стабільності
# API_HASH = "1982c980613223637de5a0785cf07e11"  # Видалено для стабільності  
BOT_TOKEN = "8434851880:AAFBRzKiSrq1x4Ta1UwO8TMUcLEr_nl5DpE"
CHAT_ID = "-1002463181604"

MEXC_BASE_URL = "https://api.mexc.com"
CHART_FILE = "signal_chart.png"

def load_cached_pairs():
    """Завантаження збережених пар з файлу"""
    cache_file = "mexc_futures_pairs.json"
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                pairs = data.get('pairs', [])
                cached_time = data.get('cached_time', '')
                logger.info(f"📁 Завантажено {len(pairs)} пар з кешу (збережено: {cached_time})")
                return pairs
    except Exception as e:
        logger.error(f"Помилка завантаження кешу: {e}")
    return []

def save_pairs_to_cache(pairs):
    """Збереження пар у файл"""
    cache_file = "mexc_futures_pairs.json"
    try:
        data = {
            'pairs': pairs,
            'cached_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
            'total_count': len(pairs)
        }
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Збережено {len(pairs)} пар у кеш файл")
        return True
    except Exception as e:
        logger.error(f"Помилка збереження кешу: {e}")
        return False

def get_mexc_futures_pairs():
    """Отримання кастомних USDT пар для сканування"""
    
    # ПРІОРИТЕТ #1: Відфільтрований список користувача
    if os.path.exists('mexc_futures_filtered_symbols.json'):
        try:
            with open('mexc_futures_filtered_symbols.json', 'r', encoding='utf-8') as f:
                filtered_data = json.load(f)
                filtered_pairs = filtered_data.get('pairs', [])
                if filtered_pairs:
                    logger.info(f"⭐ ВИКОРИСТОВУЄМО ВІДФІЛЬТРОВАНИЙ СПИСОК КОРИСТУВАЧА: {len(filtered_pairs)} пар")
                    logger.info(f"🎯 {filtered_data.get('note', 'Відфільтровані пари користувача')}")
                    return filtered_pairs
        except Exception as e:
            logger.error(f"Помилка читання відфільтрованого списку: {e}")
    
    # Спочатку перевіряємо очищений список
    if os.path.exists('clean_pairs_list.json'):
        try:
            with open('clean_pairs_list.json', 'r', encoding='utf-8') as f:
                clean_data = json.load(f)
                clean_pairs = clean_data.get('pairs', [])
                if clean_pairs and len(clean_pairs) > 500:
                    logger.info(f"✨ Використовуємо очищений список: {len(clean_pairs)} пар")
                    logger.info(f"🎯 {clean_data.get('description', 'Очищені пари')}")
                    logger.info(f"🗑️ Видалено {clean_data.get('removed_pairs', 0)} проблемних пар")
                    return clean_pairs
        except Exception as e:
            logger.error(f"Помилка читання очищеного списку: {e}")
    
    # Резерв - кастомний список
    if os.path.exists('custom_pairs_list.json'):
        try:
            with open('custom_pairs_list.json', 'r', encoding='utf-8') as f:
                custom_data = json.load(f)
                custom_pairs = custom_data.get('pairs', [])
                if custom_pairs and len(custom_pairs) > 600:
                    logger.info(f"📋 Використовуємо кастомний список: {len(custom_pairs)} пар")
                    logger.info(f"🎯 {custom_data.get('description', 'Кастомні пари')}")
                    return custom_pairs
        except Exception as e:
            logger.error(f"Помилка читання кастомного списку: {e}")
    
    # Резерв - спробуємо завантажити з основного кешу
    cached_pairs = load_cached_pairs()
    if cached_pairs and len(cached_pairs) > 700:  # Якщо є достатньо пар в кеші
        logger.info(f"✅ Використовуємо збережені пари: {len(cached_pairs)} шт.")
        return cached_pairs
    
    # Якщо кешу немає або він неповний, завантажуємо з API
    logger.info("🔄 Завантажуємо нові пари з MEXC API...")
    url = "https://contract.mexc.com/api/v1/contract/detail"
    
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if data.get("success", False) and "data" in data:
            contracts = data["data"]
            usdt_perpetual_pairs = []
            
            for contract in contracts:
                symbol = contract.get("symbol", "")
                quote_coin = contract.get("quoteCoin", "")
                display_name = contract.get("displayNameEn", "")
                
                # Фільтруємо лише USDT-базовані perpetual пари
                if quote_coin == "USDT" and "PERPETUAL" in display_name:
                    # Конвертуємо формат для спот API: BTC_USDT -> BTCUSDT
                    spot_symbol = symbol.replace("_", "")
                    usdt_perpetual_pairs.append(spot_symbol)

            logger.info(f"🔹 Отримано {len(usdt_perpetual_pairs)} ф'ючерсних пар з API")
            
            # Зберігаємо в кеш
            if len(usdt_perpetual_pairs) > 700:
                save_pairs_to_cache(usdt_perpetual_pairs)
                return usdt_perpetual_pairs
            else:
                logger.warning("Замало пар отримано з API, використовуємо кеш або спот пари")
                return cached_pairs if cached_pairs else get_usdt_pairs()
                
        else:
            logger.error(f"❌ API не повернув успішну відповідь: {data}")
            return cached_pairs if cached_pairs else []

    except Exception as e:
        logger.error(f"⚠️ Помилка при запиті до ф'ючерсного API: {e}")
        # Використовуємо кеш або спот пари як резерв
        if cached_pairs:
            logger.info("Використовуємо збережені пари через помилку API")
            return cached_pairs
        else:
            logger.info("Використовуємо спот пари як резервний варіант")
            return get_usdt_pairs()

# === Завантаження оптимальних порогів з Проекту 1 ===
def load_analyzed_signals():
    """Завантаження налаштувань з веб інтерфейсу (bot_config.json)"""
    try:
        # Спочатку перевіряємо конфігурацію з веб інтерфейсу
        if os.path.exists("bot_config.json"):
            with open("bot_config.json", 'r', encoding='utf-8') as f:
                config = json.load(f)
                
                RSI_THRESHOLD = config.get('rsi_threshold', 50.0)
                PRICE_CHANGE_THRESHOLD = config.get('price_change', 5.0)
                VOLUME_RATIO_THRESHOLD = config.get('volume_ratio', 1.5)
                
                logger.info(f"📋 Завантажено налаштування з веб інтерфейсу:")
                logger.info(f"   RSI >= {RSI_THRESHOLD}")
                logger.info(f"   Зміна ціни >= {PRICE_CHANGE_THRESHOLD}%")
                logger.info(f"   Об'єм >= {VOLUME_RATIO_THRESHOLD}x")
                logger.info(f"   Збережено: {config.get('saved_at', 'невідомо')}")
                
                return RSI_THRESHOLD, PRICE_CHANGE_THRESHOLD, VOLUME_RATIO_THRESHOLD
        
        # Резервний варіант - читаємо з CSV файлу
        elif os.path.exists("berloga_trade_messages.csv"):
            df_analyzed = pd.read_csv("berloga_trade_messages.csv")
            if 'rsi' not in df_analyzed.columns:
                df_analyzed['rsi'] = 50.0
            if 'price_change_%' not in df_analyzed.columns:
                df_analyzed['price_change_%'] = 5.0
            if 'volume_ratio' not in df_analyzed.columns:
                df_analyzed['volume_ratio'] = 1.5
        
            RSI_THRESHOLD = df_analyzed['rsi'].median()
            PRICE_CHANGE_THRESHOLD = df_analyzed['price_change_%'].median()
            VOLUME_RATIO_THRESHOLD = df_analyzed['volume_ratio'].median()
            
            logger.info(f"📊 Завантажено пороги з CSV: RSI>{RSI_THRESHOLD}, Δ%>{PRICE_CHANGE_THRESHOLD}, VolRatio>{VOLUME_RATIO_THRESHOLD}")
            return RSI_THRESHOLD, PRICE_CHANGE_THRESHOLD, VOLUME_RATIO_THRESHOLD
        
        # Дефолтні налаштування
        else:
            logger.warning("⚙️ Використовуємо дефолтні налаштування")
            return 50.0, 5.0, 1.5
        
    except Exception as e:
        logger.warning(f"❌ Помилка завантаження налаштувань: {e}")
        logger.info("⚙️ Використовуємо дефолтні налаштування")
        return 50.0, 5.0, 1.5

# === Функції ===
def send_telegram_simple(text):
    """Проста відправка повідомлення в Telegram через Bot API (стабільно)"""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("✅ Повідомлення відправлено в Telegram канал")
        return True
    except Exception as e:
        logger.error(f"❌ Помилка відправки в Telegram: {e}")
        return False

def send_telegram_message_sync(text):
    """Синхронна відправка повідомлень (стабільна версія)"""
    return send_telegram_simple(text)

def load_bybit_pairs():
    """Завантажити список ByBit пар для перевірки"""
    try:
        if os.path.exists("bybit_pairs.json"):
            with open("bybit_pairs.json", 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('pairs', []))
        return set()
    except Exception as e:
        logger.error(f"Помилка завантаження ByBit пар: {e}")
        return set()

def load_weex_pairs():
    """Завантажити список WEEX пар для перевірки"""
    try:
        if os.path.exists("weex_pairs.json"):
            with open("weex_pairs.json", 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('pairs', []))
        return set()
    except Exception as e:
        logger.error(f"Помилка завантаження WEEX пар: {e}")
        return set()

def is_on_bybit_local(pair):
    """Перевірити чи є пара у локальному списку ByBit пар"""
    bybit_pairs = load_bybit_pairs()
    return pair.upper() in bybit_pairs

def check_exchanges_for_pair(pair):
    """Перевірити на яких біржах торгується пара"""
    exchanges = []
    exchanges.append(" Ⓜ️ MEXC")  # Завжди є, оскільки ми скануємо тільки MEXC пари
    
    # Перевіряємо ByBit з червоною буквою B (виправлено)
    if is_on_bybit_local(pair):
        exchanges.append("🅱️ ByBit")  # Червона B в квадраті
    
    # Перевіряємо WEEX з оранжевою буквою W
    weex_pairs = load_weex_pairs()
    if pair in weex_pairs:
        exchanges.append("🟠 W WEEX")
    
    return exchanges

def create_chart(pair, pump_data):
    """Створити графік для памп сигналу"""
    try:
        # Спочатку пробуємо отримати 5-хвилинні дані для стабільності
        df_chart = get_historical_klines(pair, "5m", 200)  # 200 5-хвилинних свічок (16+ годин)
        
        if df_chart.empty or len(df_chart) < 20:
            # Якщо 5m не спрацьовує, пробуємо 15m
            logger.info(f"Пробуємо 15-хвилинні дані для {pair}")
            df_chart = get_historical_klines(pair, "15m", 100)  # 100 15-хвилинних свічок (25 годин)
            
        if df_chart.empty or len(df_chart) < 14:  # Мінімум для RSI
            logger.warning(f"Недостатньо даних для створення графіка {pair}")
            return None
            
        # Підготовка даних для mplfinance
        df_chart = df_chart.set_index('time_open')
        df_chart = df_chart.rename(columns={
            'open': 'Open',
            'high': 'High', 
            'low': 'Low',
            'close': 'Close',
            'volume': 'Volume'
        })
        
        # Обчисляємо технічні індикатори
        rsi_indicator = ta.momentum.RSIIndicator(df_chart['Close'], window=14)
        rsi_values = rsi_indicator.rsi()
        
        macd_indicator = ta.trend.MACD(df_chart['Close'])
        macd_line = macd_indicator.macd()
        macd_signal = macd_indicator.macd_signal()
        
        # Налаштування стилю графіка (як в еталонному прикладі)
        style = mpf.make_mpf_style(
            base_mpl_style='seaborn-v0_8-whitegrid',  # Світлий фон як на скріншоті
            marketcolors=mpf.make_marketcolors(
                up='#26a69a', down='#ef5350',  # Зелені/червоні свічки
                edge='inherit',
                wick={'up': '#26a69a', 'down': '#ef5350'},
                volume='#42a5f5'  # Синяватий обсяг
            ),
            gridstyle='-',
            gridcolor='#e0e0e0',  # Світла сітка
            facecolor='white'  # Білий фон
        )
        
        # Додаткові індикатори (тільки RSI як в еталоні)
        apds = [
            mpf.make_addplot(rsi_values, panel=1, color='#ff6b6b', width=2, ylabel='RSI')
        ]
        
        # Створення графіка (як в еталонному прикладі)
        fig, axes = mpf.plot(
            df_chart,
            type='candle',
            style=style,
            addplot=apds,
            volume=True,
            title=f'#{pair.replace("USDT", "")} Pump: +{pump_data["price_change"]}%',
            ylabel='Price',
            figratio=(12, 8),
            figscale=1.0,
            returnfig=True,
            tight_layout=True
        )
        
        # Додаємо інформацію на графік (як в еталоні)
        # Не додаємо suptitle, оскільки в еталоні тільки title
        
        # Зберігаємо графік
        fig.savefig(CHART_FILE, facecolor='white', edgecolor='none', 
                   dpi=200, bbox_inches='tight', pad_inches=0.2)
        plt.close(fig)
        
        return CHART_FILE
        
    except Exception as e:
        logger.error(f"Помилка створення графіка для {pair}: {e}")
        return None

def send_telegram_photo(photo_path, caption):
    """Відправка фото з підписом в Telegram"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as photo:
            files = {"photo": photo}
            data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            response = requests.post(url, files=files, data=data, timeout=20)
            response.raise_for_status()
            logger.info("✅ Telegram фото відправлено успішно")
            return True
    except Exception as e:
        logger.error(f"Помилка відправки фото: {e}")
        return False

def get_usdt_pairs():
    """Отримання всіх USDT пар з спот торгівлі (резервна функція)"""
    try:
        url = f"{MEXC_BASE_URL}/api/v3/exchangeInfo"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        pairs = [s['symbol'] for s in data['symbols'] if s['symbol'].endswith("USDT")]
        logger.info(f"Завантажено {len(pairs)} спот USDT пар")
        return pairs
    except Exception as e:
        logger.error(f"Помилка завантаження спот пар: {e}")
        return []

def get_historical_klines(symbol, interval, limit):
    """Отримання історичних даних (спочатку спот, потім ф'ючерсні)"""
    # Спочатку пробуємо спот API
    df = get_spot_klines(symbol, interval, limit)
    
    # Якщо спот не працює, пробуємо ф'ючерсний API
    if df.empty:
        df = get_futures_klines(symbol, interval, limit)
    
    return df

def get_spot_klines(symbol, interval, limit):
    """Отримання спот даних"""
    try:
        url = f"{MEXC_BASE_URL}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        if not isinstance(data, list) or len(data) == 0:
            return pd.DataFrame()
            
        # MEXC API повертає різну кількість колонок залежно від пари
        # Базові колонки: timestamp, open, high, low, close, volume
        num_cols = len(data[0]) if len(data) > 0 else 12
        base_cols = ["time_open", "open", "high", "low", "close", "volume"]
        extra_cols = [f"_extra_{i}" for i in range(6, num_cols)]
        all_cols = base_cols + extra_cols
        
        df = pd.DataFrame(data, columns=all_cols)
        df["time_open"] = pd.to_datetime(df["time_open"], unit='ms', utc=True)
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
        return df[["time_open", "open", "high", "low", "close", "volume"]]
        
    except Exception as e:
        # Не логуємо как warning - це нормально для деяких пар
        return pd.DataFrame()

def get_futures_klines(symbol, interval, limit):
    """Отримання ф'ючерсних даних з MEXC"""
    try:
        # Конвертуємо інтервал: 1m -> Min1, 5m -> Min5
        interval_map = {'1m': 'Min1', '5m': 'Min5', '15m': 'Min15', '1h': 'Hour1', '4h': 'Hour4', '1d': 'Day1'}
        mexc_interval = interval_map.get(interval, 'Min1')
        
        # Конвертуємо символ: BTCUSDT -> BTC_USDT
        futures_symbol = symbol.replace('USDT', '_USDT') if '_' not in symbol else symbol
        
        url = f"https://contract.mexc.com/api/v1/contract/kline/{futures_symbol}"
        params = {"interval": mexc_interval, "limit": limit}
        
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        if not data.get("data") or not isinstance(data["data"], list):
            return pd.DataFrame()
        
        klines = data["data"]
        
        # Конвертуємо в DataFrame
        df = pd.DataFrame(klines, columns=['time_open', 'open', 'high', 'low', 'close', 'volume'])
        df["time_open"] = pd.to_datetime(df["time_open"], unit='ms', utc=True)
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
        
        return df
        
    except Exception as e:
        logger.warning(f"Помилка завантаження ф'ючерсних даних для {symbol}: {e}")
        return pd.DataFrame()

def analyze_pair(pair, rsi_threshold, price_change_threshold, volume_ratio_threshold):
    """Аналіз пари на памп сигнал"""
    try:
        # Невелика затримка для запобігання rate limiting
        time.sleep(0.1)
        df = get_historical_klines(pair, "1m", 50)
        if df.empty or len(df) < 14:  # Мінімум для RSI
            return None

        # Розрахунок RSI
        rsi_indicator = ta.momentum.RSIIndicator(df['close'], window=14)
        rsi_series = rsi_indicator.rsi()
        rsi = rsi_series.iloc[-1]
        
        # Розрахунок зміни ціни
        close_last = df['close'].iloc[-1]
        close_first = df['close'].iloc[0]
        price_change = ((close_last - close_first) / close_first) * 100
        
        # Розрахунок коефіцієнту об'єму
        volume_last = df['volume'].iloc[-1]
        volume_mean = df['volume'].mean()
        vol_ratio = volume_last / volume_mean if volume_mean > 0 else 0

        # Логування для дебагу (кожна 100-та пара)
        if pair.endswith('00USDT') or pair.endswith('50USDT'):
            logger.info(f"🔍 {pair}: RSI={rsi:.1f} (>={rsi_threshold}), Ціна={price_change:.2f}% (>={price_change_threshold}), Об'єм={vol_ratio:.1f}x (>={volume_ratio_threshold})")
        
        # Перевірка на памп сигнал
        if rsi > rsi_threshold and price_change > price_change_threshold and vol_ratio > volume_ratio_threshold:
            return {
                'pair': pair,
                'rsi': round(rsi, 2),
                'price_change': round(price_change, 2),
                'vol_ratio': round(vol_ratio, 2),
                'current_price': close_last,
                'timestamp': datetime.now(timezone.utc)
            }
        return None
        
    except Exception as e:
        logger.warning(f"Помилка аналізу {pair}: {e}")
        return None

def format_pump_message_with_exchanges(pump_data, exchanges):
    """Новий формат повідомлення згідно з вимогами користувача"""
    exchanges_text = " ".join(exchanges)  # Без | розділювачів
    symbol = pump_data['pair'].replace('USDT', '')
    
    # Новий формат згідно з прикладом
    return f"""#{symbol} {pump_data['pair']}. 🟢 Pump: {pump_data['price_change']:.2f}%
🎯 RSI: {pump_data['rsi']:.1f}%
🎯 Биржи: {exchanges_text}
✉️ Чат: https://t.me/+y5PWzg0NwX83MGUy"""

def format_pump_message(pump_data):
    """Форматування повідомлення про памп на російській мові"""
    return f"""🚀 *ПАМП СИГНАЛ!*

*Пара:* `{pump_data['pair']}`
*RSI:* {pump_data['rsi']}
*Изменение цены:* +{pump_data['price_change']}%
*Объём:* x{pump_data['vol_ratio']}
*Цена:* {pump_data['current_price']:.8f} USDT

⏰ {pump_data['timestamp'].strftime('%H:%M:%S UTC')}
🤖 Автоматический мониторинг MEXC"""

# === Основний цикл ===
def main():
    """Основна функція бота"""
    logger.info("🚀 Запуск Супер Памп Бота (повна версія)")
    
    # Тестова відправка при запуску відключена за вимогою користувача
    # test_message = "🤖 Бот запущен и готов к мониторингу MEXC пар!"
    # if send_telegram_simple(test_message):
    #     logger.info("✅ Тестове повідомлення відправлено в канал")
    # else:
    #     logger.warning("⚠️ Проблема з відправкою в Telegram")
    
    # Завантажуємо пороги
    RSI_THRESHOLD, PRICE_CHANGE_THRESHOLD, VOLUME_RATIO_THRESHOLD = load_analyzed_signals()
    
    # Отримуємо список пар для моніторингу
    pairs = get_mexc_futures_pairs()
    if not pairs:
        logger.warning("Не вдалося завантажити ф'ючерсні пари, використовуємо спот пари")
        pairs = get_usdt_pairs()
    
    if not pairs:
        logger.error("Не вдалося завантажити жодні пари для моніторингу")
        return
        
    logger.info(f"Моніторимо {len(pairs)} USDT пар")
    
    # Стартове повідомлення вимкнено для зменшення спаму
    # start_msg = f"""🟢 *СУПЕР ПАМП БОТ ЗАПУЩЕН*..."""
    # send_telegram_message(start_msg)
    
    cycle_count = 0
    
    while True:
        try:
            cycle_count += 1
            start_time = datetime.now(timezone.utc)
            logger.info(f"🔄 Цикл #{cycle_count} - початок сканування о {start_time.strftime('%H:%M:%S UTC')}")
            
            # Перечитуємо налаштування з веб інтерфейсу перед кожним циклом
            current_rsi, current_price, current_volume = load_analyzed_signals()
            
            # Перевіряємо чи змінились налаштування
            if (current_rsi != RSI_THRESHOLD or current_price != PRICE_CHANGE_THRESHOLD or 
                current_volume != VOLUME_RATIO_THRESHOLD):
                logger.info(f"⚙️ ОНОВЛЕНО НАЛАШТУВАННЯ:")
                logger.info(f"   RSI: {RSI_THRESHOLD} → {current_rsi}")
                logger.info(f"   Зміна ціни: {PRICE_CHANGE_THRESHOLD}% → {current_price}%")
                logger.info(f"   Об'єм: {VOLUME_RATIO_THRESHOLD}x → {current_volume}x")
                
                RSI_THRESHOLD, PRICE_CHANGE_THRESHOLD, VOLUME_RATIO_THRESHOLD = current_rsi, current_price, current_volume
            
            pump_signals = []
            
            for i, pair in enumerate(pairs):
                try:
                    pump_data = analyze_pair(pair, RSI_THRESHOLD, PRICE_CHANGE_THRESHOLD, VOLUME_RATIO_THRESHOLD)
                    
                    if pump_data:
                        pump_signals.append(pump_data)
                        
                        # Перевіряємо на яких біржах торгується пара
                        exchanges = check_exchanges_for_pair(pair)
                        
                        # Створюємо графік
                        chart_file = create_chart(pair, pump_data)
                        
                        if chart_file and os.path.exists(chart_file):
                            # Відправляємо повідомлення з графіком
                            caption = format_pump_message_with_exchanges(pump_data, exchanges)
                            if send_telegram_photo(chart_file, caption):
                                logger.info(f"📤 Відправлено памп сигнал з графіком: {pair}")
                            else:
                                # Якщо не вдалося відправити фото, відправляємо текст
                                msg = format_pump_message(pump_data)
                                if send_telegram_message_sync(msg):
                                    logger.info(f"📤 Відправлено текстовий памп сигнал: {pair}")
                        else:
                            # Якщо не вдалося створити графік, відправляємо текст з інфо про біржі
                            msg = format_pump_message_with_exchanges(pump_data, exchanges)
                            if send_telegram_message_sync(msg):
                                logger.info(f"📤 Відправлено памп сигнал: {pair}")
                        
                        # Невелика пауза між сигналами
                        time.sleep(3)
                    
                    # Прогрес кожні 50 пар
                    if (i + 1) % 50 == 0:
                        logger.info(f"📈 Проаналізовано {i + 1}/{len(pairs)} пар...")
                        # Додаткова пауза кожні 50 пар для rate limiting
                        time.sleep(1)
                    
                    # Невелика затримка між запитами
                    time.sleep(0.1)
                    
                except Exception as e:
                    logger.warning(f"Помилка при аналізі {pair}: {e}")
                    continue
            
            end_time = datetime.now(timezone.utc)
            duration = (end_time - start_time).total_seconds()
            
            logger.info(f"✅ Цикл #{cycle_count} завершено за {duration:.1f}с. Знайдено {len(pump_signals)} сигналів")
            
            # Підсумкові повідомлення вимкнено для зменшення спаму
            # if cycle_count % 10 == 0:
            #     summary_msg = f"""📊 *ОТЧЁТ О РАБОТЕ*..."""
            #     send_telegram_message(summary_msg)
            
            logger.info("⏳ Пауза 1 хвилина до наступного сканування...")
            time.sleep(60)  # 1 хвилина пауза
            
        except KeyboardInterrupt:
            logger.info("🛑 Бот зупинено користувачем")
            # stop_msg вимкнено для зменшення спаму
            # send_telegram_message(stop_msg)
            break
            
        except Exception as e:
            logger.error(f"💥 Критична помилка в циклі #{cycle_count}: {e}")
            # error_msg вимкнено для зменшення спаму
            # send_telegram_message(error_msg)
            time.sleep(60)  # Пауза при помилці
            continue

def auto_restart_bot():
    """Автоматичний перезапуск бота при помилках та відновленні інтернету"""
    max_retries = 10
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            logger.info(f"🔄 Спроба запуску #{retry_count + 1}")
            main()
            break  # Якщо main() завершився без помилки
            
        except KeyboardInterrupt:
            logger.info("⏹️ Бот зупинено користувачем")
            break
            
        except Exception as e:
            retry_count += 1
            wait_time = min(60 * retry_count, 300)  # Максимум 5 хвилин
            
            logger.error(f"❌ Помилка #{retry_count}: {str(e)}")
            logger.info(f"⏳ Перезапуск через {wait_time} секунд...")
            
            if retry_count >= max_retries:
                logger.error("🛑 Досягнуто максимум спроб перезапуску")
                break
                
            time.sleep(wait_time)
    
    logger.info("🔚 Робота бота завершена")

if __name__ == "__main__":
    try:
        # Перевіряємо наявність секретів
        if not BOT_TOKEN or not CHAT_ID:
            logger.error("❌ Не налаштовані BOT_TOKEN або CHAT_ID")
            logger.info("💡 Додайте їх в Secrets панелі Replit")
            exit(1)
            
        auto_restart_bot()
        
    except Exception as e:
        logger.error(f"❌ Критична помилка запуску: {str(e)}")
        exit(1)