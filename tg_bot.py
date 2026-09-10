"""TG Manager Pro — Admin Shop Bot"""

import asyncio, json, logging, os, sqlite3, aiohttp, re
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
from telethon.errors import FloodWaitError, PhoneCodeExpiredError, SessionPasswordNeededError
from telethon.sessions import StringSession

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = "8873506485:AAGn-H-PISk0n9LmpZe5Efy_J9OYoafGbgg"
API_ID           = 37658735
API_HASH         = "728f6de622061878b84d9f843181d879"
ADMIN_ID         = 8826396052
SUPPORT_USERNAME = "@admin"   # ← твой юзернейм для кнопки поддержки
DB_PATH          = "accounts.db"
SESSIONS_DIR     = "sessions"
CRYPTOBOT_TOKEN  = "YOUR_CRYPTOBOT_TOKEN"
YOOMONEY_TOKEN   = ""
YOOMONEY_WALLET  = ""
COMMISSION_PCT   = 0   # 0% — весь магазин твой, комиссия себе не нужна
TON_RUB_RATE     = 700.0

CATEGORIES = ["👤 Обычный", "💎 С Premium", "📅 Старый (2013-2017)", "🔥 Редкий"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ── DB ────────────────────────────────────────────────────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
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
""")
for _sql in [
    "ALTER TABLE accounts ADD COLUMN password TEXT",
    "ALTER TABLE shop_items ADD COLUMN country TEXT DEFAULT ''",
    "ALTER TABLE shop_items ADD COLUMN year INTEGER DEFAULT 0",
    "ALTER TABLE shop_items ADD COLUMN spam_block INTEGER DEFAULT 0",
    "ALTER TABLE shop_items ADD COLUMN has_premium INTEGER DEFAULT 0",
    "ALTER TABLE shop_items ADD COLUMN category TEXT DEFAULT ''",
    "ALTER TABLE shop_items ADD COLUMN views INTEGER DEFAULT 0",
]:
    try: db.execute(_sql); db.commit()
    except: pass
db.commit()

# ── DB HELPERS ────────────────────────────────────────────────────────────────
def db_save(owner_id, phone, client, username="", full_name="", password=""):
    s = client.session.save()
    db.execute("""INSERT INTO accounts (owner_id,phone,session,username,full_name,password)
        VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,phone) DO UPDATE SET
        session=excluded.session, username=excluded.username, full_name=excluded.full_name,
        password=CASE WHEN excluded.password!='' THEN excluded.password ELSE password END""",
        (owner_id,phone,s,username,full_name,password))
    db.commit()

def db_list(owner_id):
    return db.execute("SELECT * FROM accounts WHERE owner_id=? ORDER BY created_at DESC",(owner_id,)).fetchall()

def db_get(owner_id, phone):
    return db.execute("SELECT * FROM accounts WHERE owner_id=? AND phone=?",(owner_id,phone)).fetchone()

def db_get_by_phone(phone):
    return db.execute("SELECT * FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID)).fetchone()

def db_delete(owner_id, phone):
    db.execute("DELETE FROM accounts WHERE owner_id=? AND phone=?",(owner_id,phone)); db.commit()

def shop_add_item(phone,title,description,origin,password,price_rub,stars_price,country,year,spam_block,has_premium,category):
    cur = db.execute("""INSERT INTO shop_items
        (phone,title,description,origin,password,price_rub,stars_price,country,year,spam_block,has_premium,category)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (phone,title,description,origin,password,price_rub,stars_price,country,year,spam_block,has_premium,category))
    db.commit(); return cur.lastrowid

def shop_get_items(status="active",country=None,price_min=None,price_max=None,year_min=None,spam_block=None,has_premium=None,category=None):
    q,p = "SELECT * FROM shop_items WHERE status=?",[status]
    if country:    q+=" AND LOWER(country) LIKE ?"; p.append(f"%{country.lower()}%")
    if price_min:  q+=" AND price_rub>=?";  p.append(price_min)
    if price_max:  q+=" AND price_rub<=?";  p.append(price_max)
    if year_min:   q+=" AND year>=?";       p.append(year_min)
    if spam_block is not None: q+=" AND spam_block=?"; p.append(spam_block)
    if has_premium is not None: q+=" AND has_premium=?"; p.append(has_premium)
    if category:   q+=" AND category=?";    p.append(category)
    return db.execute(q+" ORDER BY created_at DESC",p).fetchall()

def shop_get_item(item_id):
    return db.execute("SELECT * FROM shop_items WHERE id=?",(item_id,)).fetchone()

def shop_inc_views(item_id):
    db.execute("UPDATE shop_items SET views=views+1 WHERE id=?",(item_id,)); db.commit()

def shop_mark_sold(item_id):
    db.execute("UPDATE shop_items SET status='sold' WHERE id=?",(item_id,)); db.commit()

def shop_hide_item(item_id):
    db.execute("UPDATE shop_items SET status='hidden' WHERE id=?",(item_id,)); db.commit()

def shop_unhide_item(item_id):
    db.execute("UPDATE shop_items SET status='active' WHERE id=?",(item_id,)); db.commit()

def shop_create_order(item_id,buyer_id,pay_method,amount_rub):
    cur = db.execute("INSERT INTO shop_orders (item_id,buyer_id,pay_method,amount_rub) VALUES(?,?,?,?)",
        (item_id,buyer_id,pay_method,amount_rub))
    db.commit(); return cur.lastrowid

def shop_complete_order(order_id):
    db.execute("UPDATE shop_orders SET status='paid' WHERE id=?",(order_id,)); db.commit()

def shop_add_purchase(buyer_id,item_id,order_id,phone,title,paid_rub,pay_method):
    db.execute("INSERT INTO shop_purchases (buyer_id,item_id,order_id,phone,title,paid_rub,pay_method) VALUES(?,?,?,?,?,?,?)",
        (buyer_id,item_id,order_id,phone,title,paid_rub,pay_method)); db.commit()

def shop_get_purchases(buyer_id):
    return db.execute("SELECT * FROM shop_purchases WHERE buyer_id=? ORDER BY purchased_at DESC",(buyer_id,)).fetchall()

def get_rating():
    r = db.execute("SELECT COUNT(*) as t, SUM(vote) as p FROM seller_reviews").fetchone()
    t,p = r["t"] or 0, r["p"] or 0
    return t, p, t-p

def add_review(buyer_id,order_id,vote):
    try:
        db.execute("INSERT INTO seller_reviews (buyer_id,order_id,vote) VALUES(?,?,?)",(buyer_id,order_id,vote))
        db.commit(); return True
    except: return False

def can_review(buyer_id,order_id):
    has = db.execute("SELECT id FROM shop_purchases WHERE buyer_id=? AND order_id=?",(buyer_id,order_id)).fetchone()
    already = db.execute("SELECT id FROM seller_reviews WHERE buyer_id=? AND order_id=?",(buyer_id,order_id)).fetchone()
    return bool(has) and not already

# ── TELETHON ──────────────────────────────────────────────────────────────────
tg_clients: dict = {}
adm_tasks: dict  = {}
CODE_RE = re.compile(r'\b(\d{5,6})\b')

def get_client(owner_id, phone): return tg_clients.get(owner_id,{}).get(phone)
def set_client(owner_id, phone, c): tg_clients.setdefault(owner_id,{})[phone]=c
def del_client(owner_id, phone): tg_clients.get(owner_id,{}).pop(phone,None)

def attach_listener(client, owner_id, phone):
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
    await c.connect(); return c

async def get_profile(client):
    me = await client.get_me()
    if not me: return "",""
    return me.username or "", f"{me.first_name or ''} {me.last_name or ''}".strip()

# ── CRYPTOBOT ────────────────────────────────────────────────────────────────
async def get_ton_rate():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://pay.crypt.bot/api/getExchangeRates",
                headers={"Crypto-Pay-API-Token":CRYPTOBOT_TOKEN},timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                if data.get("ok"):
                    for it in data["result"]:
                        if it.get("source")=="TON" and it.get("target")=="RUB":
                            return float(it["rate"])
    except: pass
    return TON_RUB_RATE

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

HEADER = "╔═══════════════════╗\n║   <b>TG MANAGER PRO</b>   ║\n╚═══════════════════╝"

def shop_header():
    t,p,n = get_rating()
    pct = int(p/t*100) if t else 100
    return (
        f"🏪 <b>МАГАЗИН АККАУНТОВ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⭐ Рейтинг: 👍 {p}  👎 {n}  ({pct}% положит.)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

# ── KEYBOARDS ────────────────────────────────────────────────────────────────
def btn(text, cb): return InlineKeyboardButton(text=text, callback_data=cb)
def url_btn(text, url): return InlineKeyboardButton(text=text, url=url)
def kb(*rows): return InlineKeyboardMarkup(inline_keyboard=list(rows))

def kb_main(is_admin=False):
    rows = [
        [btn("🛒 Магазин аккаунтов", "shop_browse")],
        [btn("🧾 Мои покупки", "my_purchases"), btn("💬 Поддержка", f"support")],
    ]
    if is_admin:
        rows.append([btn("👑 Панель админа", "admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back_menu(): return kb([btn("◀️ В меню", "main_menu")])
def kb_cancel():    return kb([btn("❌ Отмена",  "main_menu")])

def kb_shop_browse(items):
    rows = []
    for it in items:
        icons = ""
        if it["has_premium"]: icons += "💎"
        if it["spam_block"]:  icons += "🚫"
        year = f" [{it['year']}]" if it["year"] else ""
        rows.append([btn(f"📱 {it['title']}{year} {icons} — {it['price_rub']}₽", f"shop_item:{it['id']}")])
    rows.append([btn("🔍 Фильтры", "shop_filter"), btn("◀️ Назад", "main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_item(item_id, is_admin=False):
    rows = [
        [btn("💎 CryptoBot (TON)", f"buy_crypto:{item_id}")],
        [btn("⭐ Звёзды", f"buy_stars:{item_id}"), btn("💳 ЮMoney", f"buy_ymoney:{item_id}")],
        [btn("◀️ Назад", "shop_browse")],
    ]
    if is_admin:
        rows.insert(2,[btn("🙈 Скрыть", f"adm_hide:{item_id}"), btn("🗑 Удалить", f"adm_del:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_filter(f):
    def tog(key, labels, vals):
        cur = f.get(key)
        idx = vals.index(cur) if cur in vals else -1
        lbl = labels[(idx+1) % len(labels)]
        return lbl
    spam_lbl = {None:"любой",0:"без блока ✅",1:"есть 🚫"}.get(f.get("spam_block"),"любой")
    prem_lbl = {None:"любой",0:"без 💎",1:"с 💎"}.get(f.get("has_premium"),"любой")
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"🌍 Страна: {f.get('country') or 'любая'}", "sf_country")],
        [btn(f"💰 Цена: {f.get('price_min','—')}₽ – {f.get('price_max','—')}₽", "sf_price")],
        [btn(f"📅 Год от: {f.get('year_min','—')}", "sf_year")],
        [btn(f"🚫 Спам-блок: {spam_lbl}", "sf_spam")],
        [btn(f"💎 Premium: {prem_lbl}", "sf_prem")],
        [btn("✅ Применить", "sf_apply"), btn("🗑 Сбросить", "sf_reset")],
        [btn("◀️ Назад", "shop_browse")],
    ])

def kb_admin_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Добавить аккаунт (номер)", "adm_add_phone"),
         btn("📂 Загрузить сессию",         "adm_load_session")],
        [btn("📋 Все аккаунты",   "adm_accounts"),
         btn("🛒 Товары магазина","adm_shop_items")],
        [btn("📊 Статистика",  "adm_stats"),
         btn("💾 Скачать БД",  "adm_db")],
        [btn("🔍 Слушать код", "adm_getcode_choose")],
    ])

# ── FSM ───────────────────────────────────────────────────────────────────────
class AddPhone(StatesGroup):
    phone = State(); code = State(); password = State()

class LoadSession(StatesGroup):
    phone = State(); session = State()

class AddItem(StatesGroup):
    phone=State(); title=State(); desc=State(); origin=State()
    pwd=State(); price=State(); stars=State()
    country=State(); year=State(); spamblock=State(); premium=State(); category=State()

class ShopFilter(StatesGroup):
    country=State(); price_min=State(); price_max=State(); year=State()

# ── BOT ───────────────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

def is_admin(uid): return uid == ADMIN_ID

# ── /start ────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    adm = is_admin(msg.from_user.id)
    t,p,n = get_rating()
    items = shop_get_items()
    await msg.answer(
        f"{HEADER}\n\n"
        f"👋 Добро пожаловать!\n\n"
        f"🛒 Товаров в магазине: <b>{len(items)}</b>\n"
        f"⭐ Отзывов: <b>{t}</b> (👍{p} 👎{n})",
        reply_markup=kb_main(adm), parse_mode="HTML"
    )

@dp.callback_query(F.data == "main_menu")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    adm   = is_admin(cb.from_user.id)
    items = shop_get_items()
    t,p,n = get_rating()
    await cb.message.edit_text(
        f"{HEADER}\n\n"
        f"🛒 Товаров: <b>{len(items)}</b>\n"
        f"⭐ Отзывов: <b>{t}</b> (👍{p} 👎{n})",
        reply_markup=kb_main(adm), parse_mode="HTML"
    )

@dp.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery):
    await cb.message.edit_text(
        f"💬 <b>Поддержка</b>\n\nПо всем вопросам пиши: {SUPPORT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [url_btn("✉️ Написать", f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
            [btn("◀️ Назад", "main_menu")],
        ]), parse_mode="HTML"
    )

# ── МАГАЗИН ───────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "shop_browse")
async def cb_shop(cb: CallbackQuery, state: FSMContext):
    data  = await state.get_data()
    f     = data.get("shop_filters", {})
    items = shop_get_items(**{k:v for k,v in f.items() if v is not None}) if f else shop_get_items()
    if not items:
        await cb.message.edit_text(
            f"{shop_header()}\n\n<i>Товаров пока нет</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("🔍 Фильтры", "shop_filter")],[btn("◀️ Назад","main_menu")]
            ]), parse_mode="HTML"
        ); return
    await cb.message.edit_text(
        f"{shop_header()}\n\n📦 Доступно товаров: <b>{len(items)}</b>\n\n<i>Выбери аккаунт:</i>",
        reply_markup=kb_shop_browse(items), parse_mode="HTML"
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
    lines = [
        f"📱 <b>{item['title']}</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📝 {item['description'] or '—'}",
        f"🌍 Происхождение: {item['origin'] or '—'}",
        f"🗺 Страна: {item['country'] or '—'}",
        f"📅 Год: {item['year'] or '—'}",
        f"🚫 Спам-блок: {'✅ да' if item['spam_block'] else '❌ нет'}",
        f"💎 Premium: {'✅ да' if item['has_premium'] else '❌ нет'}",
        f"🔑 Пароль 2FA: {'✅ есть' if item['password'] else '❌ нет'}",
        f"🏷 Категория: {item['category'] or '—'}",
        f"👁 Просмотров: {item['views']}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"💰 Цена: <b>{item['price_rub']}₽</b>",
    ]
    if item["stars_price"]:
        lines.append(f"⭐ Или: <b>{item['stars_price']}</b> звёзд")
    lines += [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"⭐ Рейтинг магазина: 👍{p} 👎{n} ({pct}%)",
        f"\n<i>Выбери способ оплаты:</i>",
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
        "🔍 <b>Фильтры магазина</b>\n\nНастрой параметры поиска:",
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
    await cb.message.edit_text(
        f"{shop_header()}\n\n🔍 Найдено: <b>{len(items)}</b>",
        reply_markup=kb_shop_browse(items), parse_mode="HTML"
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
            "🧾 <b>Мои покупки</b>\n\n<i>Покупок пока нет</i>",
            reply_markup=kb_back_menu(), parse_mode="HTML"
        ); return
    text = "🧾 <b>Мои покупки</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for p in ps:
        text += f"📅 {p['purchased_at'][:10]}\n📱 {c(p['phone'])} — {h(p['title'])}\n💰 {p['paid_rub']}₽ | {p['pay_method']}\n\n"
    await cb.message.edit_text(text[:4000], reply_markup=kb_back_menu(), parse_mode="HTML")

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
async def deliver(buyer_id, item_id, order_id, pay_method):
    item = shop_get_item(item_id)
    if not item: return
    shop_mark_sold(item_id)
    shop_complete_order(order_id)
    shop_add_purchase(buyer_id, item_id, order_id, item["phone"], item["title"], item["price_rub"], pay_method)
    acc = db_get_by_phone(item["phone"])
    pwd_line  = f"🔑 Пароль 2FA: {c(item['password'])}\n" if item["password"] else ""
    sess_line = f"\n📋 <b>Session string:</b>\n{c(acc['session'])}" if acc and acc["session"] else ""
    await bot.send_message(
        buyer_id,
        f"✅ <b>Покупка успешна!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 Номер: {c(item['phone'])}\n"
        f"{pwd_line}"
        f"💰 Оплачено: {item['price_rub']}₽{sess_line}\n\n"
        f"👂 Получишь код авторизации автоматически.\n\n"
        f"<i>Оставь отзыв о покупке:</i>",
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
    db.execute("UPDATE shop_orders SET pay_ref=? WHERE id=?",(str(inv["invoice_id"]),order_id)); db.commit()
    await cb.message.edit_text(
        f"💎 <b>Оплата CryptoBot</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 {item['title']}\n"
        f"💰 {item['price_rub']}₽ = <b>{inv['ton']} TON</b>\n\n"
        f"⏱ Ссылка действительна 10 минут",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [url_btn("💳 Оплатить", inv["url"])],
            [btn("✅ Проверить оплату", f"check_crypto:{order_id}:{item_id}")],
            [btn("◀️ Отмена", f"shop_item:{item_id}")],
        ]), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("check_crypto:"))
async def check_crypto(cb: CallbackQuery):
    _, order_id, item_id = cb.data.split(":")
    order = db.execute("SELECT * FROM shop_orders WHERE id=?",(int(order_id),)).fetchone()
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
        f"💳 <b>Оплата ЮMoney</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 {item['title']}\n"
        f"💰 Сумма: <b>{item['price_rub']}₽</b>\n\n"
        f"Переведи на кошелёк:\n{c(YOOMONEY_WALLET or 'уточни у поддержки')}\n\n"
        f"📝 Комментарий: {c(f'order_{item_id}_{cb.from_user.id}')}\n\n"
        f"После оплаты напиши в поддержку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [url_btn("✉️ Написать в поддержку", f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
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
        f"👑 <b>Панель администратора</b>\n━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb_admin_main(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_panel")
async def cb_admin(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    await cb.message.edit_text(
        f"👑 <b>Панель администратора</b>\n━━━━━━━━━━━━━━━━━━━━",
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
        set_client(ADMIN_ID, phone, cl)
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
    cl    = get_client(ADMIN_ID, phone)
    if not cl:
        await msg.answer("❌ Сессия истекла", reply_markup=kb_back_menu()); await state.clear(); return
    w = await msg.answer("⏳ Проверяю...")
    try:
        await cl.sign_in(phone, msg.text.strip())
        username, full_name = await get_profile(cl)
        db_save(ADMIN_ID, phone, cl, username, full_name)
        set_client(ADMIN_ID, phone, cl)
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
    cl    = get_client(ADMIN_ID, phone)
    try: await msg.delete()
    except: pass
    w = await msg.answer("⏳ Проверяю пароль...")
    try:
        await cl.sign_in(password=msg.text.strip())
        username, full_name = await get_profile(cl)
        db_save(ADMIN_ID, phone, cl, username, full_name, msg.text.strip())
        set_client(ADMIN_ID, phone, cl); attach_listener(cl, ADMIN_ID, phone)
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
    await state.set_state(LoadSession.phone)
    await cb.message.edit_text(
        "📂 <b>Загрузка сессии</b>\n\nВведи номер телефона аккаунта:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(LoadSession.phone)
async def load_sess_phone(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.update_data(phone=msg.text.strip())
    await state.set_state(LoadSession.session)
    await msg.answer(
        "📋 Отправь <b>session string</b>\n\n<i>Строка вида: 1BVtsOK...</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )

@dp.message(LoadSession.session)
async def load_sess_string(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    data  = await state.get_data(); phone = data["phone"]
    w     = await msg.answer("⏳ Проверяю сессию...")
    try:
        cl = await make_client(msg.text.strip())
        if not await cl.is_user_authorized(): raise Exception("Сессия невалидна")
        username, full_name = await get_profile(cl)
        db_save(ADMIN_ID, phone, cl, username, full_name)
        set_client(ADMIN_ID, phone, cl); attach_listener(cl, ADMIN_ID, phone)
        await state.clear()
        await w.edit_text(
            f"✅ <b>Сессия загружена!</b>\n👤 {full_name}\n📱 {c(phone)}",
            reply_markup=kb_admin_main(), parse_mode="HTML"
        )
    except Exception as e:
        await w.edit_text(f"❌ {e}", reply_markup=kb_back_menu()); await state.clear()

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
        online = "🟢" if get_client(ADMIN_ID, r["phone"]) else "⚫️"
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
    r     = db.execute("SELECT * FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID)).fetchone()
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
             btn("🗑 Удалить",       f"adm_delete:{p}")],
            [btn("◀️ Назад","adm_accounts")],
        ]), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_export:"))
async def adm_export(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+"+cb.data.split(":")[1]
    r = db.execute("SELECT * FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID)).fetchone()
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
    cl = get_client(ADMIN_ID, phone)
    if cl:
        try: await cl.log_out()
        except: pass
        del_client(ADMIN_ID, phone)
    db_delete(ADMIN_ID, phone)
    await cb.message.edit_text(f"✅ {c(phone)} удалён", reply_markup=kb([btn("◀️ Назад","adm_accounts")]), parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_getcode:"))
async def adm_getcode(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    phone = "+"+cb.data.split(":")[1]
    r = db.execute("SELECT session FROM accounts WHERE phone=? AND owner_id=?",(phone,ADMIN_ID)).fetchone()
    if not r or not r["session"]: await cb.answer("❌ Нет сессии", show_alert=True); return
    old = adm_tasks.get(phone)
    if old and not old.done(): old.cancel()

    async def _listen():
        try:
            cl = await make_client(r["session"])
            if not await cl.is_user_authorized():
                await bot.send_message(ADMIN_ID, f"❌ Сессия {phone} истекла", parse_mode="HTML"); return
            @cl.on(events.NewMessage(from_users=[777000,42777]))
            async def _h(event):
                text = event.raw_text or ""; m = CODE_RE.search(text)
                code = m.group(1) if m else None
                if code:
                    await bot.send_message(ADMIN_ID, f"🔐 {c(phone)}\nКод: <b>{code}</b>", parse_mode="HTML")
                else:
                    await bot.send_message(ADMIN_ID, f"📨 {c(phone)}\n{text}", parse_mode="HTML")
            await asyncio.sleep(120)
            await cl.disconnect()
            await bot.send_message(ADMIN_ID, f"⏹ Слушатель {c(phone)} остановлен", parse_mode="HTML")
        except asyncio.CancelledError: pass
        except Exception as e: await bot.send_message(ADMIN_ID, f"❌ {e}")
        finally: adm_tasks.pop(phone,None)

    adm_tasks[phone] = asyncio.create_task(_listen())
    await cb.message.answer(f"👂 Слушаю коды для {c(phone)} (2 мин)", parse_mode="HTML")
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
    await state.set_state(AddItem.country)
    await msg.answer("🌍 Страна (или <code>-</code>):", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(AddItem.country)
async def ai_country(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    t = msg.text.strip()
    await state.update_data(country="" if t=="-" else t)
    await state.set_state(AddItem.year)
    await msg.answer("📅 Год создания (или <code>-</code>):", reply_markup=kb_cancel(), parse_mode="HTML")

@dp.message(AddItem.year)
async def ai_year(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    t = msg.text.strip()
    await state.update_data(year=0 if t=="-" else (int(t) if t.isdigit() else 0))
    await state.set_state(AddItem.spamblock)
    await msg.answer("🚫 Есть спам-блок?",
        reply_markup=kb([btn("✅ Да","sb:1"), btn("❌ Нет","sb:0")]))

@dp.callback_query(F.data.startswith("sb:"))
async def ai_sb(cb: CallbackQuery, state: FSMContext):
    if not adm_check(cb): return
    await state.update_data(spam_block=int(cb.data.split(":")[1]))
    await state.set_state(AddItem.premium)
    await cb.message.edit_text("💎 Есть Telegram Premium?",
        reply_markup=kb([btn("✅ Да","pm:1"), btn("❌ Нет","pm:0")]))

@dp.callback_query(F.data.startswith("pm:"))
async def ai_pm(cb: CallbackQuery, state: FSMContext):
    if not adm_check(cb): return
    await state.update_data(has_premium=int(cb.data.split(":")[1]))
    await state.set_state(AddItem.category)
    cat_btns = [[btn(c_name, f"cat:{c_name}")] for c_name in CATEGORIES]
    cat_btns.append([btn("— Без категории","cat:")])
    await cb.message.edit_text("🏷 Категория:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=cat_btns))

@dp.callback_query(F.data.startswith("cat:"))
async def ai_cat(cb: CallbackQuery, state: FSMContext):
    if not adm_check(cb): return
    cat  = cb.data.split(":",1)[1]
    await state.update_data(category=cat)
    data = await state.get_data()
    await state.set_state(None)
    item_id = shop_add_item(
        phone       = data["phone"],
        title       = data["title"],
        description = data.get("desc",""),
        origin      = data.get("origin",""),
        password    = data.get("item_pwd",""),
        price_rub   = data["price"],
        stars_price = data.get("stars"),
        country     = data.get("country",""),
        year        = data.get("year",0),
        spam_block  = data.get("spam_block",0),
        has_premium = data.get("has_premium",0),
        category    = cat,
    )
    await state.clear()
    await cb.message.edit_text(
        f"✅ <b>Товар выставлен!</b>\n📱 {data['title']}\n💰 {data['price']}₽\n🆔 {c(str(item_id))}",
        reply_markup=kb([btn("🛒 В магазин","shop_browse"), btn("👑 Панель","admin_panel")]),
        parse_mode="HTML"
    )

# ── Управление товарами ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "adm_shop_items")
async def adm_shop_items(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    rows = db.execute("SELECT * FROM shop_items ORDER BY created_at DESC").fetchall()
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
    db.execute("DELETE FROM shop_items WHERE id=?",(int(cb.data.split(":")[1]),)); db.commit()
    await cb.answer("🗑 Удалён"); await adm_shop_items(cb)

# ── Статистика ────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "adm_stats")
async def adm_stats(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    accs   = db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=?",(ADMIN_ID,)).fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM shop_items WHERE status='active'").fetchone()[0]
    sold   = db.execute("SELECT COUNT(*) FROM shop_items WHERE status='sold'").fetchone()[0]
    hidden = db.execute("SELECT COUNT(*) FROM shop_items WHERE status='hidden'").fetchone()[0]
    sales  = db.execute("SELECT SUM(amount_rub) FROM shop_orders WHERE status='paid'").fetchone()[0] or 0
    buyers = db.execute("SELECT COUNT(DISTINCT buyer_id) FROM shop_purchases").fetchone()[0]
    views  = db.execute("SELECT SUM(views) FROM shop_items").fetchone()[0] or 0
    t,p,n  = get_rating()
    await cb.message.edit_text(
        f"📊 <b>Статистика</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 Аккаунтов в базе: <b>{accs}</b>\n"
        f"🟢 В магазине: <b>{active}</b>\n"
        f"✅ Продано: <b>{sold}</b>\n"
        f"🙈 Скрыто: <b>{hidden}</b>\n"
        f"👁 Просмотров всего: <b>{views}</b>\n\n"
        f"💰 Выручка: <b>{sales}₽</b>\n"
        f"👥 Покупателей: <b>{buyers}</b>\n"
        f"⭐ Отзывов: 👍{p} 👎{n}",
        reply_markup=kb([btn("◀️ Назад","admin_panel")]), parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_db")
async def adm_db(cb: CallbackQuery):
    if not adm_check(cb): await cb.answer("⛔️", show_alert=True); return
    if not os.path.exists(DB_PATH): await cb.answer("База не найдена", show_alert=True); return
    await cb.message.answer_document(FSInputFile(DB_PATH,"accounts.db"), caption="💾 База данных")
    await cb.answer("✅")

# ── СТАРТ ────────────────────────────────────────────────────────────────────
async def restore():
    rows = db.execute("SELECT owner_id,phone,session FROM accounts WHERE session IS NOT NULL").fetchall()
    for r in rows:
        try:
            cl = await make_client(r["session"])
            if await cl.is_user_authorized():
                set_client(r["owner_id"], r["phone"], cl)
                attach_listener(cl, r["owner_id"], r["phone"])
                log.info(f"✅ {r['phone']}")
        except Exception as e: log.error(f"❌ {r['phone']}: {e}")

async def main():
    log.info("🚀 TG Manager Pro запущен")
    await restore()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
