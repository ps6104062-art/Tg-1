"""
TG Manager Pro — Telegram Bot
Управление Telegram-аккаунтами через бота (aiogram 3 + Telethon)

Установка: pip install aiogram telethon
Запуск:    python tg_bot.py
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
)
import re

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError, PhoneCodeExpiredError, SessionPasswordNeededError
)
from telethon.sessions import StringSession

# ─── Конфиг ──────────────────────────────────────────────────────────────────
BOT_TOKEN    = "ВАШ_БОТ_ТОКЕН"   # ← от @BotFather
API_ID       = 12345              # ← от my.telegram.org
API_HASH     = "ВАШ_API_HASH"    # ← от my.telegram.org
DB_PATH      = "accounts.db"
SESSIONS_DIR = "sessions"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

os.makedirs(SESSIONS_DIR, exist_ok=True)

# ─── База данных ──────────────────────────────────────────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
    CREATE TABLE IF NOT EXISTS accounts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id   INTEGER NOT NULL,
        phone      TEXT    NOT NULL,
        session    TEXT,
        username   TEXT,
        full_name  TEXT,
        created_at TEXT    DEFAULT (datetime('now')),
        UNIQUE(owner_id, phone)
    );
""")
db.commit()

# ─── In-memory клиенты {owner_id: {phone: TelethonClient}} ───────────────────
tg_clients: dict[int, dict[str, TelegramClient]] = {}

# ─── FSM States ──────────────────────────────────────────────────────────────
class AddPhone(StatesGroup):
    waiting_phone    = State()
    waiting_code     = State()
    waiting_password = State()

class LoadSession(StatesGroup):
    waiting_phone   = State()
    waiting_session = State()


# ─── DB helpers ──────────────────────────────────────────────────────────────
def db_save(owner_id: int, phone: str, client: TelegramClient,
            username: str = "", full_name: str = ""):
    session_str = client.session.save()
    db.execute(
        """INSERT INTO accounts (owner_id, phone, session, username, full_name)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(owner_id, phone) DO UPDATE SET
               session=excluded.session,
               username=excluded.username,
               full_name=excluded.full_name""",
        (owner_id, phone, session_str, username, full_name)
    )
    db.commit()

def db_list(owner_id: int) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM accounts WHERE owner_id=? ORDER BY created_at DESC",
        (owner_id,)
    ).fetchall()

def db_get(owner_id: int, phone: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM accounts WHERE owner_id=? AND phone=?",
        (owner_id, phone)
    ).fetchone()

def db_delete(owner_id: int, phone: str):
    db.execute("DELETE FROM accounts WHERE owner_id=? AND phone=?", (owner_id, phone))
    db.commit()


# ─── Telethon helpers ─────────────────────────────────────────────────────────
def get_client(owner_id: int, phone: str) -> TelegramClient | None:
    return tg_clients.get(owner_id, {}).get(phone)

def set_client(owner_id: int, phone: str, client: TelegramClient):
    tg_clients.setdefault(owner_id, {})[phone] = client

def del_client(owner_id: int, phone: str):
    tg_clients.get(owner_id, {}).pop(phone, None)

# ─── Слушатель кодов ──────────────────────────────────────────────────────────
# Паттерн: "Код: 12345" / "Login code: 12345" / "your code: 12345" и т.п.
CODE_RE = re.compile(r'\b(\d{5,6})\b')

def attach_code_listener(client: TelegramClient, owner_id: int, phone: str):
    """Вешает на клиент обработчик входящих от сервисного аккаунта Telegram.
    Как только приходит сообщение с кодом — пересылает его владельцу в бота."""

    @client.on(events.NewMessage(from_users=[777000, 42777]))
    async def _handler(event):
        text = event.raw_text or ""
        match = CODE_RE.search(text)
        code  = match.group(1) if match else None

        try:
            if code:
                await bot.send_message(
                    owner_id,
                    f"🔐 <b>Код авторизации</b>\n\n"
                    f"Аккаунт: <code>{phone}</code>\n"
                    f"Код: <code>{code}</code>\n\n"
                    f"<i>Полное сообщение:</i>\n{text}",
                    parse_mode="HTML"
                )
            else:
                # Пересылаем всё сообщение от Telegram если кода нет
                await bot.send_message(
                    owner_id,
                    f"📨 <b>Сообщение от Telegram</b>\n"
                    f"Аккаунт: <code>{phone}</code>\n\n"
                    f"{text}",
                    parse_mode="HTML"
                )
        except Exception as e:
            log.error(f"[listener:{phone}] ошибка отправки: {e}")

async def make_client(session_str: str = "") -> TelegramClient:
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    return client

async def get_profile(client: TelegramClient) -> tuple[str, str]:
    me = await client.get_me()
    if not me:
        return "", ""
    username  = me.username or ""
    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    return username, full_name


# ─── Keyboards ────────────────────────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Добавить по номеру",  callback_data="add_phone")],
        [InlineKeyboardButton(text="📂 Загрузить сессию",    callback_data="load_session")],
        [InlineKeyboardButton(text="📋 Мои аккаунты",        callback_data="list_accounts")],
    ])

def kb_account(phone: str):
    p = phone.replace("+", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика",  callback_data=f"stats:{p}"),
            InlineKeyboardButton(text="📦 Экспорт",     callback_data=f"export:{p}"),
        ],
        [InlineKeyboardButton(text="🧹 Очистить диалоги", callback_data=f"clean:{p}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт",  callback_data=f"delete:{p}")],
        [InlineKeyboardButton(text="◀️ Назад",             callback_data="list_accounts")],
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu")]
    ])

def kb_confirm_clean(phone: str):
    p = phone.replace("+", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить всё", callback_data=f"clean_confirm:{p}"),
            InlineKeyboardButton(text="❌ Отмена",          callback_data=f"account:{p}"),
        ]
    ])

def kb_confirm_delete(phone: str):
    p = phone.replace("+", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delete_confirm:{p}"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data=f"account:{p}"),
        ]
    ])

def kb_accounts_list(accounts: list[sqlite3.Row], owner_id: int):
    rows = []
    for acc in accounts:
        phone  = acc["phone"]
        p      = phone.replace("+", "")
        name   = acc["full_name"] or phone
        online = "🟢" if get_client(owner_id, phone) else "⚫️"
        rows.append([InlineKeyboardButton(
            text=f"{online} {name} ({phone})",
            callback_data=f"account:{p}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")]
    ])


# ─── Bot + Dispatcher ─────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


# ─── /start ───────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "👋 <b>TG Manager Pro</b>\n\n"
        "Управляй своими Telegram-аккаунтами прямо здесь.\n"
        "Выбери действие:",
        reply_markup=kb_main(),
        parse_mode="HTML"
    )

@dp.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("📌 Главное меню:", reply_markup=kb_main())


# ─── Главное меню (callback) ──────────────────────────────────────────────────
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "📌 <b>Главное меню</b>\nВыбери действие:",
        reply_markup=kb_main(), parse_mode="HTML"
    )

# ─── Список аккаунтов ─────────────────────────────────────────────────────────
@dp.callback_query(F.data == "list_accounts")
async def cb_list(cb: CallbackQuery):
    owner_id = cb.from_user.id
    accounts = db_list(owner_id)
    if not accounts:
        await cb.message.edit_text(
            "📭 <b>Аккаунтов нет</b>\n\nДобавь первый через меню.",
            reply_markup=kb_back(), parse_mode="HTML"
        )
        return
    await cb.message.edit_text(
        f"📋 <b>Твои аккаунты</b> ({len(accounts)} шт.)\n\nВыбери для управления:",
        reply_markup=kb_accounts_list(accounts, owner_id),
        parse_mode="HTML"
    )

# ─── Карточка аккаунта ────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("account:"))
async def cb_account(cb: CallbackQuery):
    owner_id = cb.from_user.id
    p        = cb.data.split(":", 1)[1]
    phone    = "+" + p
    row      = db_get(owner_id, phone)
    if not row:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return

    online   = "🟢 Online" if get_client(owner_id, phone) else "⚫️ Offline"
    name     = row["full_name"] or "—"
    uname    = f"@{row['username']}" if row["username"] else "—"
    added    = row["created_at"][:10]

    await cb.message.edit_text(
        f"<b>{name}</b> {uname}\n"
        f"📱 <code>{phone}</code>\n"
        f"Статус: {online}\n"
        f"Добавлен: {added}",
        reply_markup=kb_account(phone),
        parse_mode="HTML"
    )

# ─── Статистика ───────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("stats:"))
async def cb_stats(cb: CallbackQuery):
    owner_id = cb.from_user.id
    phone    = "+" + cb.data.split(":", 1)[1]
    client   = get_client(owner_id, phone)

    if not client:
        row = db_get(owner_id, phone)
        if not row or not row["session"]:
            await cb.answer("Нет активной сессии", show_alert=True)
            return
        client = await make_client(row["session"])
        set_client(owner_id, phone, client)

    await cb.answer("⏳ Считаю диалоги...")
    try:
        dialogs = await client.get_dialogs()
        await cb.message.edit_text(
            f"📊 <b>Статистика</b> <code>{phone}</code>\n\n"
            f"💬 Диалогов: <b>{len(dialogs)}</b>",
            reply_markup=kb_account(phone),
            parse_mode="HTML"
        )
    except Exception as e:
        await cb.message.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_account(phone))

# ─── Экспорт сессии ───────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("export:"))
async def cb_export(cb: CallbackQuery):
    owner_id = cb.from_user.id
    phone    = "+" + cb.data.split(":", 1)[1]
    row      = db_get(owner_id, phone)

    if not row or not row["session"]:
        await cb.answer("Нет сохранённой сессии", show_alert=True)
        return

    data = {
        "phone":       phone,
        "session":     row["session"],
        "username":    row["username"],
        "full_name":   row["full_name"],
        "exported_at": datetime.utcnow().isoformat()
    }

    path = os.path.join(SESSIONS_DIR, phone.lstrip("+"))
    os.makedirs(path, exist_ok=True)
    file_path = os.path.join(path, "session.json")
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    await cb.answer("✅ Сессия экспортирована")

    # Отправляем файл прямо в чат
    from aiogram.types import FSInputFile
    doc = FSInputFile(file_path, filename=f"{phone.lstrip('+')}_session.json")
    await cb.message.answer_document(
        doc,
        caption=f"📦 Сессия для <code>{phone}</code>",
        parse_mode="HTML"
    )

# ─── Очистка диалогов — подтверждение ────────────────────────────────────────
@dp.callback_query(F.data.startswith("clean:"))
async def cb_clean_confirm(cb: CallbackQuery):
    phone = "+" + cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        f"⚠️ <b>Подтверди очистку</b>\n\n"
        f"Все диалоги аккаунта <code>{phone}</code> будут удалены.\n"
        f"<b>Это действие необратимо!</b>",
        reply_markup=kb_confirm_clean(phone),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("clean_confirm:"))
async def cb_clean_do(cb: CallbackQuery):
    owner_id = cb.from_user.id
    phone    = "+" + cb.data.split(":", 1)[1]
    client   = get_client(owner_id, phone)

    if not client:
        row = db_get(owner_id, phone)
        if not row or not row["session"]:
            await cb.answer("Нет активной сессии", show_alert=True)
            return
        client = await make_client(row["session"])
        set_client(owner_id, phone, client)

    await cb.message.edit_text(f"⏳ Очищаю диалоги <code>{phone}</code>...", parse_mode="HTML")

    deleted = errors = 0
    try:
        async for dialog in client.iter_dialogs():
            try:
                await client.delete_dialog(dialog.id, revoke=True)
                deleted += 1
            except Exception:
                errors += 1
    except Exception as e:
        await cb.message.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_account(phone))
        return

    await cb.message.edit_text(
        f"✅ <b>Очистка завершена</b>\n\n"
        f"Аккаунт: <code>{phone}</code>\n"
        f"🗑 Удалено диалогов: <b>{deleted}</b>\n"
        f"⚠️ Ошибок: <b>{errors}</b>",
        reply_markup=kb_account(phone),
        parse_mode="HTML"
    )

# ─── Удаление аккаунта — подтверждение ───────────────────────────────────────
@dp.callback_query(F.data.startswith("delete:"))
async def cb_delete_confirm(cb: CallbackQuery):
    phone = "+" + cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        f"🗑 <b>Удалить аккаунт?</b>\n\n"
        f"<code>{phone}</code> будет удалён из базы.\n"
        f"Сессия будет отозвана.",
        reply_markup=kb_confirm_delete(phone),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("delete_confirm:"))
async def cb_delete_do(cb: CallbackQuery):
    owner_id = cb.from_user.id
    phone    = "+" + cb.data.split(":", 1)[1]
    client   = get_client(owner_id, phone)

    if client:
        try:
            await client.log_out()
        except Exception:
            pass
        del_client(owner_id, phone)

    db_delete(owner_id, phone)
    await cb.message.edit_text(
        f"✅ Аккаунт <code>{phone}</code> удалён.",
        reply_markup=kb_back(),
        parse_mode="HTML"
    )

# ─── Добавить по номеру ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "add_phone")
async def cb_add_phone(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddPhone.waiting_phone)
    await cb.message.edit_text(
        "📱 <b>Добавить аккаунт</b>\n\n"
        "Введи номер телефона в формате:\n<code>+79001234567</code>",
        reply_markup=kb_cancel(),
        parse_mode="HTML"
    )

@dp.message(AddPhone.waiting_phone)
async def fsm_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        await msg.answer("❌ Некорректный номер. Пример: <code>+79001234567</code>",
                         parse_mode="HTML")
        return

    wait = await msg.answer("⏳ Отправляю код...")
    try:
        client = await make_client()
        await client.send_code_request(phone)
        set_client(msg.from_user.id, phone, client)
        await state.update_data(phone=phone)
        await state.set_state(AddPhone.waiting_code)
        await wait.edit_text(
            f"📨 Код отправлен на <code>{phone}</code>\n\n"
            f"Введи код из Telegram (без пробелов):",
            reply_markup=kb_cancel(),
            parse_mode="HTML"
        )
    except FloodWaitError as e:
        await wait.edit_text(f"⏳ Flood wait — подожди {e.seconds} сек.", reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_back())
        await state.clear()

@dp.message(AddPhone.waiting_code)
async def fsm_code(msg: Message, state: FSMContext):
    code  = msg.text.strip()
    data  = await state.get_data()
    phone = data["phone"]
    client = get_client(msg.from_user.id, phone)

    if not client:
        await msg.answer("❌ Сессия истекла, начни заново.", reply_markup=kb_back())
        await state.clear()
        return

    wait = await msg.answer("⏳ Проверяю код...")
    try:
        await client.sign_in(phone, code)
        username, full_name = await get_profile(client)
        db_save(msg.from_user.id, phone, client, username, full_name)
        set_client(msg.from_user.id, phone, client)
        attach_code_listener(client, msg.from_user.id, phone)
        await state.clear()
        await wait.edit_text(
            f"✅ <b>Аккаунт добавлен!</b>\n\n"
            f"👤 {full_name or '—'} {'@'+username if username else ''}\n"
            f"📱 <code>{phone}</code>\n\n"
            f"👂 Слушаю входящие коды...",
            reply_markup=kb_main(),
            parse_mode="HTML"
        )
    except PhoneCodeExpiredError:
        await wait.edit_text("❌ Код истёк. Начни заново.", reply_markup=kb_back())
        await state.clear()
    except SessionPasswordNeededError:
        await state.set_state(AddPhone.waiting_password)
        await wait.edit_text(
            "🔐 Требуется пароль 2FA.\n\nВведи пароль:",
            reply_markup=kb_cancel(),
            parse_mode="HTML"
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_back())
        await state.clear()

@dp.message(AddPhone.waiting_password)
async def fsm_password(msg: Message, state: FSMContext):
    password = msg.text.strip()
    data     = await state.get_data()
    phone    = data["phone"]
    client   = get_client(msg.from_user.id, phone)

    # Удаляем сообщение с паролем для безопасности
    try:
        await msg.delete()
    except Exception:
        pass

    if not client:
        await msg.answer("❌ Сессия истекла, начни заново.", reply_markup=kb_back())
        await state.clear()
        return

    wait = await msg.answer("⏳ Проверяю пароль...")
    try:
        await client.sign_in(password=password)
        username, full_name = await get_profile(client)
        db_save(msg.from_user.id, phone, client, username, full_name)
        set_client(msg.from_user.id, phone, client)
        attach_code_listener(client, msg.from_user.id, phone)
        await state.clear()
        await wait.edit_text(
            f"✅ <b>Аккаунт добавлен!</b>\n\n"
            f"👤 {full_name or '—'} {'@'+username if username else ''}\n"
            f"📱 <code>{phone}</code>\n\n"
            f"👂 Слушаю входящие коды...",
            reply_markup=kb_main(),
            parse_mode="HTML"
        )
    except Exception as e:
        await wait.edit_text(f"❌ Неверный пароль: {e}", reply_markup=kb_back())
        await state.clear()


# ─── Загрузить сессию ─────────────────────────────────────────────────────────
@dp.callback_query(F.data == "load_session")
async def cb_load_session(cb: CallbackQuery, state: FSMContext):
    await state.set_state(LoadSession.waiting_phone)
    await cb.message.edit_text(
        "📂 <b>Загрузка сессии</b>\n\n"
        "Введи номер телефона аккаунта:",
        reply_markup=kb_cancel(),
        parse_mode="HTML"
    )

@dp.message(LoadSession.waiting_phone)
async def fsm_sess_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    if not phone.startswith("+"):
        await msg.answer("❌ Формат: <code>+79001234567</code>", parse_mode="HTML")
        return
    await state.update_data(phone=phone)
    await state.set_state(LoadSession.waiting_session)
    await msg.answer(
        "📋 Теперь отправь <b>session string</b>:\n\n"
        "<i>Это строка вида 1BVtsOK...</i>",
        parse_mode="HTML",
        reply_markup=kb_cancel()
    )

@dp.message(LoadSession.waiting_session)
async def fsm_sess_string(msg: Message, state: FSMContext):
    sess_str = msg.text.strip()
    data     = await state.get_data()
    phone    = data["phone"]
    wait     = await msg.answer("⏳ Проверяю сессию...")

    try:
        client = await make_client(sess_str)
        if not await client.is_user_authorized():
            raise Exception("Сессия невалидна или истекла")
        username, full_name = await get_profile(client)
        db_save(msg.from_user.id, phone, client, username, full_name)
        set_client(msg.from_user.id, phone, client)
        attach_code_listener(client, msg.from_user.id, phone)
        await state.clear()
        await wait.edit_text(
            f"✅ <b>Сессия загружена!</b>\n\n"
            f"👤 {full_name or '—'} {'@'+username if username else ''}\n"
            f"📱 <code>{phone}</code>\n\n"
            f"👂 Слушаю входящие коды...",
            reply_markup=kb_main(),
            parse_mode="HTML"
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_back())
        await state.clear()


# ─── Запуск ───────────────────────────────────────────────────────────────────
async def restore_listeners():
    """При старте подключаем все сохранённые аккаунты и вешаем слушатели."""
    rows = db.execute("SELECT owner_id, phone, session FROM accounts WHERE session IS NOT NULL").fetchall()
    if not rows:
        return
    log.info(f"🔄 Восстанавливаю {len(rows)} аккаунт(ов)...")
    for row in rows:
        try:
            client = await make_client(row["session"])
            if await client.is_user_authorized():
                set_client(row["owner_id"], row["phone"], client)
                attach_code_listener(client, row["owner_id"], row["phone"])
                log.info(f"  ✅ {row['phone']} — слушатель активен")
            else:
                log.warning(f"  ⚠️ {row['phone']} — сессия истекла, пропускаю")
        except Exception as e:
            log.error(f"  ❌ {row['phone']} — ошибка: {e}")

async def main():
    log.info("🚀 TG Manager Pro Bot запущен")
    await restore_listeners()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
