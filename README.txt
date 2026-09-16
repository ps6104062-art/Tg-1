TG Manager Pro Bot
==================

Установка и запуск:
  1. Открой tg_bot.py и вставь свои данные в начале файла:
       BOT_TOKEN  — от @BotFather
       API_ID     — от my.telegram.org
       API_HASH   — от my.telegram.org

  2. Установи зависимости:
       pip install -r requirements.txt

  3. Запусти:
       python tg_bot.py
     или
       bash start_bot.sh

Возможности:
  - Добавить аккаунт по номеру телефона
  - Загрузить готовую session string
  - Список аккаунтов с online/offline статусом
  - Статистика (кол-во диалогов)
  - Экспорт сессии файлом прямо в чат
  - Очистить все диалоги (с подтверждением)
  - Удалить аккаунт
  - Автоперехват кодов авторизации Telegram
