
# MEXC Pump Scanner - Ready for Railway

This repository contains a ready-to-deploy MEXC futures pump scanner (multi-WS, signal detection, charting, Telegram alerts).

## Files
- bot.py - main bot
- pairs.json - list of 592 symbols (common + placeholders)
- config.json - settings
- requirements.txt - Python dependencies
- Procfile - Railway process definition

## How to deploy on Railway (step-by-step)
1. Create a GitHub repo and push these files.
2. Create an account on Railway.app and connect GitHub.
3. New Project -> Deploy from GitHub -> select this repo.
4. In Railway project settings -> Variables add:
   - BOT_TOKEN = your Telegram bot token
   - CHAT_ID = your telegram chat id (group or channel)
5. Deploy, check logs. Adjust config.json if needed.
