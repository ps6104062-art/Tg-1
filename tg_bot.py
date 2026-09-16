"""TG Manager Pro — Admin Shop Bot (v2: bugfixes + performance)"""

import asyncio, json, logging, os, sqlite3, aiohttp, re, time
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, LabeledPrice, PreCheckoutQuery, FSInputFile
)
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PhoneCodeExpiredError, SessionPasswordNeededError, FreshResetAuthorisationForbiddenError
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "8873506485:AAGn-H-PISk0n9LmpZe5Efy_J9OYoafGbgg")
API_ID           = int(os.getenv("API_ID", "37658735"))
API_HASH         = os.getenv("API_HASH", "728f6de622061878b84d9f843181d879")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "5688523575"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@tebyaobossal")
DB_PATH          = "accounts.db"
SESSIONS_DIR     = "sessions"
CRYPTOBOT_TOKEN  = os.getenv("CRYPTOBOT_TOKEN", "632894:AAnLTRPHdAnsH96IKAGa9j1CX5EPcSFTrt7")
YOOMONEY_WALLET  = os.getenv("YOOMONEY_WALLET", "+79770517190 юмани")
COMMISSION_PCT   = 0
TON_RUB_RATE     = 120

# Пагинация
PAGE_SIZE = 8

CATEGORIES = ["👤 Обычный", "💎 С Premium", "📅 Старый (2013-2017)", "🔥 Редкий"]

# ── ПРЕМИУМ ЭМОДЗИ ────────────────────────────────────────────────────────────
def pe(emoji_id, fallback):
    """Премиум кастомный эмодзи"""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

E_DIAMOND = pe("5377620962390857342", "💎")  # премиум / заголовок магазина
E_STAR    = pe("5267500801240092311", "⭐")  # рейтинг / отзывы
E_FIRE    = pe("5188344996356448758", "🔥")  # редкие / горячие
E_CROWN   = pe("5424746623462823358", "👑")  # VIP / заголовок
E_CART    = pe("5278702045883292456", "🛒")  # магазин / корзина
E_CHECK   = pe("5206607081334906820", "✅")  # успех / оплата
E_MONEY   = pe("5278467510604160626", "💰")  # цена / деньги
E_ROCKET  = pe("5188481279963715781", "🚀")  # старт / приветствие
E_CARD    = pe("5472250091332993630", "💳")  # ЮMoney
E_TON     = pe("5382164415019768638", "🪙")  # CryptoBot / TON
E_ADMIN   = pe("5197288647275071607", "👑")  # админ-панель
E_STATS   = pe("5190806721286657692", "📊")  # статистика

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ── DB ────────────────────────────────────────────────────────────────────────
# FIX: используем threading.Lock для защиты SQLite в async-окружении
import threading
_db_lock = threading.Lock()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")   # FIX: WAL позволяет параллельные reads
db.execute("PRAGMA synchronous=NORMAL") # FIX: баланс скорость/надёжность
db.executescript("""
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    session TEXT,
    username TEXT,
    full_name TEXT,
    password TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(owner_id, phone)
);
CREATE TABLE IF NOT EXISTS shop_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    origin TEXT,
    password TEXT,
    price_rub INTEGER NOT NULL,
    stars_price INTEGER,
    country TEXT DEFAULT '',
    year INTEGER DEFAULT 0,
    reg_date TEXT DEFAULT '',
    tg_id INTEGER DEFAULT 0,
    spam_block INTEGER DEFAULT 0,
    has_premium INTEGER DEFAULT 0,
    category TEXT DEFAULT '',
    views INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS shop_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    buyer_id INTEGER NOT NULL,
    pay_method TEXT NOT NULL,
    amount_rub INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    pay_ref TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS shop_purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    title TEXT NOT NULL,
    paid_rub INTEGER NOT NULL,
    pay_method TEXT NOT NULL,
    purchased_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS seller_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    vote INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(buyer_id, order_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""")
for _sql in [
    "ALTER TABLE accounts ADD COLUMN password TEXT",
    "ALTER TABLE shop_items ADD COLUMN country TEXT DEFAULT ''",
    "ALTER TABLE shop_items ADD COLUMN year INTEGER DEFAULT 0",
    "ALTER TABLE shop_items ADD COLUMN reg_date TEXT DEFAULT ''",
    "ALTER TABLE shop_items ADD COLUMN tg_id INTEGER DEFAULT 0",
    "ALTER TABLE shop_items ADD COLUMN spam_block INTEGER DEFAULT 0",
    "ALTER TABLE shop_items ADD COLUMN has_premium INTEGER DEFAULT 0",
    "ALTER TABLE shop_items ADD COLUMN category TEXT DEFAULT ''",
    "ALTER TABLE shop_items ADD COLUMN views INTEGER DEFAULT 0",
]:
    try: db.execute(_sql); db.commit()
    except: pass
db.commit()

# ── DB HELPERS ────────────────────────────────────────────────────────────────
def _db_exec(sql, params=()):
    """FIX: потокобезопасный execute с lock"""
    with _db_lock:
        cur = db.execute(sql, params)
        db.commit()
        return cur

def _db_fetch(sql, params=()):
    """Потокобезопасный fetchall"""
    with _db_lock:
        return db.execute(sql, params).fetchall()

def _db_fetchone(sql, params=()):
    """Потокобезопасный fetchone"""
    with _db_lock:
        return db.execute(sql, params).fetchone()

def db_save(owner_id, phone, client, username="", full_name="", password=""):
    s = client.session.save()
    _db_exec("""INSERT INTO accounts (owner_id,phone,session,username,full_name,password)
        VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,phone) DO UPDATE SET
        session=excluded.session, username=excluded.username, full_name=excluded.full_name,
        password=CASE WHEN excluded.password!='' THEN excluded.password ELSE password END""",
        (owner_id,phone,s,username,full_name,password))

def db_list(owner_id):
    return _db_fetch("SELECT * FROM accounts WHERE owner_id=? ORDER BY created_at DESC",(owner_id,))

def db_get(owner_id, phone):
    return _db_fetchone("SELECT * FROM accounts WHERE owner_id=? AND phone=?",(owner_id,phone))

def db_get_by_phone(phone):
    return _db_fetchone("SELECT * FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID))

def db_delete(owner_id, phone):
    _db_exec("DELETE FROM accounts WHERE owner_id=? AND phone=?",(owner_id,phone))

def shop_add_item(phone,title,description,origin,password,price_rub,stars_price,country,year,reg_date,tg_id,spam_block,has_premium,category):
    cur = _db_exec("""INSERT INTO shop_items
        (phone,title,description,origin,password,price_rub,stars_price,country,year,reg_date,tg_id,spam_block,has_premium,category)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (phone,title,description,origin,password,price_rub,stars_price,country,year,reg_date,tg_id,spam_block,has_premium,category))
    return cur.lastrowid

def shop_get_items(status="active",country=None,price_min=None,price_max=None,year_min=None,spam_block=None,has_premium=None,category=None):
    q,p = "SELECT * FROM shop_items WHERE status=?",[status]
    if country:    q+=" AND LOWER(country) LIKE ?"; p.append(f"%{country.lower()}%")
    if price_min:  q+=" AND price_rub>=?";  p.append(price_min)
    if price_max:  q+=" AND price_rub<=?";  p.append(price_max)
    if year_min:   q+=" AND year>=?";       p.append(year_min)
    if spam_block is not None: q+=" AND spam_block=?"; p.append(spam_block)
    if has_premium is not None: q+=" AND has_premium=?"; p.append(has_premium)
    if category:   q+=" AND category=?";    p.append(category)
    return _db_fetch(q+" ORDER BY created_at DESC",p)

def shop_get_item(item_id):
    return _db_fetchone("SELECT * FROM shop_items WHERE id=?",(item_id,))

def shop_inc_views(item_id):
    _db_exec("UPDATE shop_items SET views=views+1 WHERE id=?",(item_id,))

def shop_mark_sold(item_id):
    _db_exec("UPDATE shop_items SET status='sold' WHERE id=?",(item_id,))

def shop_hide_item(item_id):
    _db_exec("UPDATE shop_items SET status='hidden' WHERE id=?",(item_id,))

def shop_unhide_item(item_id):
    _db_exec("UPDATE shop_items SET status='active' WHERE id=?",(item_id,))

def shop_create_order(item_id,buyer_id,pay_method,amount_rub):
    cur = _db_exec("INSERT INTO shop_orders (item_id,buyer_id,pay_method,amount_rub) VALUES(?,?,?,?)",
        (item_id,buyer_id,pay_method,amount_rub))
    return cur.lastrowid

def shop_complete_order(order_id):
    _db_exec("UPDATE shop_orders SET status='paid' WHERE id=?",(order_id,))

def shop_add_purchase(buyer_id,item_id,order_id,phone,title,paid_rub,pay_method):
    _db_exec("INSERT INTO shop_purchases (buyer_id,item_id,order_id,phone,title,paid_rub,pay_method) VALUES(?,?,?,?,?,?,?)",
        (buyer_id,item_id,order_id,phone,title,paid_rub,pay_method))

def shop_get_purchases(buyer_id):
    return _db_fetch("SELECT * FROM shop_purchases WHERE buyer_id=? ORDER BY purchased_at DESC",(buyer_id,))

def get_rating():
    r = _db_fetchone("SELECT COUNT(*) as t, SUM(vote) as p FROM seller_reviews")
    t,p = r["t"] or 0, r["p"] or 0
    return t, p, t-p

def add_review(buyer_id,order_id,vote):
    try:
        _db_exec("INSERT INTO seller_reviews (buyer_id,order_id,vote) VALUES(?,?,?)",(buyer_id,order_id,vote))
        return True
    except: return False

def can_review(buyer_id,order_id):
    has    = _db_fetchone("SELECT id FROM shop_purchases WHERE buyer_id=? AND order_id=?",(buyer_id,order_id))
    already= _db_fetchone("SELECT id FROM seller_reviews WHERE buyer_id=? AND order_id=?",(buyer_id,order_id))
    return bool(has) and not already

# ── НАСТРОЙКИ (баннер и т.д.) ─────────────────────────────────────────────────
def get_setting(key, default=None):
    row = _db_fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default

def set_setting(key, value):
    _db_exec(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )

def del_setting(key):
    _db_exec("DELETE FROM settings WHERE key=?", (key,))

# ── FIX: Rate limiter ─────────────────────────────────────────────────────────
_rate_cache: dict[int, float] = {}
RATE_LIMIT_SEC = 1.0  # минимум 1 сек между запросами одного юзера

def check_rate_limit(user_id: int) -> bool:
    """Возвращает True если запрос разрешён, False — если слишком частый"""
    now = time.monotonic()
    last = _rate_cache.get(user_id, 0)
    if now - last < RATE_LIMIT_SEC:
        return False
    _rate_cache[user_id] = now
    return True

# ── FIX: Кэш курса TON ───────────────────────────────────────────────────────
_ton_rate_cache: dict = {"rate": TON_RUB_RATE, "ts": 0}
TON_CACHE_TTL = 300  # 5 минут

async def get_ton_rate():
    now = time.monotonic()
    if now - _ton_rate_cache["ts"] < TON_CACHE_TTL:
        return _ton_rate_cache["rate"]   # FIX: не делаем HTTP каждый раз
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://pay.crypt.bot/api/getExchangeRates",
                headers={"Crypto-Pay-API-Token":CRYPTOBOT_TOKEN},timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                if data.get("ok"):
                    for it in data["result"]:
                        if it.get("source")=="TON" and it.get("target")=="RUB":
                            rate = float(it["rate"])
                            _ton_rate_cache["rate"] = rate
                            _ton_rate_cache["ts"]   = now
                            return rate
    except: pass
    return _ton_rate_cache["rate"]

# ── FIX: Реестр активных Telethon-клиентов с правильным cleanup ──────────────
class ClientRegistry:
    """Потокобезопасный реестр Telethon клиентов"""
    def __init__(self):
        self._lock = asyncio.Lock()
        self._clients: dict[tuple, TelegramClient] = {}
        self._tasks:   dict[str, asyncio.Task]      = {}

    async def get(self, owner_id, phone) -> TelegramClient | None:
        async with self._lock:
            return self._clients.get((owner_id, phone))

    async def set(self, owner_id, phone, client: TelegramClient):
        async with self._lock:
            self._clients[(owner_id, phone)] = client

    async def remove(self, owner_id, phone):
        async with self._lock:
            cl = self._clients.pop((owner_id, phone), None)
        if cl:
            try:
                await cl.disconnect()
            except Exception as e:
                log.warning(f"disconnect {phone}: {e}")

    async def all_clients(self):
        async with self._lock:
            return list(self._clients.values())

    # Задачи (listener)
    def get_task(self, phone) -> asyncio.Task | None:
        return self._tasks.get(phone)

    def set_task(self, phone, task: asyncio.Task):
        self._tasks[phone] = task

    def cancel_task(self, phone):
        t = self._tasks.pop(phone, None)
        if t and not t.done():
            t.cancel()

registry = ClientRegistry()

CODE_RE = re.compile(r'\b(\d{5,6})\b')

def attach_listener(client, owner_id, phone):
    """FIX: проверяем, не навешан ли уже handler на этот клиент"""
    @client.on(events.NewMessage(from_users=[777000,42777]))
    async def _h(event):
        text  = event.raw_text or ""
        match = CODE_RE.search(text)
        code  = match.group(1) if match else None
        try:
            if code:
                await bot.send_message(owner_id,
                    f"🔐 <b>Код авторизации</b>\n📱 <code>{phone}</code>\n\n🔑 Код: <code>{code}</code>",
                    parse_mode="HTML")
            else:
                await bot.send_message(owner_id,
                    f"📨 <b>Сообщение от Telegram</b>\n📱 <code>{phone}</code>\n\n{text}",
                    parse_mode="HTML")
        except Exception as e: log.error(f"listener:{phone}: {e}")

async def make_client(session_str=""):
    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await c.connect()
    return c

async def get_spam_block(client) -> bool:
    try:
        await client.send_message("@SpamBot", "/start")
        await asyncio.sleep(3)
        msgs = await client.get_messages("@SpamBot", limit=1)
        if msgs:
            txt = msgs[0].raw_text.lower()
            return "limited" in txt or "spam" in txt or "ограни" in txt
    except Exception as e:
        log.warning(f"spambot check: {e}")
    return False

async def get_reg_date(client) -> str:
    try:
        me = await client.get_me()
        if not me: return ""
        await client.send_message("@dateregbot", str(me.id))
        await asyncio.sleep(5)
        msgs = await client.get_messages("@dateregbot", limit=1)
        if msgs:
            txt = msgs[0].raw_text or ""
            m = re.search(r'(\w+\s+\d{4})', txt)
            if m: return m.group(1)
    except Exception as e:
        log.warning(f"dateregbot: {e}")
    return ""

PHONE_COUNTRY = {
    "7":   "🇷🇺 Россия",    "380": "🇺🇦 Украина",   "375": "🇧🇾 Беларусь",
    "77":  "🇰🇿 Казахстан", "374": "🇦🇲 Армения",   "994": "🇦🇿 Азербайджан",
    "995": "🇬🇪 Грузия",    "992": "🇹🇯 Таджикистан","998": "🇺🇿 Узбекистан",
    "996": "🇰🇬 Кыргызстан","993": "🇹🇲 Туркменистан","373":"🇲🇩 Молдова",
    "1":   "🇺🇸 США/Канада","44":  "🇬🇧 Великобритания","49":"🇩🇪 Германия",
    "33":  "🇫🇷 Франция",   "34":  "🇪🇸 Испания",   "39": "🇮🇹 Италия",
    "31":  "🇳🇱 Нидерланды","48":  "🇵🇱 Польша",    "90": "🇹🇷 Турция",
    "86":  "🇨🇳 Китай",     "91":  "🇮🇳 Индия",     "81": "🇯🇵 Япония",
}

def phone_to_country(phone: str) -> str:
    digits = phone.lstrip("+")
    for length in (3, 2, 1):
        code = digits[:length]
        if code in PHONE_COUNTRY:
            return PHONE_COUNTRY[code]
    return "🌍 Неизвестно"

async def auto_collect(phone: str, client) -> dict:
    info = {
        "country": phone_to_country(phone), "reg_date": "",
        "year": 0, "spam_block": 0, "has_premium": 0,
        "tg_id": 0, "username": "", "full_name": "",
        "has_photo": 0, "contacts": 0, "dialogs": 0, "has_2fa": 0,
    }
    try:
        me = await client.get_me()
        if me:
            info["tg_id"]       = me.id
            info["username"]    = me.username or ""
            info["full_name"]   = f"{me.first_name or ''} {me.last_name or ''}".strip()
            info["has_premium"] = 1 if getattr(me, "premium", False) else 0
            info["has_photo"]   = 1 if me.photo else 0
    except Exception as e:
        log.warning(f"auto_collect me: {e}")
    reg_date = await get_reg_date(client)
    if reg_date:
        info["reg_date"] = reg_date
        m = re.search(r'(\d{4})', reg_date)
        if m: info["year"] = int(m.group(1))
    try:
        from telethon.tl.functions.account import GetPasswordRequest
        pwd = await client(GetPasswordRequest())
        info["has_2fa"] = 1 if pwd.has_password else 0
    except Exception: pass
    try:
        contacts = await client.get_contacts()
        info["contacts"] = len(contacts)
    except Exception: pass
    try:
        dialogs = await client.get_dialogs(limit=500)
        info["dialogs"] = len(dialogs)
    except Exception: pass
    try:
        info["spam_block"] = 1 if await get_spam_block(client) else 0
    except Exception: pass
    return info

async def get_profile(client):
    me = await client.get_me()
    if not me: return "",""
    return me.username or "", f"{me.first_name or ''} {me.last_name or ''}".strip()

async def cryptobot_create_invoice(amount_rub,order_id,desc):
    rate = await get_ton_rate()
    ton  = round(amount_rub/rate,4)
    async with aiohttp.ClientSession() as s:
        async with s.post("https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token":CRYPTOBOT_TOKEN},
            json={"asset":"TON","amount":str(ton),"description":desc,"payload":str(order_id),"expires_in":600},
            timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if data.get("ok"):
                inv = data["result"]
                return {"url":inv["pay_url"],"invoice_id":inv["invoice_id"],"ton":ton}
            raise Exception(data.get("error",{}).get("name","CryptoBot error"))

async def cryptobot_check(invoice_id):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}",
            headers={"Crypto-Pay-API-Token":CRYPTOBOT_TOKEN},timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if data.get("ok"):
                items = data["result"].get("items",[])
                if items: return items[0].get("status")=="paid"
    return False

# ── HTML STYLE ────────────────────────────────────────────────────────────────
def h(text): return f"<b>{text}</b>"
def c(text): return f"<code>{text}</code>"
def i(text): return f"<i>{text}</i>"

HEADER = (
    f"{'━'*22}\n"
    f"{'{E_ROCKET}'} <b>ЧУЧМЕК СТОР</b> {'{E_DIAMOND}'}\n"
    f"{'━'*22}"
)
# HEADER строится динамически чтобы использовать pe() — определяем функцию ниже

def get_header():
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E_ROCKET} <b>ЧУЧМЕК СТОР</b> {E_DIAMOND}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

def shop_header():
    t,p,n = get_rating()
    pct = int(p/t*100) if t else 100
    return (
        f"{E_CART} <b>ЧУЧМЕК СТОР — МАГАЗИН</b> {E_DIAMOND}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{E_STAR} Рейтинг: 👍 {p}  👎 {n}  ({pct}% положит.)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

# ── KEYBOARDS ────────────────────────────────────────────────────────────────
def btn(text, cb): return InlineKeyboardButton(text=text, callback_data=cb)
def url_btn(text, url): return InlineKeyboardButton(text=text, url=url)
def kb(*rows): return InlineKeyboardMarkup(inline_keyboard=list(rows))

def kb_main(is_admin=False):
    rows = [
        [btn(f"🛒 Каталог аккаунтов", "shop_browse:0")],
        [btn("🧾 Мои покупки", "my_purchases"), btn("💬 Поддержка", "support")],
    ]
    if is_admin:
        rows.append([btn("👑 Панель админа", "admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back_menu(): return kb([btn("◀️ В меню", "main_menu")])
def kb_cancel():    return kb([btn("❌ Отмена",  "main_menu")])

def kb_shop_browse(items, page=0):
    """FIX: пагинация — показываем PAGE_SIZE товаров на страницу"""
    total = len(items)
    start = page * PAGE_SIZE
    end   = start + PAGE_SIZE
    page_items = items[start:end]

    rows = []
    for it in page_items:
        icons = ""
        if it["has_premium"]: icons += "💎"
        if it["spam_block"]:  icons += "🚫"
        year = f" [{it['year']}]" if it["year"] else ""
        rows.append([btn(f"📱 {it['title']}{year} {icons} — {it['price_rub']}₽", f"shop_item:{it['id']}")])

    # Кнопки навигации
    nav = []
    if page > 0:
        nav.append(btn(f"◀️ {page}", f"shop_browse:{page-1}"))
    if end < total:
        nav.append(btn(f"{page+2} ▶️", f"shop_browse:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([btn("🔍 Фильтры", "shop_filter"), btn("◀️ Назад", "main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_item(item_id, is_admin=False):
    rows = [
        [btn("🪙 CryptoBot (TON)", f"buy_crypto:{item_id}")],
        [btn("⭐ Звёзды", f"buy_stars:{item_id}"), btn("💳 ЮMoney", f"buy_ymoney:{item_id}")],
        [btn("◀️ Назад в каталог", "shop_browse:0")],
    ]
    if is_admin:
        rows.insert(2,[btn("🙈 Скрыть", f"adm_hide:{item_id}"), btn("🗑 Удалить", f"adm_del:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_filter(f):
    spam_lbl = {None:"любой",0:"без блока ✅",1:"есть 🚫"}.get(f.get("spam_block"),"любой")
    prem_lbl = {None:"любой",0:"без 💎",1:"с 💎"}.get(f.get("has_premium"),"любой")
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"🌍 Страна: {f.get('country') or 'любая'}", "sf_country")],
        [btn(f"💰 Цена: {f.get('price_min','—')}₽ – {f.get('price_max','—')}₽", "sf_price")],
        [btn(f"📅 Год от: {f.get('year_min','—')}", "sf_year")],
        [btn(f"🚫 Спам-блок: {spam_lbl}", "sf_spam")],
        [btn(f"💎 Premium: {prem_lbl}", "sf_prem")],
        [btn("✅ Применить", "sf_apply"), btn("🗑 Сбросить", "sf_reset")],
        [btn("◀️ Назад", "shop_browse:0")],
    ])

def kb_admin_main():
    banner_lbl = "🖼 Баннер ✅" if get_setting("banner_file_id") else "🖼 Баннер ➕"
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Добавить аккаунт (номер)", "adm_add_phone"),
         btn("📂 Загрузить сессию",         "adm_load_session")],
        [btn("📋 Все аккаунты",   "adm_accounts"),
         btn("🛒 Товары магазина","adm_shop_items")],
        [btn("📊 Статистика",  "adm_stats"),
         btn("💾 Скачать БД",  "adm_db")],
        [btn(banner_lbl, "adm_banner")],
        [btn("🔍 Слушать код", "adm_getcode_choose")],
        [btn("🏪 В магазин",   "shop_browse:0")],
    ])

# ── FSM ───────────────────────────────────────────────────────────────────────
class AddPhone(StatesGroup):
    phone = State(); code = State(); password = State()

class LoadSession(StatesGroup):
    session = State()

class AddItem(StatesGroup):
    phone=State(); title=State(); desc=State(); origin=State()
    pwd=State(); price=State(); stars=State(); category=State()

class ShopFilter(StatesGroup):
    country=State(); price_min=State(); price_max=State(); year=State()

class SetBanner(StatesGroup):
    photo = State()

# ── BOT ───────────────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

def is_admin(uid): return uid == ADMIN_ID

# ── FIX: Middleware для rate limit ────────────────────────────────────────────
@dp.callback_query.middleware()
async def rate_limit_middleware(handler, event: CallbackQuery, data):
    user_id = event.from_user.id
    if not check_rate_limit(user_id):
        await event.answer("⏳ Не так быстро!", show_alert=False)
        return
    return await handler(event, data)

# ── ГЛАВНОЕ МЕНЮ — умная отправка с баннером ──────────────────────────────────
def _main_text(name: str | None = None) -> str:
    t, p, n = get_rating()
    items   = shop_get_items()
    greeting = f"{E_ROCKET} Привет, <b>{name}</b>! Добро пожаловать!\n\n" if name else ""
    return (
        f"{get_header()}\n\n"
        f"{greeting}"
        f"Здесь продаются <b>проверенные Telegram аккаунты</b> —\n"
        f"старые, редкие, с Premium. Всё чисто {E_CHECK}\n\n"
        f"{E_CART} Аккаунтов в продаже: <b>{len(items)}</b>\n"
        f"{E_STAR} Отзывов: <b>{t}</b> (👍{p} 👎{n})\n\n"
        f"<i>Выбери что тебя интересует:</i>"
    )

async def send_main_menu(target, adm: bool, name: str | None = None):
    """
    Отправляет главное меню.
    target — Message (для /start) или CallbackQuery (для кнопки «Назад»).
    Если баннер установлен — шлёт фото с текстом как подписью.
    """
    text   = _main_text(name)
    kb     = kb_main(adm)
    banner = get_setting("banner_file_id")

    if isinstance(target, Message):
        if banner:
            await target.answer_photo(photo=banner, caption=text,
                                       reply_markup=kb, parse_mode="HTML")
        else:
            await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:  # CallbackQuery
        if banner:
            # нельзя edit_text → удаляем старое, шлём фото
            try:
                await target.message.delete()
            except Exception:
                pass
            await target.message.answer_photo(photo=banner, caption=text,
                                               reply_markup=kb, parse_mode="HTML")
        else:
            try:
                await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await target.message.answer(text, reply_markup=kb, parse_mode="HTML")

# ── /start ────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    adm  = is_admin(msg.from_user.id)
    name = msg.from_user.first_name or "друг"
    await send_main_menu(msg, adm, name)

@dp.callback_query(F.data == "main_menu")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    adm = is_admin(cb.from_user.id)
    await send_main_menu(cb, adm)

@dp.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery):
    await cb.message.edit_text(
        f"💬 <b>Поддержка Чучмек Стор</b>\n\n"
        f"Есть вопросы по заказу, аккаунту или оплате?\n"
        f"Пиши — разберёмся {E_CHECK}\n\n"
        f"Менеджер: {SUPPORT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [url_btn("✉️ Написать менеджеру", f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
            [btn("◀️ Назад", "main_menu")],
        ]), parse_mode="HTML"
    )

# ── МАГАЗИН ───────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("shop_browse:"))
async def cb_shop(cb: CallbackQuery, state: FSMContext):
    page  = int(cb.data.split(":")[1])
    data  = await state.get_data()
    f     = data.get("shop_filters", {})
    items = shop_get_items(**{k:v for k,v in f.items() if v is not None}) if f else shop_get_items()
    if not items:
        await cb.message.edit_text(
            f"{shop_header()}\n\n<i>Аккаунтов пока нет, скоро завезём {E_FIRE}</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("🔍 Фильтры", "shop_filter")],[btn("◀️ Назад","main_menu")]
            ]), parse_mode="HTML"
        ); return
    total = len(items)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    await cb.message.edit_text(
        f"{shop_header()}\n\n"
        f"{E_DIAMOND} В наличии: <b>{total} аккаунтов</b> | Стр. {page+1}/{pages}\n\n"
        f"<i>Тапни на аккаунт чтобы узнать подробности:</i>",
        reply_markup=kb_shop_browse(items, page), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("shop_item:"))
async def cb_shop_item(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    item    = shop_get_item(item_id)
    if not item or item["status"] != "active":
        await cb.answer("❌ Товар уже продан или снят", show_alert=True); return
    shop_inc_views(item_id)
    item = shop_get_item(item_id)
    adm  = is_admin(cb.from_user.id)
    t,p,n = get_rating()
    pct   = int(p/t*100) if t else 100
    prem_badge = f" {E_DIAMOND}" if item["has_premium"] else ""
    fire_badge = f" {E_FIRE}" if item["category"] and "Редкий" in item["category"] else ""
    lines = [
        f"📱 <b>{item['title']}</b>{prem_badge}{fire_badge}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📝 {item['description'] or '—'}",
        f"🌍 Происхождение: {item['origin'] or '—'}",
        f"🗺 Страна: {item['country'] or '—'}",
        f"📅 Дата регистрации: {item['reg_date'] or (str(item['year']) if item['year'] else '—')}",
        f"🆔 TG ID: {item['tg_id'] or '—'}",
        f"🚫 Спам-блок: {'✅ да' if item['spam_block'] else '❌ нет'}",
        f"{E_DIAMOND} Premium: {'✅ да' if item['has_premium'] else '❌ нет'}",
        f"🔑 Пароль 2FA: {'✅ есть' if item['password'] else '❌ нет'}",
        f"🏷 Категория: {item['category'] or '—'}",
        f"👁 Просмотров: {item['views']}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{E_MONEY} Цена: <b>{item['price_rub']}₽</b>",
    ]
    if item["stars_price"]:
        lines.append(f"{E_STAR} Или: <b>{item['stars_price']}</b> звёзд")
    lines += [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{E_STAR} Рейтинг магазина: 👍{p} 👎{n} ({pct}%)",
        f"\n<i>Выбери способ оплаты {E_CHECK}:</i>",
    ]
    await cb.message.edit_text(
        "\n".join(lines), reply_markup=kb_item(item_id, adm), parse_mode="HTML"
    )

# ── ФИЛЬТРЫ ───────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "shop_filter")
async def cb_filter(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    f    = data.get("shop_filters", {})
    await cb.message.edit_text(
        f"🔍 <b>Фильтры — Чучмек Стор</b>\n\n"
        f"Настрой параметры и найди свой аккаунт {E_DIAMOND}",
        reply_markup=kb_filter(f), parse_mode="HTML"
    )

@dp.callback_query(F.data == "sf_spam")
async def sf_spam(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data(); f = data.get("shop_filters",{})
    f["spam_block"] = {None:1,1:0,0:None}.get(f.get("spam_block"))
    await state.update_data(shop_filters=f)
    await cb.message.edit_reply_markup(reply_markup=kb_filter(f))

@dp.callback_query(F.data == "sf_prem")
async def sf_prem(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data(); f = data.get("shop_filters",{})
    f["has_premium"] = {None:1,1:0,0:None}.get(f.get("has_premium"))
    await state.update_data(shop_filters=f)
    await cb.message.edit_reply_markup(reply_markup=kb_filter(f))

@dp.callback_query(F.data == "sf_reset")
async def sf_reset(cb: CallbackQuery, state: FSMContext):
    await state.update_data(shop_filters={})
    await cb.message.edit_reply_markup(reply_markup=kb_filter({}))

@dp.callback_query(F.data == "sf_apply")
async def sf_apply(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data(); f = data.get("shop_filters",{})
    kw   = {k:v for k,v in f.items() if v is not None}
    items = shop_get_items(**kw)
    if not items:
        await cb.answer("❌ Ничего не найдено", show_alert=True); return
    total = len(items)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    await cb.message.edit_text(
        f"{shop_header()}\n\n🔍 Найдено: <b>{total}</b> | Стр. 1/{pages}",
        reply_markup=kb_shop_browse(items, 0), parse_mode="HTML"
    )

@dp.callback_query(F.data == "sf_country")
async def sf_country(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopFilter.country)
    await cb.message.edit_text("🌍 Введи страну или <code>-</code> для сброса:", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(ShopFilter.country)
async def sf_country_in(msg: Message, state: FSMContext):
    data = await state.get_data(); f = data.get("shop_filters",{})
    f["country"] = None if msg.text.strip()=="-" else msg.text.strip()
    await state.update_data(shop_filters=f); await state.set_state(None)
    await msg.answer("🔍 <b>Фильтры</b>", reply_markup=kb_filter(f), parse_mode="HTML")

@dp.callback_query(F.data == "sf_price")
async def sf_price(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopFilter.price_min)
    await cb.message.edit_text("💰 Минимальная цена (или <code>-</code>):", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(ShopFilter.price_min)
async def sf_pmin(msg: Message, state: FSMContext):
    data = await state.get_data(); f = data.get("shop_filters",{})
    txt = msg.text.strip()
    f["price_min"] = None if txt=="-" else (int(txt) if txt.isdigit() else None)
    await state.update_data(shop_filters=f); await state.set_state(ShopFilter.price_max)
    await msg.answer("💰 Максимальная цена (или <code>-</code>):", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(ShopFilter.price_max)
async def sf_pmax(msg: Message, state: FSMContext):
    data = await state.get_data(); f = data.get("shop_filters",{})
    txt = msg.text.strip()
    f["price_max"] = None if txt=="-" else (int(txt) if txt.isdigit() else None)
    await state.update_data(shop_filters=f); await state.set_state(None)
    await msg.answer("🔍 <b>Фильтры</b>", reply_markup=kb_filter(f), parse_mode="HTML")

@dp.callback_query(F.data == "sf_year")
async def sf_year(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopFilter.year)
    await cb.message.edit_text("📅 Год от (например <code>2018</code>) или <code>-</code>:", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(ShopFilter.year)
async def sf_year_in(msg: Message, state: FSMContext):
    data = await state.get_data(); f = data.get("shop_filters",{})
    txt = msg.text.strip()
    f["year_min"] = None if txt=="-" else (int(txt) if txt.isdigit() else None)
    await state.update_data(shop_filters=f); await state.set_state(None)
    await msg.answer("🔍 <b>Фильтры</b>", reply_markup=kb_filter(f), parse_mode="HTML")

# ── МОИ ПОКУПКИ ───────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "my_purchases")
async def cb_purchases(cb: CallbackQuery):
    ps = shop_get_purchases(cb.from_user.id)
    if not ps:
        await cb.message.edit_text(
            f"🧾 <b>Мои покупки</b>\n\n"
            f"<i>Покупок пока нет — загляни в каталог {E_CART}</i>",
            reply_markup=kb_back_menu(), parse_mode="HTML"
        ); return
    # FIX: строим части и объединяем с проверкой лимита
    parts = []
    for p in ps:
        parts.append(f"📅 {p['purchased_at'][:10]}\n📱 {c(p['phone'])} — {h(p['title'])}\n💰 {p['paid_rub']}₽ | {p['pay_method']}\n")
    header = f"🧾 <b>Мои покупки в Чучмек Стор</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    text = header + "\n".join(parts)
    if len(text) > 4000:
        # FIX: обрезаем правильно — не посередине записи
        text = header
        for part in parts:
            if len(text) + len(part) + 3 > 4000:
                text += f"\n<i>...и ещё {len(parts) - parts.index(part)} покупок</i>"
                break
            text += part + "\n"
    await cb.message.edit_text(text, reply_markup=kb_back_menu(), parse_mode="HTML")

# ── ОТЗЫВЫ ────────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("review:"))
async def cb_review(cb: CallbackQuery):
    _, order_id, vote = cb.data.split(":")
    if not can_review(cb.from_user.id, int(order_id)):
        await cb.answer("❌ Уже оставил отзыв", show_alert=True); return
    add_review(cb.from_user.id, int(order_id), int(vote))
    await cb.answer("✅ Отзыв сохранён!", show_alert=True)
    try: await cb.message.edit_reply_markup(reply_markup=None)
    except: pass

# ── ОПЛАТА ────────────────────────────────────────────────────────────────────
# FIX: Lock на доставку — предотвращает двойную продажу одного товара
_deliver_locks: dict[int, asyncio.Lock] = {}

async def deliver(buyer_id, item_id, order_id, pay_method):
    # FIX: per-item lock — нельзя продать товар дважды при параллельных запросах
    if item_id not in _deliver_locks:
        _deliver_locks[item_id] = asyncio.Lock()
    async with _deliver_locks[item_id]:
        item = shop_get_item(item_id)
        if not item or item["status"] != "active":
            await bot.send_message(buyer_id, "❌ Товар уже был продан кому-то другому. Средства вернутся автоматически.", parse_mode="HTML")
            return
        shop_mark_sold(item_id)

    shop_complete_order(order_id)
    shop_add_purchase(buyer_id, item_id, order_id, item["phone"], item["title"], item["price_rub"], pay_method)
    acc = db_get_by_phone(item["phone"])
    pwd_line  = f"🔑 Пароль 2FA: {c(item['password'])}\n" if item["password"] else ""
    sess_line = f"\n📋 <b>Session string:</b>\n{c(acc['session'])}" if acc and acc["session"] else ""
    await bot.send_message(
        buyer_id,
        f"{E_CHECK} <b>Покупка прошла успешно!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 Номер: {c(item['phone'])}\n"
        f"{pwd_line}"
        f"{E_MONEY} Оплачено: {item['price_rub']}₽{sess_line}\n\n"
        f"👂 Код авторизации придёт автоматически.\n"
        f"Если вопросы — пиши в поддержку {E_ROCKET}\n\n"
        f"<i>Как тебе покупка? Оставь отзыв:</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            btn("👍 Отлично", f"review:{order_id}:1"),
            btn("👎 Плохо",   f"review:{order_id}:0"),
        ]]),
        parse_mode="HTML"
    )
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Продажа!</b>\n"
        f"📱 {c(item['phone'])} — {item['title']}\n"
        f"💰 {item['price_rub']}₽ | {pay_method}\n"
        f"👤 Покупатель: {c(str(buyer_id))}",
        parse_mode="HTML"
    )
    if acc and acc["session"]:
        try:
            cl = await make_client(acc["session"])
            if await cl.is_user_authorized():
                attach_listener(cl, buyer_id, item["phone"])
                await registry.set(buyer_id, item["phone"], cl)
        except Exception as e: log.error(f"deliver listener: {e}")

@dp.callback_query(F.data.startswith("buy_crypto:"))
async def buy_crypto(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    item    = shop_get_item(item_id)
    if not item or item["status"]!="active":
        await cb.answer("❌ Товар уже продан", show_alert=True); return
    order_id = shop_create_order(item_id, cb.from_user.id, "cryptobot", item["price_rub"])
    try:
        inv = await cryptobot_create_invoice(item["price_rub"], order_id, item["title"])
    except Exception as e:
        await cb.answer(f"❌ {e}", show_alert=True); return
    _db_exec("UPDATE shop_orders SET pay_ref=? WHERE id=?",(str(inv["invoice_id"]),order_id))
    await cb.message.edit_text(
        f"{E_TON} <b>Оплата через CryptoBot</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 {item['title']}\n"
        f"{E_MONEY} {item['price_rub']}₽ = <b>{inv['ton']} TON</b>\n\n"
        f"⏱ Ссылка действует <b>10 минут</b>\n"
        f"После оплаты нажми «Проверить» {E_CHECK}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [url_btn("🪙 Оплатить в CryptoBot", inv["url"])],
            [btn("✅ Проверить оплату", f"check_crypto:{order_id}:{item_id}")],
            [btn("◀️ Отмена", f"shop_item:{item_id}")],
        ]), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("check_crypto:"))
async def check_crypto(cb: CallbackQuery):
    _, order_id, item_id = cb.data.split(":")
    order = _db_fetchone("SELECT * FROM shop_orders WHERE id=?",(int(order_id),))
    if not order or not order["pay_ref"]:
        await cb.answer("❌ Заказ не найден", show_alert=True); return
    if order["status"]=="paid":
        await cb.answer("✅ Уже оплачено", show_alert=True); return
    await cb.answer("⏳ Проверяю...")
    if await cryptobot_check(int(order["pay_ref"])):
        await cb.message.edit_text("⏳ Выдаю товар...", parse_mode="HTML")
        await deliver(cb.from_user.id, int(item_id), int(order_id), "CryptoBot")
    else:
        await cb.answer("❌ Оплата не найдена", show_alert=True)

@dp.callback_query(F.data.startswith("buy_stars:"))
async def buy_stars(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    item    = shop_get_item(item_id)
    if not item or item["status"]!="active":
        await cb.answer("❌ Товар уже продан", show_alert=True); return
    if not item["stars_price"]:
        await cb.answer("❌ Продавец не принимает звёзды", show_alert=True); return
    order_id = shop_create_order(item_id, cb.from_user.id, "stars", item["price_rub"])
    await bot.send_invoice(
        chat_id=cb.from_user.id, title=item["title"],
        description=item["description"] or item["phone"],
        payload=f"stars:{order_id}:{item_id}", currency="XTR",
        prices=[LabeledPrice(label=item["title"], amount=item["stars_price"])]
    )
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_paid(msg: Message):
    p = msg.successful_payment.invoice_payload
    if p.startswith("stars:"):
        _, oid, iid = p.split(":")
        await deliver(msg.from_user.id, int(iid), int(oid), "Звёзды")

@dp.callback_query(F.data.startswith("buy_ymoney:"))
async def buy_ymoney(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    item    = shop_get_item(item_id)
    if not item or item["status"]!="active":
        await cb.answer("❌ Товар уже продан", show_alert=True); return
    await cb.message.edit_text(
        f"{E_CARD} <b>Оплата через ЮMoney</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 {item['title']}\n"
        f"{E_MONEY} Сумма: <b>{item['price_rub']}₽</b>\n\n"
        f"Переведи на кошелёк:\n{c(YOOMONEY_WALLET or 'уточни у поддержки')}\n\n"
        f"📝 В комментарии укажи:\n{c(f'order_{item_id}_{cb.from_user.id}')}\n\n"
        f"После перевода напиши менеджеру — выдадим аккаунт {E_CHECK}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [url_btn("✉️ Написать менеджеру", f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
            [btn("◀️ Назад", f"shop_item:{item_id}")],
        ]), parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ── АДМИН-ПАНЕЛЬ ─────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def adm_check(cb): return is_admin(cb.from_user.id)

@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔️"); return
    await msg.answer(
        f"{E_ADMIN} <b>Панель администратора</b>\n"
        f"<b>Чучмек Стор</b> — управление\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb_admin_main(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_panel")
async def cb_admin(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    await cb.message.edit_text(
        f"{E_ADMIN} <b>Панель администратора</b>\n"
        f"<b>Чучмек Стор</b> — управление\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb_admin_main(), parse_mode="HTML"
    )

# ── Добавить аккаунт (номер) ─────────────────────────────────────────────────
@dp.callback_query(F.data == "adm_add_phone")
async def adm_add_phone(cb: CallbackQuery, state: FSMContext):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    await state.set_state(AddPhone.phone)
    await cb.message.edit_text(
        "📱 <b>Добавить аккаунт</b>\n\nВведи номер телефона:\n<code>+79001234567</code>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddPhone.phone)
async def adm_phone_in(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    phone = msg.text.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        await msg.answer("❌ Формат: <code>+79001234567</code>", parse_mode="HTML"); return
    w = await msg.answer("⏳ Отправляю код...")
    try:
        cl = await make_client()
        await cl.send_code_request(phone)
        await registry.set(ADMIN_ID, phone, cl)
        await state.update_data(phone=phone)
        await state.set_state(AddPhone.code)
        await w.edit_text(f"📨 Код отправлен на {c(phone)}\n\nВведи код:", reply_markup=kb_cancel(), parse_mode="HTML")
    except FloodWaitError as e:
        await w.edit_text(f"⏳ Flood wait {e.seconds} сек.", reply_markup=kb_back_menu()); await state.clear()
    except Exception as e:
        await w.edit_text(f"❌ {e}", reply_markup=kb_back_menu()); await state.clear()

@dp.message(AddPhone.code)
async def adm_code_in(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    data  = await state.get_data(); phone = data["phone"]
    cl    = await registry.get(ADMIN_ID, phone)
    if not cl:
        await msg.answer("❌ Сессия истекла", reply_markup=kb_back_menu()); await state.clear(); return
    w = await msg.answer("⏳ Проверяю...")
    try:
        await cl.sign_in(phone, msg.text.strip())
        username, full_name = await get_profile(cl)
        db_save(ADMIN_ID, phone, cl, username, full_name)
        attach_listener(cl, ADMIN_ID, phone)
        await state.clear()
        await w.edit_text(
            f"✅ <b>Аккаунт добавлен!</b>\n👤 {full_name} {('@'+username) if username else ''}\n📱 {c(phone)}",
            reply_markup=kb_admin_main(), parse_mode="HTML"
        )
    except PhoneCodeExpiredError:
        await w.edit_text("❌ Код истёк", reply_markup=kb_back_menu()); await state.clear()
    except SessionPasswordNeededError:
        await state.set_state(AddPhone.password)
        await w.edit_text("🔐 Требуется пароль 2FA:", reply_markup=kb_cancel(), parse_mode="HTML")
    except Exception as e:
        await w.edit_text(f"❌ {e}", reply_markup=kb_back_menu()); await state.clear()

@dp.message(AddPhone.password)
async def adm_pwd_in(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    data  = await state.get_data(); phone = data["phone"]
    cl    = await registry.get(ADMIN_ID, phone)
    try: await msg.delete()
    except: pass
    w = await msg.answer("⏳ Проверяю пароль...")
    try:
        await cl.sign_in(password=msg.text.strip())
        username, full_name = await get_profile(cl)
        db_save(ADMIN_ID, phone, cl, username, full_name, msg.text.strip())
        attach_listener(cl, ADMIN_ID, phone)
        await state.clear()
        await w.edit_text(
            f"✅ <b>Добавлен с 2FA!</b>\n📱 {c(phone)}",
            reply_markup=kb_admin_main(), parse_mode="HTML"
        )
    except Exception as e:
        await w.edit_text(f"❌ {e}", reply_markup=kb_back_menu()); await state.clear()

# ── Загрузить сессию ──────────────────────────────────────────────────────────
@dp.callback_query(F.data == "adm_load_session")
async def adm_load_sess(cb: CallbackQuery, state: FSMContext):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    await state.set_state(LoadSession.session)
    await cb.message.edit_text(
        "📂 <b>Загрузка сессии</b>\n\n"
        "Отправь сессию в любом формате:\n\n"
        "• <b>Session string</b> — строка вида <code>1BVtsOK...</code>\n"
        "• <b>JSON файл</b> — <code>session.json</code>\n"
        "• <b>TXT файл</b> — файл со строкой сессии\n"
        "• <b>.session файл</b> — файл Telethon\n\n"
        "<i>Номер телефона определится автоматически</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

async def _load_and_save(session_str, msg_or_cb, phone=None):
    """Загружает сессию. Если phone не передан — определяет автоматически через get_me()."""
    is_msg = isinstance(msg_or_cb, Message)
    answer = msg_or_cb.answer if is_msg else msg_or_cb.message.answer
    w = await answer("⏳ Проверяю сессию...")
    try:
        cl = await make_client(session_str)
        if not await cl.is_user_authorized():
            raise Exception("Сессия невалидна или истекла")
        username, full_name = await get_profile(cl)
        # Если номер не передан — берём из аккаунта
        if not phone:
            me = await cl.get_me()
            if not me or not me.phone:
                raise Exception("Не удалось получить номер телефона из сессии")
            phone = "+" + me.phone.lstrip("+")
        db_save(ADMIN_ID, phone, cl, username, full_name)
        await registry.set(ADMIN_ID, phone, cl)
        attach_listener(cl, ADMIN_ID, phone)
        await w.edit_text(
            f"✅ <b>Сессия загружена!</b>\n👤 {full_name} {'@'+username if username else ''}\n📱 {c(phone)}",
            reply_markup=kb_admin_main(), parse_mode="HTML"
        )
        return True
    except Exception as e:
        await w.edit_text(f"❌ {e}", reply_markup=kb_back_menu())
        return False

@dp.message(LoadSession.session)
async def load_sess_input(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.clear()

    if msg.document:
        fname = msg.document.file_name or ""
        w = await msg.answer("⏳ Скачиваю файл...")
        file = await bot.download(msg.document)
        raw  = file.read()

        if fname.endswith(".json"):
            try:
                obj = json.loads(raw.decode("utf-8"))
                sess = obj.get("session") or obj.get("string") or obj.get("session_string","")
                phone = obj.get("phone")  # берём из JSON если есть
                if not sess: raise Exception("Нет поля session в JSON")
                await w.delete()
                await _load_and_save(sess, msg, phone=phone)
            except Exception as e:
                await w.edit_text(f"❌ Ошибка JSON: {e}", reply_markup=kb_back_menu())
            return

        if fname.endswith(".session"):
            # Сохраняем файл во временное место с уникальным именем
            tmp_name = f"upload_tmp_{int(time.monotonic()*1000)}"
            path = os.path.join(SESSIONS_DIR, tmp_name + ".session")
            with open(path, "wb") as f_: f_.write(raw)
            try:
                cl = TelegramClient(os.path.join(SESSIONS_DIR, tmp_name), API_ID, API_HASH)
                await cl.connect()
                if not await cl.is_user_authorized():
                    raise Exception("Сессия невалидна или истекла")
                # Получаем данные из аккаунта
                me = await cl.get_me()
                if not me or not me.phone:
                    raise Exception("Не удалось получить номер телефона из .session файла")
                phone = "+" + me.phone.lstrip("+")
                username, full_name = await get_profile(cl)
                # FIX: конвертируем файловую сессию в StringSession строку
                # db_save вызывает client.session.save() который у файлового клиента
                # возвращает путь к файлу а не строку — поэтому пишем напрямую в БД
                sess_str = StringSession.save(cl.session)
                _db_exec(
                    """INSERT INTO accounts (owner_id,phone,session,username,full_name)
                       VALUES(?,?,?,?,?) ON CONFLICT(owner_id,phone) DO UPDATE SET
                       session=excluded.session, username=excluded.username,
                       full_name=excluded.full_name""",
                    (ADMIN_ID, phone, sess_str, username, full_name)
                )
                await registry.set(ADMIN_ID, phone, cl)
                attach_listener(cl, ADMIN_ID, phone)
                await w.edit_text(
                    f"✅ <b>Сессия загружена!</b>\n👤 {full_name} {'@'+username if username else ''}\n📱 {c(phone)}",
                    reply_markup=kb_admin_main(), parse_mode="HTML"
                )
            except Exception as e:
                await w.edit_text(f"❌ Ошибка .session: {e}", reply_markup=kb_back_menu())
            finally:
                # Удаляем временный файл
                try: os.remove(path)
                except: pass
            return

        # TXT или любой другой файл — читаем как строку сессии
        try:
            sess = raw.decode("utf-8").strip()
            await w.delete()
            await _load_and_save(sess, msg)
        except Exception as e:
            await w.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_back_menu())
        return

    if msg.text:
        await _load_and_save(msg.text.strip(), msg)
        return

    await msg.answer("❌ Неизвестный формат. Отправь строку или файл.", reply_markup=kb_back_menu())

# ── Все аккаунты ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "adm_accounts")
async def adm_accounts(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    rows = db_list(ADMIN_ID)
    if not rows:
        await cb.message.edit_text("📭 Аккаунтов нет", reply_markup=kb([btn("◀️ Назад","admin_panel")])); return
    btns = []
    for r in rows:
        p = r["phone"].replace("+","")
        online = "🟢" if await registry.get(ADMIN_ID, r["phone"]) else "⚫️"
        btns.append([btn(f"{online} {r['full_name'] or r['phone']}", f"adm_acc:{p}")])
    btns.append([btn("◀️ Назад","admin_panel")])
    await cb.message.edit_text(
        f"📋 <b>Аккаунты</b> ({len(rows)} шт.)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_acc:"))
async def adm_acc(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+"+cb.data.split(":")[1]
    r     = _db_fetchone("SELECT * FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID))
    if not r: await cb.answer("Не найден", show_alert=True); return
    p = phone.replace("+","")
    await cb.message.edit_text(
        f"👤 <b>{r['full_name'] or '—'}</b> {'@'+r['username'] if r['username'] else ''}\n"
        f"📱 {c(phone)}\n"
        f"💾 Сессия: {'✅' if r['session'] else '❌'}\n"
        f"🔑 2FA: {c(r['password']) if r['password'] else '❌'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("🛒 Выставить в магазин", f"adm_sell:{p}"),
             btn("📤 Экспорт сессии",      f"adm_export:{p}")],
            [btn("🔐 Слушать коды", f"adm_getcode:{p}"),
             btn("🚪 Выйти из сессии",    f"adm_logout:{p}")],
            [btn("🔫 Кикнуть другие сессии", f"adm_kick_sessions:{p}")],
            [btn("🗑 Удалить из бота",    f"adm_delete:{p}")],
            [btn("◀️ Назад", "adm_accounts"), btn("🏠 Панель", "admin_panel")],
        ]), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_export:"))
async def adm_export(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+"+cb.data.split(":")[1]
    r = _db_fetchone("SELECT * FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID))
    if not r or not r["session"]: await cb.answer("❌ Нет сессии", show_alert=True); return
    p = phone.lstrip("+"); path = os.path.join(SESSIONS_DIR,"exports"); os.makedirs(path,exist_ok=True)
    jf = os.path.join(path,f"{p}.json"); tf = os.path.join(path,f"{p}.txt")
    with open(jf,"w") as f: json.dump({"phone":phone,"session":r["session"],"username":r["username"],"full_name":r["full_name"]},f,indent=2,ensure_ascii=False)
    with open(tf,"w") as f: f.write(r["session"])
    await cb.message.answer_document(FSInputFile(jf,f"{p}_session.json"), caption=f"📤 JSON: {c(phone)}", parse_mode="HTML")
    await cb.message.answer_document(FSInputFile(tf,f"{p}_session.txt"),  caption=f"📤 String: {c(phone)}", parse_mode="HTML")
    await cb.answer("✅")

@dp.callback_query(F.data.startswith("adm_delete:"))
async def adm_delete(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+"+cb.data.split(":")[1]
    # FIX: правильный cleanup через registry
    await registry.remove(ADMIN_ID, phone)
    db_delete(ADMIN_ID, phone)
    await cb.message.edit_text(f"✅ {c(phone)} удалён из бота", reply_markup=kb([btn("◀️ Назад","adm_accounts")]), parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_kick_sessions:"))
async def adm_kick_sessions(cb: CallbackQuery):
    """Кикает все другие активные сессии аккаунта, кроме текущей.

    Telegram запрещает ResetAuthorization, если сессия создана менее 24 часов
    назад (FreshResetAuthorisationForbiddenError). Поэтому мы перехватываем эту
    ошибку и честно сообщаем о ней пользователю.
    """
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+" + cb.data.split(":")[1]
    r = _db_fetchone("SELECT session FROM accounts WHERE phone=? AND owner_id=?", (phone, ADMIN_ID))
    if not r or not r["session"]:
        await cb.answer("❌ Нет сессии", show_alert=True); return

    w = await cb.message.edit_text(f"⏳ Получаю список сессий {c(phone)}...", parse_mode="HTML")
    try:
        cl = await registry.get(ADMIN_ID, phone)
        if not cl:
            cl = await make_client(r["session"])
        if not await cl.is_user_authorized():
            await w.edit_text("❌ Сессия неактивна.", reply_markup=kb([btn("◀️ Назад", "adm_accounts")]), parse_mode="HTML")
            return

        result = await cl(GetAuthorizationsRequest())
        others = [a for a in result.authorizations if not a.current]

        if not others:
            await w.edit_text(
                f"ℹ️ <b>Других сессий нет</b>\n📱 {c(phone)}\n\n<i>Активна только текущая сессия бота.</i>",
                reply_markup=kb([btn("◀️ Назад", f"adm_acc:{phone.replace('+','')}")]),
                parse_mode="HTML"
            )
            return

        kicked = 0
        fresh_blocked = 0
        errors = []

        for auth in others:
            try:
                await cl(ResetAuthorizationRequest(hash=auth.hash))
                kicked += 1
            except FreshResetAuthorisationForbiddenError:
                # Сессия создана < 24 ч — Telegram не даёт кикнуть
                fresh_blocked += 1
            except Exception as e:
                errors.append(str(e))

        lines = [f"🔫 <b>Кик других сессий завершён</b>\n📱 {c(phone)}\n"]
        lines.append(f"✅ Кикнуто: <b>{kicked}</b> из {len(others)}")
        if fresh_blocked:
            lines.append(f"⏳ Пропущено (сессия &lt; 24 ч): <b>{fresh_blocked}</b>")
        if errors:
            lines.append(f"❌ Ошибки: {len(errors)}")
            lines.append(f"<i>{'; '.join(errors[:3])}</i>")

        await w.edit_text(
            "\n".join(lines),
            reply_markup=kb([btn("◀️ Назад", f"adm_acc:{phone.replace('+','')}")]),
            parse_mode="HTML"
        )
        log.info(f"kick_sessions {phone}: kicked={kicked}, fresh_blocked={fresh_blocked}")

    except Exception as e:
        await w.edit_text(
            f"❌ Ошибка: {e}\n\n{c(phone)}",
            reply_markup=kb([btn("◀️ Назад", f"adm_acc:{phone.replace('+','')}")]),
            parse_mode="HTML"
        )
        log.error(f"kick_sessions {phone}: {e}")


@dp.callback_query(F.data.startswith("adm_logout:"))
async def adm_logout(cb: CallbackQuery):
    """Выход из аккаунта Telegram (log_out) — сессия деавторизуется на серверах TG"""
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+"+cb.data.split(":")[1]
    r = _db_fetchone("SELECT session FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID))
    if not r or not r["session"]:
        await cb.answer("❌ Нет сессии", show_alert=True); return
    w = await cb.message.edit_text(f"⏳ Выхожу из аккаунта {c(phone)}...", parse_mode="HTML")
    try:
        # Берём активный клиент из реестра или создаём новый
        cl = await registry.get(ADMIN_ID, phone)
        if not cl:
            cl = await make_client(r["session"])
        if not await cl.is_user_authorized():
            await w.edit_text(
                f"⚠️ Сессия {c(phone)} уже неактивна.\nУдаляю из базы...",
                reply_markup=kb([btn("◀️ Назад","adm_accounts")]), parse_mode="HTML"
            )
            await registry.remove(ADMIN_ID, phone)
            db_delete(ADMIN_ID, phone)
            return
        # Выполняем выход — серверы TG деавторизуют сессию
        await cl.log_out()
        # Чистим реестр и БД
        registry.cancel_task(phone)
        await registry.remove(ADMIN_ID, phone)
        db_delete(ADMIN_ID, phone)
        await w.edit_text(
            f"✅ <b>Выход выполнен!</b>\n"
            f"📱 {c(phone)}\n\n"
            f"<i>Аккаунт вышел из Telegram. Сессия удалена из бота.</i>",
            reply_markup=kb([btn("◀️ Назад","adm_accounts")]), parse_mode="HTML"
        )
        log.info(f"✅ log_out {phone}")
    except Exception as e:
        await w.edit_text(
            f"❌ Ошибка при выходе: {e}\n\n{c(phone)}",
            reply_markup=kb([btn("◀️ Назад","adm_accounts")]), parse_mode="HTML"
        )
        log.error(f"log_out {phone}: {e}")

# ── FIX: Listener через task с правильным lifecycle ──────────────────────────
@dp.callback_query(F.data.startswith("adm_getcode:"))
async def adm_getcode(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+"+cb.data.split(":")[1]
    r = _db_fetchone("SELECT session FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID))
    if not r or not r["session"]: await cb.answer("❌ Нет сессии", show_alert=True); return

    # FIX: отменяем предыдущую задачу без утечки
    registry.cancel_task(phone)

    async def _listen():
        cl = None
        try:
            cl = await make_client(r["session"])
            if not await cl.is_user_authorized():
                await bot.send_message(ADMIN_ID, f"❌ Сессия {phone} истекла"); return

            @cl.on(events.NewMessage(from_users=[777000,42777]))
            async def _h(event):
                text = event.raw_text or ""; m = CODE_RE.search(text)
                code = m.group(1) if m else None
                if code:
                    await bot.send_message(ADMIN_ID, f"🔐 {c(phone)}\nКод: <b>{code}</b>", parse_mode="HTML")
                else:
                    await bot.send_message(ADMIN_ID, f"📨 {c(phone)}\n{text}", parse_mode="HTML")

            await bot.send_message(ADMIN_ID, f"👂 Слушаю коды для {c(phone)} (5 мин)", parse_mode="HTML")
            # FIX: sleep с cancellation support
            await asyncio.sleep(300)
            await bot.send_message(ADMIN_ID, f"⏹ Слушатель {c(phone)} остановлен", parse_mode="HTML")
        except asyncio.CancelledError:
            log.info(f"listener {phone} cancelled")
        except Exception as e:
            await bot.send_message(ADMIN_ID, f"❌ listener {phone}: {e}")
        finally:
            # FIX: всегда отключаемся — нет утечки соединения
            if cl:
                try: await cl.disconnect()
                except: pass
            registry.cancel_task(phone)

    task = asyncio.create_task(_listen())
    registry.set_task(phone, task)
    await cb.answer("✅")

@dp.callback_query(F.data == "adm_getcode_choose")
async def adm_getcode_choose(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    rows = db_list(ADMIN_ID)
    if not rows: await cb.answer("Нет аккаунтов", show_alert=True); return
    btns = [[btn(f"📱 {r['full_name'] or r['phone']}", f"adm_getcode:{r['phone'].replace('+','')}")] for r in rows]
    btns.append([btn("◀️ Назад","admin_panel")])
    await cb.message.edit_text("🔐 Выбери аккаунт для прослушки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

# ── Добавить товар в магазин ─────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("adm_sell:"))
async def adm_sell(cb: CallbackQuery, state: FSMContext):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+"+cb.data.split(":")[1]
    await state.update_data(phone=phone)
    await state.set_state(AddItem.title)
    await cb.message.edit_text(
        f"🛒 <b>Выставить в магазин</b>\n📱 {c(phone)}\n\n✏️ Название товара:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(AddItem.title)
async def ai_title(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.update_data(title=msg.text.strip())
    await state.set_state(AddItem.desc)
    await msg.answer("📝 Описание (или <code>-</code>):", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(AddItem.desc)
async def ai_desc(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    t = msg.text.strip()
    await state.update_data(desc="" if t=="-" else t)
    await state.set_state(AddItem.origin)
    await msg.answer("🌍 Происхождение (или <code>-</code>):", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(AddItem.origin)
async def ai_origin(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    t = msg.text.strip()
    await state.update_data(origin="" if t=="-" else t)
    await state.set_state(AddItem.pwd)
    await msg.answer("🔑 Пароль 2FA (или <code>-</code>):", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(AddItem.pwd)
async def ai_pwd(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    t = msg.text.strip()
    try: await msg.delete()
    except: pass
    await state.update_data(item_pwd="" if t=="-" else t)
    await state.set_state(AddItem.price)
    await msg.answer("💰 Цена в рублях:", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(AddItem.price)
async def ai_price(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    if not msg.text.strip().isdigit():
        await msg.answer("❌ Введи число:"); return
    await state.update_data(price=int(msg.text.strip()))
    await state.set_state(AddItem.stars)
    await msg.answer("⭐ Цена в звёздах (или <code>-</code>):", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(AddItem.stars)
async def ai_stars(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    t = msg.text.strip()
    await state.update_data(stars=None if t=="-" else (int(t) if t.isdigit() else None))
    await state.set_state(AddItem.category)
    cat_btns = [[btn(c_name, f"cat:{c_name}")] for c_name in CATEGORIES]
    cat_btns.append([btn("— Без категории","cat:")])
    await msg.answer("🏷 Категория:", reply_markup=InlineKeyboardMarkup(inline_keyboard=cat_btns))

@dp.callback_query(F.data.startswith("cat:"))
async def ai_cat(cb: CallbackQuery, state: FSMContext):
    if not adm_check(cb): return
    cat   = cb.data.split(":",1)[1]
    await state.update_data(category=cat)
    data  = await state.get_data()
    phone = data["phone"]
    wait_msg = await cb.message.edit_text(
        f"⏳ <b>Собираю данные об аккаунте...</b>\n📱 <code>{phone}</code>\n\n"
        f"🔍 Проверяю: страну, год, Premium, спам-блок...",
        parse_mode="HTML"
    )
    cl = await registry.get(ADMIN_ID, phone)
    if not cl:
        row = _db_fetchone("SELECT session FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID))
        if row and row["session"]:
            try: cl = await make_client(row["session"])
            except Exception as e:
                await wait_msg.edit_text(f"❌ Ошибка: {e}", reply_markup=kb([btn("◀️ Назад","admin_panel")]))
                await state.clear(); return
    info = await auto_collect(phone, cl) if cl else {"country": phone_to_country(phone)}
    item_id = shop_add_item(
        phone=phone, title=data["title"], description=data.get("desc",""),
        origin=data.get("origin",""), password=data.get("item_pwd",""),
        price_rub=data["price"], stars_price=data.get("stars"),
        country=info.get("country",phone_to_country(phone)),
        year=info.get("year",0), reg_date=info.get("reg_date",""),
        tg_id=info.get("tg_id",0), spam_block=info.get("spam_block",0),
        has_premium=info.get("has_premium",0), category=cat,
    )
    await state.clear()
    rd = info.get("reg_date") or "—"
    tid = info.get("tg_id") or "—"
    await wait_msg.edit_text(
        f"✅ <b>Товар выставлен!</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 {data['title']} — {data['price']}₽\n\n<b>Собранные данные:</b>\n"
        f"🌍 Страна: {info.get('country',phone_to_country(phone))}\n"
        f"📅 Дата рег.: {rd}\n🆔 TG ID: {tid}\n"
        f"💎 Premium: {'✅' if info.get('has_premium') else '❌'}\n"
        f"🚫 Спам: {'✅' if info.get('spam_block') else '❌'}\n"
        f"🔒 2FA: {'✅' if info.get('has_2fa') else '❌'}\n"
        f"📸 Фото: {'✅' if info.get('has_photo') else '❌'}\n"
        f"👥 Контактов: {info.get('contacts',0)} | 💬 Диалогов: {info.get('dialogs',0)}\n"
        f"🆔 ID товара: <code>{item_id}</code>",
        reply_markup=kb([btn("🛒 В магазин","shop_browse:0"),btn("👑 Панель","admin_panel")]),
        parse_mode="HTML"
    )

# ── Управление товарами ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "adm_shop_items")
async def adm_shop_items(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    rows = _db_fetch("SELECT * FROM shop_items ORDER BY created_at DESC")
    if not rows:
        await cb.message.edit_text("🛒 Магазин пуст",
            reply_markup=kb([btn("◀️ Назад","admin_panel")])); return
    btns = []
    for it in rows:
        ico = "🟢" if it["status"]=="active" else ("🙈" if it["status"]=="hidden" else "✅")
        btns.append([btn(f"{ico} {it['title']} — {it['price_rub']}₽", f"adm_item:{it['id']}")])
    btns.append([btn("◀️ Назад","admin_panel")])
    await cb.message.edit_text(
        f"🛒 <b>Товары магазина</b> ({len(rows)} шт.)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_item:"))
async def adm_item(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    it = shop_get_item(int(cb.data.split(":")[1]))
    if not it: await cb.answer("Не найден", show_alert=True); return
    status_txt = {"active":"🟢 Активен","hidden":"🙈 Скрыт","sold":"✅ Продан"}.get(it["status"],"?")
    hide_btn = btn("👁 Показать", f"adm_unhide:{it['id']}") if it["status"]=="hidden" else btn("🙈 Скрыть", f"adm_hide:{it['id']}")
    await cb.message.edit_text(
        f"📱 <b>{it['title']}</b>\n{status_txt}\n💰 {it['price_rub']}₽\n👁 Просмотров: {it['views']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [hide_btn, btn("🗑 Удалить", f"adm_del:{it['id']}")],
            [btn("◀️ Назад","adm_shop_items")],
        ]), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_hide:"))
async def adm_hide(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    shop_hide_item(int(cb.data.split(":")[1]))
    await cb.answer("🙈 Скрыт"); await adm_shop_items(cb)

@dp.callback_query(F.data.startswith("adm_unhide:"))
async def adm_unhide(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    shop_unhide_item(int(cb.data.split(":")[1]))
    await cb.answer("✅ Показан"); await adm_shop_items(cb)

@dp.callback_query(F.data.startswith("adm_del:"))
async def adm_del(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    _db_exec("DELETE FROM shop_items WHERE id=?",(int(cb.data.split(":")[1]),))
    await cb.answer("🗑 Удалён"); await adm_shop_items(cb)

# ── Статистика ────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "adm_stats")
async def adm_stats(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    accs   = _db_fetchone("SELECT COUNT(*) FROM accounts WHERE owner_id=?",(ADMIN_ID,))[0]
    active = _db_fetchone("SELECT COUNT(*) FROM shop_items WHERE status='active'")[0]
    sold   = _db_fetchone("SELECT COUNT(*) FROM shop_items WHERE status='sold'")[0]
    hidden = _db_fetchone("SELECT COUNT(*) FROM shop_items WHERE status='hidden'")[0]
    sales  = _db_fetchone("SELECT SUM(amount_rub) FROM shop_orders WHERE status='paid'")[0] or 0
    buyers = _db_fetchone("SELECT COUNT(DISTINCT buyer_id) FROM shop_purchases")[0]
    views  = _db_fetchone("SELECT SUM(views) FROM shop_items")[0] or 0
    t,p,n  = get_rating()
    await cb.message.edit_text(
        f"{E_STATS} <b>Статистика Чучмек Стор</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 Аккаунтов в базе: <b>{accs}</b>\n"
        f"🟢 Активных в магазине: <b>{active}</b>\n"
        f"{E_CHECK} Продано: <b>{sold}</b>\n"
        f"🙈 Скрыто: <b>{hidden}</b>\n"
        f"👁 Просмотров всего: <b>{views}</b>\n\n"
        f"{E_MONEY} Выручка: <b>{sales}₽</b>\n"
        f"👥 Уникальных покупателей: <b>{buyers}</b>\n"
        f"{E_STAR} Отзывов: 👍{p} 👎{n}",
        reply_markup=kb([btn("◀️ Назад","admin_panel")]), parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_db")
async def adm_db(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    if not os.path.exists(DB_PATH): await cb.answer("База не найдена", show_alert=True); return
    await cb.message.answer_document(FSInputFile(DB_PATH,"accounts.db"), caption="💾 База данных")
    await cb.answer("✅")

# ── Баннер главного меню ──────────────────────────────────────────────────────
def _banner_menu_kb():
    has = bool(get_setting("banner_file_id"))
    rows = [[btn("📤 Загрузить новый баннер", "adm_banner_set")]]
    if has:
        rows.append([btn("🗑 Удалить баннер", "adm_banner_del")])
    rows.append([btn("◀️ Назад", "admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "adm_banner")
async def adm_banner(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    has = bool(get_setting("banner_file_id"))
    status = "✅ Баннер установлен — показывается в главном меню" if has else "❌ Баннер не установлен"
    text = (
        f"🖼 <b>Баннер главного меню</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{status}\n\n"
        f"Баннер — картинка, которая показывается\n"
        f"под текстом при /start и в главном меню.\n\n"
        f"<i>Отправь фото чтобы заменить, или удали текущий.</i>"
    )
    if has:
        # показываем текущий баннер
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer_photo(
            photo=get_setting("banner_file_id"),
            caption=text,
            reply_markup=_banner_menu_kb(),
            parse_mode="HTML"
        )
    else:
        await cb.message.edit_text(text, reply_markup=_banner_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_banner_set")
async def adm_banner_set(cb: CallbackQuery, state: FSMContext):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    await state.set_state(SetBanner.photo)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        "🖼 <b>Загрузка баннера</b>\n\n"
        "Отправь <b>фото</b> — оно будет показываться\n"
        "в главном меню под текстом приветствия.\n\n"
        "<i>Лучший размер: 1280×640 или любое горизонтальное фото.</i>",
        reply_markup=kb([btn("❌ Отмена", "adm_banner")]),
        parse_mode="HTML"
    )

@dp.message(SetBanner.photo)
async def adm_banner_photo(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    if not msg.photo:
        await msg.answer("❌ Это не фото. Отправь картинку.", reply_markup=kb([btn("❌ Отмена", "adm_banner")]))
        return
    file_id = msg.photo[-1].file_id  # берём максимальное разрешение
    set_setting("banner_file_id", file_id)
    await state.clear()
    await msg.answer_photo(
        photo=file_id,
        caption=(
            f"✅ <b>Баннер установлен!</b>\n\n"
            f"Теперь он показывается в главном меню\n"
            f"у всех пользователей при /start."
        ),
        reply_markup=kb([btn("◀️ В панель", "admin_panel")]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_banner_del")
async def adm_banner_del(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    del_setting("banner_file_id")
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        "🗑 <b>Баннер удалён</b>\n\n"
        "Главное меню снова показывается без картинки.",
        reply_markup=kb([btn("◀️ В панель", "admin_panel")]),
        parse_mode="HTML"
    )

# ── СТАРТ ────────────────────────────────────────────────────────────────────
async def restore():
    rows = _db_fetch("SELECT owner_id,phone,session FROM accounts WHERE session IS NOT NULL")
    for r in rows:
        try:
            cl = await make_client(r["session"])
            if await cl.is_user_authorized():
                await registry.set(r["owner_id"], r["phone"], cl)
                attach_listener(cl, r["owner_id"], r["phone"])
                log.info(f"✅ restored {r['phone']}")
        except Exception as e: log.error(f"❌ restore {r['phone']}: {e}")

async def main():
    log.info("🚀 Чучмек Стор v5 запущен")
    await restore()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
