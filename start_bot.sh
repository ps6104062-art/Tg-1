#!/bin/bash
echo "📦 Устанавливаю зависимости..."
pip install aiogram telethon --quiet

echo "🚀 Запускаю TG Manager Bot..."
python tg_bot.py
