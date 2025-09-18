# 🚀 Налаштування Памп-Бота на Railway

## 📦 Файли в архіві
- `pump_bot_full.py` - Головний файл памп-бота
- `bot_config.json` - Конфігурація з параметрами сканування
- `mexc_futures_filtered_symbols.json` - 715 пар MEXC для сканування
- `bybit_pairs.json` - 525 пар Bybit для перевірки
- `weex_pairs.json` - пари WEEX для перевірки  
- `pyproject.toml` - Python залежності
- `Procfile` - Команда запуску для Railway
- `.streamlit/config.toml` - Конфігурація Streamlit

## 🔧 Крок за кроком налаштування на Railway

### 1️⃣ Створення проекту
1. Зайдіть на [railway.app](https://railway.app)
2. Натисніть "Start a New Project"
3. Оберіть "Deploy from GitHub repo" або "Empty Project"

### 2️⃣ Завантаження файлів
1. Розархівуйте `railway-pump-bot.zip`
2. Завантажте всі файли до Railway проекту
3. Переконайтесь що всі файли присутні

### 3️⃣ Налаштування змінних середовища
У Railway додайте ці змінні середовища (Environment Variables):

```bash
BOT_TOKEN=ВАШ_ТОКЕН_БОТА
CHAT_ID=-1002463181604
PYTHONPATH=/app
PORT=8080
```

### 4️⃣ Запуск
Railway автоматично виявить `Procfile` та запустить бота командою:
```bash
python pump_bot_full.py
```

## ⚙️ Параметри бота (в bot_config.json)
```json
{
  "rsi_threshold": 70.0,
  "price_change_threshold": 6.0,
  "volume_ratio_threshold": 2.0
}
```

## 🔄 Автоматична робота
Бот буде:
- ✅ Сканувати 715 пар MEXC кожні 1 хвилину
- ✅ Відсилати сигнали в Telegram канал російською мовою
- ✅ Генерувати графіки з RSI, MACD та свічками
- ✅ Показувати на яких біржах доступна пара
- ✅ Автоматично перезапускатися при помилках

## 📊 Моніторинг
У Railway перегляньте логи для відстеження роботи:
```
🔄 Цикл #1 - початок сканування
📈 Проаналізовано 50/715 пар...
✅ Цикл завершено. Знайдено 0 сигналів
```

## 🆘 Підтримка
- Бот використовує ті самі налаштування що й на Replit
- Всі данні пар оновлені станом на 06.09.2025
- Токен бота: `8434851880:AAFBRzKiSrq1x4Ta1UwO8TMUcLEr_nl5DpE`
- ID каналу: `-1002463181604`