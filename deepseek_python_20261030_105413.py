import asyncio
import base64
import logging
import os
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import aiohttp
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = "8806340628:AAFODmsRz157x4cP0CHmYGyZwtAiyhBKWuA"
ADMIN_ID = 8318557688

DB_PATH = os.getenv("DB_PATH", "/app/data/access.db")

# TON
TON_API_URL = os.getenv("TON_API_URL", "https://toncenter.com/api/v2")
TON_API_V3_URL = os.getenv("TON_API_V3_URL", "https://toncenter.com/api/v3")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
DEFAULT_TON_ADDRESS = os.getenv("TON_ADDRESS", "")           # твой кошелёк
DEFAULT_PRICE_TON = Decimal(os.getenv("SUBSCRIPTION_PRICE_TON", "1"))
DEFAULT_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
PAYMENT_CHECK_INTERVAL = int(os.getenv("PAYMENT_CHECK_INTERVAL", "15"))

ADMIN_STATES = {}

# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

try:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
except Exception:
    pass

# ============================================================
# БАЗА
# ============================================================

async def db_connect():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    db = await db_connect()
    try:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'payment'
        );
        CREATE TABLE IF NOT EXISTS free_access (
            user_id INTEGER PRIMARY KEY,
            added_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount_nano INTEGER NOT NULL,
            comment TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            tx_hash TEXT,
            days INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS internal_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            added_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
        """)
        for key, value in {
            "ton_address": DEFAULT_TON_ADDRESS,
            "price_ton": str(DEFAULT_PRICE_TON),
            "days": str(DEFAULT_DAYS),
        }.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
        await db.commit()
    finally:
        await db.close()


async def get_setting(key, default=""):
    db = await db_connect()
    try:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default
    finally:
        await db.close()


async def set_setting(key, value):
    db = await db_connect()
    try:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await db.commit()
    finally:
        await db.close()


async def register_user(user_id, username=None, first_name=None):
    if not user_id:
        return
    db = await db_connect()
    try:
        now = int(time.time())
        await db.execute(
            "INSERT INTO internal_users(user_id,username,first_name,added_at,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "first_name=excluded.first_name, last_seen=excluded.last_seen",
            (int(user_id), username, first_name, now, now),
        )
        await db.commit()
    finally:
        await db.close()


async def has_access(user_id):
    now = int(time.time())
    db = await db_connect()
    try:
        cur = await db.execute("SELECT 1 FROM free_access WHERE user_id=?", (user_id,))
        if await cur.fetchone():
            return True
        cur = await db.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        return bool(row and int(row["expires_at"]) > now)
    finally:
        await db.close()


async def get_expires(user_id):
    db = await db_connect()
    try:
        cur = await db.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        return int(row["expires_at"]) if row else 0
    finally:
        await db.close()


async def grant_subscription(user_id, days):
    now = int(time.time())
    db = await db_connect()
    try:
        cur = await db.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        base = max(now, int(row["expires_at"])) if row else now
        expires = base + int(days) * 86400
        await db.execute(
            "INSERT INTO subscriptions(user_id, expires_at, source) VALUES(?, ?, 'payment') "
            "ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, source='payment'",
            (user_id, expires),
        )
        await db.commit()
        return expires
    finally:
        await db.close()


async def add_free_access(user_id):
    db = await db_connect()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO free_access(user_id, added_at) VALUES(?, ?)",
            (user_id, int(time.time())),
        )
        await db.commit()
    finally:
        await db.close()


async def revoke_access(user_id):
    db = await db_connect()
    try:
        await db.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM free_access WHERE user_id=?", (user_id,))
        await db.commit()
    finally:
        await db.close()


def ton_to_nano(value):
    return int(Decimal(str(value)) * Decimal(1_000_000_000))


def format_ton(value):
    return format(Decimal(str(value)).normalize(), "f")


# ============================================================
# СОЗДАНИЕ TON-СЧЁТА
# ============================================================

async def create_invoice(user_id):
    address = (await get_setting("ton_address", DEFAULT_TON_ADDRESS)).strip()
    if not address:
        raise ValueError("TON-адрес не настроен. Админ должен задать его в /admin.")
    try:
        price = Decimal(await get_setting("price_ton", str(DEFAULT_PRICE_TON)))
    except InvalidOperation:
        raise ValueError("Некорректная цена в настройках.")
    if price <= 0:
        raise ValueError("Цена должна быть больше 0.")
    days = int(await get_setting("days", str(DEFAULT_DAYS)))

    now = int(time.time())
    invoice_id = secrets.token_hex(8).upper()
    comment = f"SUB-{user_id}-{invoice_id}"
    amount_nano = ton_to_nano(price)
    expires_at = now + 60 * 60  # час на оплату

    db = await db_connect()
    try:
        await db.execute(
            "INSERT INTO payments(invoice_id,user_id,amount_nano,comment,created_at,expires_at,days) "
            "VALUES(?,?,?,?,?,?,?)",
            (invoice_id, user_id, amount_nano, comment, now, expires_at, days),
        )
        await db.commit()
    finally:
        await db.close()

    return {
        "invoice_id": invoice_id,
        "comment": comment,
        "amount": price,
        "amount_nano": amount_nano,
        "days": days,
        "expires_at": expires_at,
        "address": address,
    }


# ============================================================
# ПРОВЕРКА TON ПЛАТЕЖЕЙ
# ============================================================

def _recursive_text_candidates(value):
    out = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            out.append(text)
    elif isinstance(value, dict):
        for key in ("message", "text", "comment", "value"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        for key in ("decoded", "message_content", "msg_data"):
            if key in value:
                out.extend(_recursive_text_candidates(value.get(key)))
    elif isinstance(value, list):
        for item in value:
            out.extend(_recursive_text_candidates(item))
    return out


def decode_payment_comment(in_msg):
    if not in_msg:
        return ""
    for candidate in _recursive_text_candidates(in_msg):
        if candidate.startswith("SUB-"):
            return candidate

    msg_data = in_msg.get("msg_data") or {}
    if isinstance(msg_data, dict):
        raw = msg_data.get("text") or msg_data.get("body")
        if raw:
            try:
                decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode(
                    "utf-8", errors="ignore"
                ).strip()
                if decoded.startswith("SUB-"):
                    return decoded
            except Exception:
                pass

    content = in_msg.get("message_content") or {}
    for candidate in _recursive_text_candidates(content):
        if candidate.startswith("SUB-"):
            return candidate
    body = content.get("body") if isinstance(content, dict) else None
    if isinstance(body, str):
        try:
            decoded = base64.b64decode(body + "=" * (-len(body) % 4)).decode(
                "utf-8", errors="ignore"
            ).strip("\x00 \t\r\n")
            if "SUB-" in decoded:
                return decoded[decoded.find("SUB-"):].split("\x00", 1)[0].strip()
        except Exception:
            pass
    return ""


async def ton_get_transactions(address):
    headers = {"Accept": "application/json"}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            params = {"account": address, "limit": 100, "sort": "desc"}
            async with session.get(
                f"{TON_API_V3_URL}/transactions", params=params, headers=headers
            ) as response:
                data = await response.json(content_type=None)
                if response.status == 200 and isinstance(data, dict) and "transactions" in data:
                    return data.get("transactions") or []
                logger.warning("TON v3 response: HTTP %s", response.status)
        except Exception as error:
            logger.warning("TON v3 request failed: %s", error)

        params = {"address": address, "limit": 100, "archival": "true"}
        async with session.get(
            f"{TON_API_URL}/getTransactions", params=params, headers=headers
        ) as response:
            data = await response.json(content_type=None)
            if response.status != 200 or not data.get("ok"):
                raise RuntimeError(data.get("error", f"TON API HTTP {response.status}"))
            return data.get("result", [])


async def check_payments_once():
    address = (await get_setting("ton_address", DEFAULT_TON_ADDRESS)).strip()
    if not address:
        return
    try:
        transactions = await ton_get_transactions(address)
    except Exception as error:
        logger.warning("TON PAYMENT CHECK ERROR: %s", error)
        return

    db = await db_connect()
    try:
        cur = await db.execute(
            "SELECT * FROM payments WHERE status='pending' ORDER BY created_at ASC"
        )
        pending = await cur.fetchall()
        now = int(time.time())

        for payment in pending:
            if int(payment["expires_at"]) < now:
                await db.execute(
                    "UPDATE payments SET status='expired' WHERE invoice_id=?",
                    (payment["invoice_id"],),
                )
                await db.commit()
                continue

            expected_comment = payment["comment"].strip()
            expected_amount = int(payment["amount_nano"])

            claim = await db.execute(
                "UPDATE payments SET status='processing' WHERE invoice_id=? AND status='pending'",
                (payment["invoice_id"],),
            )
            if claim.rowcount != 1:
                continue
            await db.commit()

            matched = False
            for tx in transactions:
                in_msg = tx.get("in_msg") or {}
                try:
                    value = int(in_msg.get("value", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if value != expected_amount:
                    continue
                if decode_payment_comment(in_msg) != expected_comment:
                    continue

                tx_hash = str(
                    ((tx.get("transaction_id") or {}).get("hash")
                     or tx.get("hash")
                     or in_msg.get("hash")
                     or "")
                ).strip()
                if not tx_hash:
                    continue

                cur2 = await db.execute(
                    "SELECT 1 FROM payments WHERE tx_hash=? AND status='paid'",
                    (tx_hash,),
                )
                if await cur2.fetchone():
                    continue

                user_id = int(payment["user_id"])
                days = int(payment["days"])

                cur3 = await db.execute(
                    "SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)
                )
                sub = await cur3.fetchone()
                base = max(now, int(sub["expires_at"])) if sub else now
                expires = base + days * 86400

                await db.execute(
                    "INSERT INTO subscriptions(user_id, expires_at, source) VALUES(?, ?, 'payment') "
                    "ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, source='payment'",
                    (user_id, expires),
                )
                await db.execute(
                    "UPDATE payments SET status='paid', tx_hash=? WHERE invoice_id=?",
                    (tx_hash, payment["invoice_id"]),
                )
                await db.commit()
                matched = True

                try:
                    await bot.send_message(
                        user_id,
                        f"<b>✅ Оплата получена.</b>\n\nДоступ выдан на <b>{days} дн.</b>\n"
                        f"Действует до: <code>{time.strftime('%d.%m.%Y %H:%M', time.localtime(expires))}</code>",
                    )
                except Exception as notify_error:
                    logger.info("PAYMENT NOTIFY ERROR | user=%s | %s", user_id, notify_error)

                logger.info(
                    "TON PAYMENT CONFIRMED | user=%s | invoice=%s | tx=%s | amount_nano=%s",
                    user_id, payment["invoice_id"], tx_hash, value,
                )
                break

            if not matched:
                await db.execute(
                    "UPDATE payments SET status='pending' WHERE invoice_id=? AND status='processing'",
                    (payment["invoice_id"],),
                )
                await db.commit()
    finally:
        await db.close()


async def payment_watcher():
    while True:
        try:
            await check_payments_once()
        except Exception:
            logger.exception("PAYMENT WATCHER LOOP ERROR")
        await asyncio.sleep(PAYMENT_CHECK_INTERVAL)


# ============================================================
# BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())


# ============================================================
# ПАНЕЛЬ ПОЛЬЗОВАТЕЛЯ
# ============================================================

def access_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить доступ", style="success", callback_data="pay_access")],
        [InlineKeyboardButton(text="✅ Проверить оплату", style="primary", callback_data="check_payment")],
    ])


async def show_paywall(message, edit=False):
    price = await get_setting("price_ton", str(DEFAULT_PRICE_TON))
    days = await get_setting("days", str(DEFAULT_DAYS))
    text = (
        "<b>🔒 Доступ закрыт</b>\n\n"
        f"Стоимость доступа: <b>{format_ton(price)} TON</b>\n"
        f"Срок: <b>{days} дн.</b>\n\n"
        "Чтобы получить доступ, нажмите «Оплатить доступ»."
    )
    if edit:
        await message.edit_text(text, reply_markup=access_keyboard())
    else:
        await message.answer(text, reply_markup=access_keyboard())


@dp.message(F.text == "/start")
async def start_cmd(message: Message):
    await register_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "<b>👋 Админ</b>\n\nПанель управления: /admin\n"
            "Вы всегда имеете доступ."
        )
        return

    if await has_access(message.from_user.id):
        expires = await get_expires(message.from_user.id)
        exp_text = (
            time.strftime("%d.%m.%Y %H:%M", time.localtime(expires))
            if expires else "бессрочно"
        )
        await message.answer(
            f"<b>✅ У вас есть доступ.</b>\n\nДействует до: <code>{exp_text}</code>"
        )
        return

    await show_paywall(message)


@dp.callback_query(F.data == "pay_access")
async def pay_access(callback: CallbackQuery):
    if await has_access(callback.from_user.id):
        await callback.answer("У вас уже есть доступ", show_alert=True)
        return
    try:
        invoice = await create_invoice(callback.from_user.id)
    except Exception as error:
        logger.exception("CREATE INVOICE ERROR | user=%s", callback.from_user.id)
        await callback.answer(f"Ошибка: {str(error)[:150]}", show_alert=True)
        return

    pay_url = (
        f"ton://transfer/{invoice['address']}"
        f"?amount={invoice['amount_nano']}&text={quote(invoice['comment'])}"
    )
    tonkeeper_url = (
        f"https://app.tonkeeper.com/transfer/{invoice['address']}"
        f"?amount={invoice['amount_nano']}&text={quote(invoice['comment'])}"
    )

    text = (
        "<b>💳 Оплата доступа</b>\n\n"
        f"Сумма: <b>{format_ton(invoice['amount'])} TON</b>\n"
        f"Срок: <b>{invoice['days']} дн.</b>\n"
        f"Счёт действителен: <b>60 мин.</b>\n\n"
        "<b>Адрес получателя:</b>\n"
        f"<code>{invoice['address']}</code>\n\n"
        f"<b>Сумма:</b> <code>{format_ton(invoice['amount'])} TON</code>\n"
        f"<b>Комментарий (обязательно):</b>\n"
        f"<code>{invoice['comment']}</code>\n\n"
        "Отправьте точную сумму и <b>обязательно</b> укажите комментарий — "
        "без него платёж не определится автоматически.\n\n"
        "После оплаты нажмите «Проверить оплату»."
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Открыть Tonkeeper", url=tonkeeper_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", style="primary", callback_data=f"check_pay:{invoice['invoice_id']}")],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("check_pay:") | (F.data == "check_payment"))
async def check_payment_callback(callback: CallbackQuery):
    if await has_access(callback.from_user.id):
        await callback.answer("✅ Оплата подтверждена", show_alert=True)
        await show_paywall_ok(callback.message, callback.from_user.id)
        return

    await callback.answer("Проверяем блокчейн…")
    await check_payments_once()

    if await has_access(callback.from_user.id):
        await show_paywall_ok(callback.message, callback.from_user.id)
    else:
        await callback.answer(
            "Платёж ещё не найден. Подождите пару минут и проверьте снова.",
            show_alert=True,
        )


async def show_paywall_ok(message, user_id):
    expires = await get_expires(user_id)
    exp_text = (
        time.strftime("%d.%m.%Y %H:%M", time.localtime(expires))
        if expires else "бессрочно"
    )
    try:
        await message.edit_text(
            f"<b>✅ Оплата получена.</b>\n\nВаш доступ активен до: <code>{exp_text}</code>"
        )
    except Exception:
        await message.answer(
            f"<b>✅ Оплата получена.</b>\n\nВаш доступ активен до: <code>{exp_text}</code>"
        )


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настройки", style="primary", callback_data="admin_settings")],
        [InlineKeyboardButton(text="➕ Выдать доступ", style="success", callback_data="admin_grant")],
        [InlineKeyboardButton(text="➖ Забрать доступ", style="danger", callback_data="admin_revoke")],
        [InlineKeyboardButton(text="📊 Статистика", style="primary", callback_data="admin_stats")],
    ])


def admin_only(user_id):
    return user_id == ADMIN_ID


@dp.message(F.text == "/admin")
async def admin_cmd(message: Message):
    if not admin_only(message.from_user.id):
        return
    price = await get_setting("price_ton", str(DEFAULT_PRICE_TON))
    days = await get_setting("days", str(DEFAULT_DAYS))
    address = await get_setting("ton_address", DEFAULT_TON_ADDRESS)
    await message.answer(
        "<b>🔧 Админ-панель</b>\n\n"
        f"Кошелёк: <code>{address or 'не задан'}</code>\n"
        f"Цена: <b>{format_ton(price)} TON</b>\n"
        f"Срок: <b>{days} дн.</b>",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    price = await get_setting("price_ton", str(DEFAULT_PRICE_TON))
    days = await get_setting("days", str(DEFAULT_DAYS))
    address = await get_setting("ton_address", DEFAULT_TON_ADDRESS)
    await callback.message.edit_text(
        "<b>🔧 Админ-панель</b>\n\n"
        f"Кошелёк: <code>{address or 'не задан'}</code>\n"
        f"Цена: <b>{format_ton(price)} TON</b>\n"
        f"Срок: <b>{days} дн.</b>",
        reply_markup=admin_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_settings")
async def admin_settings(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    price = await get_setting("price_ton", str(DEFAULT_PRICE_TON))
    days = await get_setting("days", str(DEFAULT_DAYS))
    address = await get_setting("ton_address", DEFAULT_TON_ADDRESS)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Кошелёк: {address[:10] + '…' if address else 'не задан'}", callback_data="admin_set_address")],
        [InlineKeyboardButton(text=f"💵 Цена: {format_ton(price)} TON", callback_data="admin_set_price")],
        [InlineKeyboardButton(text=f"📅 Срок: {days} дн.", callback_data="admin_set_days")],
        [InlineKeyboardButton(text="Назад", style="primary", callback_data="admin_back")],
    ])
    await callback.message.edit_text(
        "<b>⚙️ Настройки</b>\n\n"
        f"Кошелёк: <code>{address or 'не задан'}</code>\n"
        f"Цена: <b>{format_ton(price)} TON</b>\n"
        f"Срок: <b>{days} дн.</b>",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data.in_({"admin_set_address", "admin_set_price", "admin_set_days"}))
async def admin_setting_prompt(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    mapping = {
        "admin_set_address": ("address", "Отправь TON-адрес кошелька для приёма оплат."),
        "admin_set_price": ("price", "Отправь цену в TON, например: <code>1.5</code>"),
        "admin_set_days": ("days", "Отправь срок доступа в днях, например: <code>30</code>"),
    }
    state, prompt = mapping[callback.data]
    ADMIN_STATES[callback.from_user.id] = state
    await callback.message.edit_text(
        f"<b>Изменение настройки</b>\n\n{prompt}\n\n/cancel — отмена"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_grant")
async def admin_grant(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    ADMIN_STATES[callback.from_user.id] = "grant"
    await callback.message.edit_text(
        "<b>➕ Выдать доступ</b>\n\n"
        "Отправь Telegram ID пользователя (числом).\n"
        "Доступ будет выдан как бесплатный (бессрочный).\n\n"
        "/cancel — отмена"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_revoke")
async def admin_revoke(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    ADMIN_STATES[callback.from_user.id] = "revoke"
    await callback.message.edit_text(
        "<b>➖ Забрать доступ</b>\n\n"
        "Отправь Telegram ID пользователя.\n\n/cancel — отмена"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    now = int(time.time())
    db = await db_connect()
    try:
        cur = await db.execute("SELECT COUNT(*) c FROM internal_users")
        users_total = (await cur.fetchone())["c"]
        cur = await db.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE expires_at > ?", (now,)
        )
        active = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COUNT(*) c FROM free_access")
        free = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COUNT(*) c FROM payments WHERE status='paid'")
        paid = (await cur.fetchone())["c"]
    finally:
        await db.close()
    await callback.message.edit_text(
        "<b>📊 Статистика</b>\n\n"
        f"Всего пользователей: <b>{users_total}</b>\n"
        f"Активных подписок: <b>{active}</b>\n"
        f"Бесплатных: <b>{free}</b>\n"
        f"Оплаченных счетов: <b>{paid}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", style="primary", callback_data="admin_back")]
        ]),
    )
    await callback.answer()


@dp.message(F.text == "/cancel")
async def cancel_state(message: Message):
    if admin_only(message.from_user.id) and message.from_user.id in ADMIN_STATES:
        ADMIN_STATES.pop(message.from_user.id, None)
        await message.answer("Отменено. /admin — панель.")


@dp.message(F.from_user.id == ADMIN_ID)
async def admin_input(message: Message):
    if not admin_only(message.from_user.id):
        return
    state = ADMIN_STATES.get(message.from_user.id)
    if not state or not message.text:
        return
    value = message.text.strip()
    try:
        if state == "address":
            if len(value) < 40 or " " in value:
                raise ValueError
            await set_setting("ton_address", value)
            await message.answer(f"✅ Кошелёк сохранён:\n<code>{value}</code>")
        elif state == "price":
            price = Decimal(value.replace(",", "."))
            if price <= 0:
                raise ValueError
            await set_setting("price_ton", format_ton(price))
            await message.answer(f"✅ Цена: <b>{format_ton(price)} TON</b>")
        elif state == "days":
            days = int(value)
            if days <= 0 or days > 3650:
                raise ValueError
            await set_setting("days", days)
            await message.answer(f"✅ Срок: <b>{days} дн.</b>")
        elif state == "grant":
            user_id = int(value)
            await add_free_access(user_id)
            await message.answer(f"✅ Бесплатный доступ выдан ID <code>{user_id}</code>.")
        elif state == "revoke":
            user_id = int(value)
            await revoke_access(user_id)
            await message.answer(f"✅ Доступ забран у ID <code>{user_id}</code>.")
        else:
            return
        ADMIN_STATES.pop(message.from_user.id, None)
    except (ValueError, InvalidOperation):
        await message.answer("❌ Неверное значение. Попробуй ещё раз или /cancel.")


# ============================================================
# FALLBACK
# ============================================================

@dp.message(F.text)
async def any_message(message: Message):
    if message.from_user.id == ADMIN_ID:
        return
    if await has_access(message.from_user.id):
        await message.answer(
            "✅ У вас есть доступ. Используйте /start, чтобы увидеть статус."
        )
    else:
        await show_paywall(message)


# ============================================================
# KEEPALIVE / START
# ============================================================

async def keepalive():
    while True:
        await asyncio.sleep(20)
        try:
            await bot.get_me()
        except Exception:
            pass


async def main():
    bot_info = await bot.get_me()
    logger.info("BOT @%s started (ID: %s)", bot_info.username, bot_info.id)

    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(keepalive())
    asyncio.create_task(payment_watcher())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())