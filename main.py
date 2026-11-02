#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-магазин для товарного канала в мессенджере MAX «Цена ШОК» (max.ru/id631100036329_biz2)
Клиент: Елизавета (г. Самара)
Функционал:
1. Выкладка товаров в канал через админку бота (фото + название/описание + цена + количество в шт.).
2. Автоматические кнопки под каждым постом в канале:
   - «🛒 Забронировать»
   - «🛍 Моя корзина»
   - «👩‍💼 Связь с менеджером»
3. Гибкий учёт остатков:
   - Если товар 1 шт. -> сразу падает в корзину и снимается с публикации в канале.
   - Если товара несколько шт. -> выбор/ввод желаемого количества, списание из остатка, обновление поста в канале (и снятие поста при остатке 0).
4. Корзина покупателя + расчёт доставки и самовывоза:
   - Бесплатная доставка от 1 000 руб. (если сумма меньше — бот пишет, сколько осталось добрать до бесплатной доставки).
   - Кнопка «📍 Самовывоз» от любой суммы (адрес самовывоза уточняется по связи с менеджером).
5. Автоматический возврат товара обратно в канал при отказе:
   - Если покупатель удаляет позицию из корзины или менеджер нажимает «Отказ / Вернуть в канал» в карточке заказа,
     бот автоматически возвращает количество на остаток и заново публикует/восстанавливает пост товара в канале MAX.
"""

import asyncio
import base64
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("max_shop_bot")

MAX_API_BASE = "https://platform-api.max.ru"
_DEFAULT_B64 = "ZjlMSG9kRDBjT0xzWGtJZ3hZTVNGajdjYVlCVzBOUXpQYXJISUNEZTFZRUNtTHFhVlBkUjJPWW5FNVowZ1Bsa0dUN3RmWU5Gbnk5NUhpOXZGbDJM"
MAX_BOT_TOKEN = (
    os.getenv("MAX_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
    or os.getenv("MAX_TOKEN")
    or base64.b64decode(_DEFAULT_B64).decode("utf-8")
).strip()
DB_PATH = BASE_DIR / "shop_data.db"

DEFAULT_CHANNEL_CHAT_ID = -79384826402979
DEFAULT_FREE_DELIVERY = int(os.getenv("FREE_DELIVERY_THRESHOLD", "1500"))
DEFAULT_MANAGER_URL = os.getenv(
    "MANAGER_URL",
    "https://max.ru/u/f9LHodD0cOLwisaQT6OasJIhpM99nL8Pt9p-0FTvXvQrqhzv4wLKzzkPLzk",
)
SUPER_ADMIN_IDS = {24725732, 11879587}


# =====================================================================
# БАЗА ДАННЫХ SQLite
# =====================================================================
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            added_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            total_qty INTEGER NOT NULL,
            available_qty INTEGER NOT NULL,
            photo_payload TEXT,
            channel_chat_id INTEGER,
            channel_message_id TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cart_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            created_at TEXT,
            UNIQUE(user_id, product_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            user_username TEXT,
            phone TEXT,
            delivery_type TEXT,
            delivery_address TEXT,
            total_amount INTEGER NOT NULL,
            items_json TEXT NOT NULL,
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
        """
    )
    conn.commit()

    # Инициализация настроек по умолчанию
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("free_delivery_min", str(DEFAULT_FREE_DELIVERY)))
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("manager_url", DEFAULT_MANAGER_URL))
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("channel_chat_id", str(DEFAULT_CHANNEL_CHAT_ID)))
    # Администраторы канала «Цена ШОК»: Елизавета (24725732) и владелец канала Елена (11879587)
    cur.execute(
        "INSERT OR IGNORE INTO admins (user_id, name, added_at) VALUES (?, ?, ?)",
        (24725732, "Елизавета", "2026-11-02 23:24:00"),
    )
    cur.execute(
        "INSERT OR IGNORE INTO admins (user_id, name, added_at) VALUES (?, ?, ?)",
        (11879587, "Елена", "2026-11-02 23:24:00"),
    )
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else default


def set_setting(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    if user_id in SUPER_ADMIN_IDS:
        return True
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    found = cur.fetchone() is not None
    conn.close()
    return found


def add_admin(user_id: int, name: str = "Администратор") -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO admins (user_id, name, added_at) VALUES (?, ?, ?)",
        (user_id, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def get_all_admins() -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM admins")
    rows = {r[0] for r in cur.fetchall()} | SUPER_ADMIN_IDS
    conn.close()
    return list(rows)


# =====================================================================
# КЛИЕНТ MAX BOT API (platform-api.max.ru)
# =====================================================================
class MaxBotClient:
    def __init__(self, token: str):
        self.token = token
        self.session: Optional[aiohttp.ClientSession] = None
        self.bot_id: Optional[int] = None
        self.bot_username: str = "id631100036329_1_bot"
        self.bot_name: str = "Цена шок"

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"Authorization": self.token, "Content-Type": "application/json"}
        )
        me = await self.request("GET", "/me")
        if me:
            self.bot_id = me.get("user_id")
            self.bot_username = me.get("username", "id631100036329_1_bot")
            self.bot_name = me.get("name", "Цена шок")
            logger.info(f"Бот авторизован: {self.bot_name} (@{self.bot_username}, ID={self.bot_id})")
        # Очищаем сторонние вебхуки, чтобы работал Long Polling
        subs = await self.request("GET", "/subscriptions")
        if subs and subs.get("subscriptions"):
            for sub in subs["subscriptions"]:
                sub_url = sub.get("url")
                if sub_url:
                    await self.request("DELETE", "/subscriptions", params={"url": sub_url})
                    logger.info(f"Удалён сторонний вебхук: {sub_url}")

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.session:
            return None
        url = f"{MAX_API_BASE}{path}"
        req_params = dict(params or {})
        try:
            async with self.session.request(method, url, params=req_params, json=json_body, timeout=35) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.warning(f"MAX API {method} {path} HTTP {resp.status}: {text}")
                    return None
                return json.loads(text) if text else {}
        except Exception as e:
            logger.error(f"Ошибка запроса MAX API {method} {path}: {e}")
            return None

    async def send_message(
        self,
        text: str,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        buttons: Optional[List[List[Dict[str, Any]]]] = None,
        photo_payload: Optional[Dict[str, Any]] = None,
        text_format: str = "markdown",
    ) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if chat_id is not None:
            params["chat_id"] = chat_id
        elif user_id is not None:
            params["user_id"] = user_id
        else:
            return None

        attachments: List[Dict[str, Any]] = []
        if photo_payload:
            clean_photo = {"token": photo_payload["token"]} if "token" in photo_payload else photo_payload
            attachments.append({"type": "image", "payload": clean_photo})
        if buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})

        body: Dict[str, Any] = {
            "text": text,
            "format": text_format,
            "attachments": attachments,
        }
        return await self.request("POST", "/messages", params=params, json_body=body)

    async def edit_message(
        self,
        message_id: str,
        text: str,
        buttons: Optional[List[List[Dict[str, Any]]]] = None,
        photo_payload: Optional[Dict[str, Any]] = None,
        text_format: str = "markdown",
    ) -> Optional[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        if photo_payload:
            clean_photo = {"token": photo_payload["token"]} if "token" in photo_payload else photo_payload
            attachments.append({"type": "image", "payload": clean_photo})
        if buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})

        body: Dict[str, Any] = {
            "text": text,
            "format": text_format,
            "attachments": attachments,
        }
        return await self.request("PUT", "/messages", params={"message_id": message_id}, json_body=body)

    async def delete_message(self, message_id: str) -> bool:
        res = await self.request("DELETE", "/messages", params={"message_id": message_id})
        return res is not None

    async def answer_callback(
        self,
        callback_id: str,
        notification: Optional[str] = None,
    ) -> None:
        body: Dict[str, Any] = {"notification": notification if notification else "✅ Выполнено"}
        await self.request("POST", "/answers", params={"callback_id": callback_id}, json_body=body)

    async def detect_channel_id(self) -> int:
        saved = get_setting("channel_chat_id", str(DEFAULT_CHANNEL_CHAT_ID))
        try:
            return int(saved)
        except ValueError:
            set_setting("channel_chat_id", str(DEFAULT_CHANNEL_CHAT_ID))
            return DEFAULT_CHANNEL_CHAT_ID


# =====================================================================
# ФОРМИРОВАНИЕ ПОСТОВ В КАНАЛЕ И АВТОВОЗВРАТ ТОВАРОВ
# =====================================================================
def build_channel_post_text(product_id: int, title: str, price: int, available_qty: int) -> str:
    qty_line = (
        f"📦 **В наличии:** {available_qty} шт."
        if available_qty > 1
        else "📦 **В наличии:** 1 шт. (последний экземпляр!)"
    )
    return (
        f"🔥 **{title}**\n\n"
        f"💰 **Цена:** **{price} руб.**\n"
        f"{qty_line}\n\n"
        f"👇 Нажмите **«Забронировать»**, чтобы отложить товар в свою корзину:"
    )


def build_channel_post_buttons(bot_username: str, product_id: int) -> List[List[Dict[str, Any]]]:
    manager_url = get_setting("manager_url", DEFAULT_MANAGER_URL)
    cart_url = f"https://max.ru/{bot_username}?start=cart" if bot_username else manager_url
    buttons: List[List[Dict[str, Any]]] = [
        [
            {"type": "callback", "text": "🛒 Забронировать", "payload": f"book:{product_id}"},
        ],
        [
            {"type": "link", "text": "🛍 Моя корзина", "url": cart_url},
            {"type": "link", "text": "👩‍💼 Связь с менеджером", "url": manager_url},
        ],
    ]
    return buttons


async def sync_channel_product_post(bot: MaxBotClient, product_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, price, total_qty, available_qty, photo_payload, channel_chat_id, channel_message_id, is_active "
        "FROM products WHERE id = ?",
        (product_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return

    pid, title, price, total_qty, avail_qty, photo_json, ch_id, msg_id, is_active = row
    photo_payload = json.loads(photo_json) if photo_json else None
    if not ch_id:
        ch_id = await bot.detect_channel_id()

    if avail_qty <= 0:
        if msg_id:
            deleted = await bot.delete_message(msg_id)
            if not deleted:
                sold_text = f"🔒 **{title}**\n💰 Цена: {price} руб.\n\n⏳ **Товар забронирован!**"
                await bot.edit_message(msg_id, sold_text, buttons=[], photo_payload=photo_payload)
            cur.execute("UPDATE products SET channel_message_id = NULL WHERE id = ?", (pid,))
            conn.commit()
    else:
        post_text = build_channel_post_text(pid, title, price, avail_qty)
        buttons = build_channel_post_buttons(bot.bot_username, pid)
        if msg_id:
            edited = await bot.edit_message(msg_id, post_text, buttons=buttons, photo_payload=photo_payload)
            if not edited:
                msg_id = None
        if not msg_id and ch_id:
            sent = await bot.send_message(
                text=post_text,
                chat_id=int(ch_id),
                buttons=buttons,
                photo_payload=photo_payload,
            )
            new_mid = sent.get("message", {}).get("body", {}).get("mid") if sent else None
            if new_mid:
                cur.execute(
                    "UPDATE products SET channel_chat_id = ?, channel_message_id = ?, is_active = 1 WHERE id = ?",
                    (int(ch_id), new_mid, pid),
                )
                conn.commit()
                logger.info(f"Товар #{pid} («{title}») опубликован/возвращён в канал (mid={new_mid}, остаток={avail_qty})")
    conn.close()


# =====================================================================
# СОСТОЯНИЯ ДИАЛОГА И КОРЗИНА
# =====================================================================
USER_STATES: Dict[int, Dict[str, Any]] = {}


def get_cart_summary(user_id: int) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.product_id, c.qty, p.title, p.price
        FROM cart_items c
        JOIN products p ON p.id = c.product_id
        WHERE c.user_id = ?
        ORDER BY c.id ASC
        """,
        (user_id,),
    )
    items = []
    total = 0
    for cid, pid, qty, title, price in cur.fetchall():
        subtotal = qty * price
        total += subtotal
        items.append(
            {
                "cart_id": cid,
                "product_id": pid,
                "qty": qty,
                "title": title,
                "price": price,
                "subtotal": subtotal,
            }
        )
    conn.close()
    return {"items": items, "total": total}


async def show_user_cart(bot: MaxBotClient, user_id: int) -> None:
    summary = get_cart_summary(user_id)
    items = summary["items"]
    total = summary["total"]
    free_min = int(get_setting("free_delivery_min", "1500"))
    manager_url = get_setting("manager_url", DEFAULT_MANAGER_URL)

    if not items:
        await bot.send_message(
            text=(
                "🛍 **Ваша корзина пока пуста.**\n\n"
                "Перейдите в наш канал **«Цена ШОК»** и нажмите **«🛒 Забронировать»** под понравившимся товаром!"
            ),
            user_id=user_id,
            buttons=[
                [{"type": "link", "text": "📢 Перейти в канал «Цена ШОК»", "url": "https://max.ru/id631100036329_biz2"}],
                [{"type": "link", "text": "👩‍💼 Связь с менеджером", "url": manager_url}],
            ],
        )
        return

    lines = ["🛍 **Ваша корзина:**\n"]
    buttons: List[List[Dict[str, Any]]] = []

    for idx, it in enumerate(items, 1):
        lines.append(
            f"{idx}. **{it['title']}** — {it['qty']} шт. × {it['price']} руб. = **{it['subtotal']} руб.**"
        )
        buttons.append(
            [
                {
                    "type": "callback",
                    "text": f"❌ Отменить: {it['title'][:22]} ({it['qty']} шт.)",
                    "payload": f"cart_del:{it['product_id']}",
                }
            ]
        )

    lines.append(f"\n💰 **Итого к оплате:** **{total} руб.**\n")

    if total >= free_min:
        lines.append(
            f"✅ **Вам доступна БЕСПЛАТНАЯ ДОСТАВКА** (заказ от {free_min} руб.) или **Самовывоз**!"
        )
        buttons.append(
            [{"type": "callback", "text": "🚚 Оформить БЕСПЛАТНУЮ доставку", "payload": "checkout:delivery"}]
        )
    else:
        diff = free_min - total
        lines.append(
            f"ℹ️ *Бесплатная доставка действует при заказе от {free_min} руб. "
            f"(осталось добрать ещё на **{diff} руб.**).* "
            f"Сейчас вы можете оформить **самовывоз** от любой суммы или добрать товары в канале:"
        )

    buttons.append(
        [{"type": "callback", "text": "📍 Оформить самовывоз", "payload": "checkout:pickup"}]
    )
    buttons.append(
        [
            {"type": "callback", "text": "🗑 Очистить корзину", "payload": "cart_clear"},
            {"type": "link", "text": "👩‍💼 Связь с менеджером", "url": manager_url},
        ]
    )

    await bot.send_message(
        text="\n".join(lines),
        user_id=user_id,
        buttons=buttons,
    )


async def reserve_product_for_user(
    bot: MaxBotClient,
    user_id: int,
    user_name: str,
    product_id: int,
    qty_to_add: int,
) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT title, price, available_qty FROM products WHERE id = ?", (product_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return "Товар не найден."

    title, price, avail_qty = row
    if avail_qty <= 0:
        conn.close()
        return f"К сожалению, товар «{title}» уже полностью забронировали."
    if qty_to_add > avail_qty:
        conn.close()
        return f"В наличии осталось только {avail_qty} шт. товара «{title}». Введите количество от 1 до {avail_qty}."

    new_avail = avail_qty - qty_to_add
    cur.execute("UPDATE products SET available_qty = ? WHERE id = ?", (new_avail, product_id))
    cur.execute(
        """
        INSERT INTO cart_items (user_id, user_name, product_id, qty, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, product_id) DO UPDATE SET qty = qty + excluded.qty
        """,
        (user_id, user_name, product_id, qty_to_add, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

    await sync_channel_product_post(bot, product_id)

    # Мгновенное уведомление администраторам о новой брони в корзину
    admin_book_text = (
        f"🛒 **НОВАЯ БРОНЬ В КАНАЛЕ «Цена ШОК»!**\n\n"
        f"👤 **Покупатель:** {user_name} (ID `{user_id}`)\n"
        f"📦 **Товар:** **{title}** — {qty_to_add} шт. × {price} руб. = **{qty_to_add * price} руб.**\n"
        f"📊 **Осталось в канале:** {new_avail} шт.\n\n"
        "⏳ Товар добавлен в корзину покупателя (ожидает выбора других товаров и оформления доставки/самовывоза).\n"
        "Если покупатель передумал, вы можете сразу вернуть товар в канал кнопкой ниже:"
    )
    admin_book_btns = [
        [
            {
                "type": "callback",
                "text": f"🔄 Снять бронь и вернуть «{title[:18]}» в канал",
                "payload": f"adm_unbook:{user_id}:{product_id}",
            }
        ]
    ]
    for adm_id in get_all_admins():
        await bot.send_message(text=admin_book_text, user_id=adm_id, buttons=admin_book_btns)

    return None


async def return_product_from_cart(bot: MaxBotClient, user_id: int, product_id: int) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT c.qty, p.title FROM cart_items c JOIN products p ON p.id = c.product_id WHERE c.user_id = ? AND c.product_id = ?",
        (user_id, product_id),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    qty, title = row
    cur.execute("DELETE FROM cart_items WHERE user_id = ? AND product_id = ?", (user_id, product_id))
    cur.execute("UPDATE products SET available_qty = available_qty + ? WHERE id = ?", (qty, product_id))
    conn.commit()
    conn.close()

    await sync_channel_product_post(bot, product_id)
    return f"Бронь на «{title}» ({qty} шт.) отменена, товар возвращён в канал."


async def clear_user_cart_with_return(bot: MaxBotClient, user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT product_id, qty FROM cart_items WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    cur.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
    for pid, qty in rows:
        cur.execute("UPDATE products SET available_qty = available_qty + ? WHERE id = ?", (qty, pid))
    conn.commit()
    conn.close()

    for pid, _ in rows:
        await sync_channel_product_post(bot, pid)


# =====================================================================
# АДМИН-ПАНЕЛЬ
# =====================================================================
def get_open_delivery_dates() -> List[str]:
    raw = get_setting("delivery_dates", "").strip()
    if not raw or raw in ("0", "нет", "-"):
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    return parts[:8]


async def show_admin_menu(bot: MaxBotClient, user_id: int) -> None:
    free_min = get_setting("free_delivery_min", "1500")
    ch_id = await bot.detect_channel_id()
    open_dates = get_open_delivery_dates()
    dates_status = ", ".join(open_dates) if open_dates else "По согласованию с менеджером"

    text = (
        "⚙️ **Панель управления магазином «Цена ШОК»**\n\n"
        f"📢 **Канал MAX:** ✅ Подключён («Цена ШОК», `{ch_id}`)\n"
        f"🚚 **Бесплатная доставка:** от **{free_min} руб.**\n"
        f"📅 **Даты доставки:** {dates_status}\n"
        "📍 **Самовывоз:** от любой суммы (адрес по связи с менеджером)\n\n"
        "Выберите действие:"
    )
    buttons = [
        [{"type": "callback", "text": "➕ Выложить новый товар в канал", "payload": "adm:new_prod"}],
        [{"type": "callback", "text": "📅 Открыть / изменить даты доставки", "payload": "adm:set_deliv_dates"}],
        [{"type": "callback", "text": "📦 Товары и остатки", "payload": "adm:list_prods"}],
        [{"type": "callback", "text": "📋 Заказы покупателей (и возврат в канал)", "payload": "adm:orders"}],
        [{"type": "callback", "text": "🚚 Изменить порог бесплатной доставки", "payload": "adm:set_free_min"}],
    ]
    await bot.send_message(text=text, user_id=user_id, buttons=buttons)


# =====================================================================
# ОБРАБОТЧИКИ СОБЫТИЙ MAX
# =====================================================================
async def handle_booking_trigger(
    bot: MaxBotClient,
    user_id: int,
    user_name: str,
    product_id: int,
    callback_id: Optional[str] = None,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT title, price, available_qty FROM products WHERE id = ?", (product_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        if callback_id:
            await bot.answer_callback(callback_id, "Товар не найден.")
        return

    title, price, avail_qty = row
    if avail_qty <= 0:
        if callback_id:
            await bot.answer_callback(callback_id, "К сожалению, этот товар уже забронировали!")
        await bot.send_message(f"😔 Товар **«{title}»** уже полностью забронирован.", user_id=user_id)
        return

    if avail_qty == 1:
        err = await reserve_product_for_user(bot, user_id, user_name, product_id, 1)
        if err:
            if callback_id:
                await bot.answer_callback(callback_id, err)
            await bot.send_message(f"⚠️ {err}", user_id=user_id)
            return
        set_setting(f"pending_book_{user_id}", "")
        if callback_id:
            await bot.answer_callback(
                callback_id,
                f"✅ «{title}» (1 шт.) забронирован в вашу корзину! Откройте бот @id631100036329_1_bot для оформления заказа.",
            )
        await bot.send_message(
            text=f"✅ Товар **«{title}»** (1 шт. — **{price} руб.**) забронирован и добавлен в вашу корзину!",
            user_id=user_id,
        )
        await show_user_cart(bot, user_id)
    else:
        set_setting(f"pending_book_{user_id}", str(product_id))
        USER_STATES[user_id] = {
            "state": "await_book_qty",
            "product_id": product_id,
            "user_name": user_name,
        }
        qty_buttons: List[Dict[str, Any]] = []
        max_btn = min(avail_qty, 5)
        for q in range(1, max_btn + 1):
            qty_buttons.append({"type": "callback", "text": f"{q} шт.", "payload": f"book_qty:{product_id}:{q}"})
        rows = [qty_buttons]
        if avail_qty > 5:
            rows.append(
                [{"type": "callback", "text": f"Все ({avail_qty} шт.)", "payload": f"book_qty:{product_id}:{avail_qty}"}]
            )
        rows.append([{"type": "callback", "text": "🛍 Открыть мою корзину", "payload": "open_cart"}])

        sent_prompt = await bot.send_message(
            text=(
                f"🛒 Вы выбрали товар: **{title}**\n"
                f"💰 Цена: **{price} руб./шт.** | 📦 Доступно в наличии: **{avail_qty} шт.**\n\n"
                f"👇 Нажмите на кнопку с нужным количеством или **напишите число в ответном сообщении** (от 1 до {avail_qty}):"
            ),
            user_id=user_id,
            buttons=rows,
        )
        if callback_id:
            if sent_prompt is None:
                await bot.answer_callback(
                    callback_id,
                    f"👉 Нажмите кнопку «🛍 Моя корзина» под этим постом и нажмите «Начать» в боте для выбора количества «{title}»!",
                )
            else:
                await bot.answer_callback(
                    callback_id,
                    f"📩 Откройте чат с ботом (кнопка «Моя корзина»), чтобы выбрать количество товара «{title}»!",
                )


async def finish_checkout(
    bot: MaxBotClient,
    user_id: int,
    user_name: str,
    user_username: str,
    delivery_type: str,
    delivery_address: str,
    phone_and_comment: str,
) -> None:
    summary = get_cart_summary(user_id)
    items = summary["items"]
    total = summary["total"]
    if not items:
        await bot.send_message("Ваша корзина пуста.", user_id=user_id)
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO orders (user_id, user_name, user_username, phone, delivery_type, delivery_address, total_amount, items_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
        """,
        (
            user_id,
            user_name,
            user_username,
            phone_and_comment,
            delivery_type,
            delivery_address,
            total,
            json.dumps(items, ensure_ascii=False),
            now_str,
        ),
    )
    order_id = cur.lastrowid
    cur.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    items_text = "\n".join(
        [f"• **{it['title']}** — {it['qty']} шт. × {it['price']} руб. = **{it['subtotal']} руб.**" for it in items]
    )
    manager_url = get_setting("manager_url", DEFAULT_MANAGER_URL)

    if delivery_type == "delivery":
        delivery_info = f"🚚 **Бесплатная доставка:** {delivery_address}"
    else:
        delivery_info = "📍 **Способ получения:** Самовывоз (точный адрес по связи с менеджером)"

    await bot.send_message(
        text=(
            f"🎉 **Ваш заказ №{order_id} успешно оформлен!**\n\n"
            f"📦 **Состав заказа:**\n{items_text}\n\n"
            f"💰 **Итого:** **{total} руб.**\n"
            f"{delivery_info}\n"
            f"📞 Контакт / комментарий: {phone_and_comment}\n\n"
            "Менеджер уже получил ваш заказ и свяжется с вами (также вы можете написать менеджеру по кнопке ниже):"
        ),
        user_id=user_id,
        buttons=[
            [{"type": "link", "text": "👩‍💼 Связь с менеджером", "url": manager_url}],
            [{"type": "link", "text": "📢 Вернуться в канал «Цена ШОК»", "url": "https://max.ru/id631100036329_biz2"}],
        ],
    )

    user_link = f"https://max.ru/{user_username}" if user_username else f"ID {user_id}"
    admin_text = (
        f"🔔 **НОВЫЙ ЗАКАЗ №{order_id} («Цена ШОК»)**\n\n"
        f"👤 **Покупатель:** {user_name} ({user_link})\n"
        f"📞 **Телефон / комментарий:** {phone_and_comment}\n"
        f"{delivery_info}\n\n"
        f"📦 **Товары:**\n{items_text}\n\n"
        f"💰 **Сумма заказа:** **{total} руб.**"
    )
    admin_buttons = [
        [{"type": "callback", "text": f"✅ Заказ №{order_id} выдан / выполнен", "payload": f"ord_done:{order_id}"}],
        [
            {
                "type": "callback",
                "text": f"🔄 Отказ — вернуть товары заказа №{order_id} в канал",
                "payload": f"ord_cancel:{order_id}",
            }
        ],
    ]
    for adm_id in get_all_admins():
        await bot.send_message(text=admin_text, user_id=adm_id, buttons=admin_buttons)


async def cancel_order_and_restore_to_channel(bot: MaxBotClient, order_id: int, admin_user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT items_json, status, user_name FROM orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await bot.send_message(f"Заказ №{order_id} не найден.", user_id=admin_user_id)
        return
    items_json, status, buyer_name = row
    if status == "cancelled":
        conn.close()
        await bot.send_message(
            f"Заказ №{order_id} уже был отменён ранее, товары уже возвращены в канал.",
            user_id=admin_user_id,
        )
        return

    items = json.loads(items_json)
    cur.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
    for it in items:
        cur.execute(
            "UPDATE products SET available_qty = available_qty + ? WHERE id = ?",
            (it["qty"], it["product_id"]),
        )
    conn.commit()
    conn.close()

    restored_names = []
    for it in items:
        await sync_channel_product_post(bot, int(it["product_id"]))
        restored_names.append(f"• {it['title']} (+{it['qty']} шт.)")

    await bot.send_message(
        text=(
            f"🔄 **Заказ №{order_id} ({buyer_name}) отменён.**\n"
            f"Следующие товары автоматически возвращены на остаток и снова опубликованы в канале **«Цена ШОК»**:\n"
            + "\n".join(restored_names)
        ),
        user_id=admin_user_id,
    )


async def process_update(bot: MaxBotClient, upd: Dict[str, Any]) -> None:
    utype = upd.get("update_type")

    if utype == "bot_added":
        ch_id = upd.get("chat_id")
        user = upd.get("user", {})
        uid = user.get("user_id")
        uname = user.get("name", "Администратор")
        if ch_id:
            set_setting("channel_chat_id", str(ch_id))
            logger.info(f"Бот добавлен в канал chat_id={ch_id} пользователем {uid}")
        if uid:
            add_admin(uid, uname)
            await bot.send_message(
                "✅ Бот успешно подключён к вашему каналу **«Цена ШОК»**! Теперь вы можете выкладывать товары через панель управления ниже:",
                user_id=uid,
            )
            await show_admin_menu(bot, uid)
        return

    if utype == "bot_started":
        user = upd.get("user", {})
        user_id = user.get("user_id")
        user_name = user.get("name", "Покупатель")
        payload = (upd.get("payload") or "").strip()
        if not user_id:
            return

        if payload.startswith("book_"):
            try:
                pid = int(payload.split("_", 1)[1])
                await handle_booking_trigger(bot, user_id, user_name, pid)
                return
            except ValueError:
                pass
        saved_pending = get_setting(f"pending_book_{user_id}", "")
        if saved_pending.isdigit():
            set_setting(f"pending_book_{user_id}", "")
            await handle_booking_trigger(bot, user_id, user_name, int(saved_pending))
            return
        pending = USER_STATES.get(user_id, {})
        if pending.get("state") == "await_book_qty" and pending.get("product_id"):
            await handle_booking_trigger(bot, user_id, user_name, int(pending["product_id"]))
            return
        await show_user_cart(bot, user_id)
        if is_admin(user_id):
            await show_admin_menu(bot, user_id)
        return

    if utype == "message_callback":
        cb = upd.get("callback", {})
        cb_id = cb.get("callback_id")
        payload = cb.get("payload", "")
        user = cb.get("user", {})
        user_id = user.get("user_id")
        user_name = user.get("name", "Покупатель")
        user_username = user.get("username", "")
        if not user_id:
            return

        if payload.startswith("book:"):
            pid = int(payload.split(":", 1)[1])
            await handle_booking_trigger(bot, user_id, user_name, pid, callback_id=cb_id)
            return

        if payload.startswith("book_qty:"):
            _, pid_s, qty_s = payload.split(":")
            pid, qty = int(pid_s), int(qty_s)
            USER_STATES.pop(user_id, None)
            err = await reserve_product_for_user(bot, user_id, user_name, pid, qty)
            if err:
                await bot.answer_callback(cb_id, err)
                await bot.send_message(f"⚠️ {err}", user_id=user_id)
            else:
                await bot.answer_callback(cb_id, f"✅ Добавлено в корзину: {qty} шт.!")
                await bot.send_message(f"✅ Товар ({qty} шт.) добавлен в вашу корзину!", user_id=user_id)
                await show_user_cart(bot, user_id)
            return

        if payload == "open_cart":
            await bot.answer_callback(cb_id)
            await show_user_cart(bot, user_id)
            return

        if payload.startswith("cart_del:"):
            pid = int(payload.split(":", 1)[1])
            msg = await return_product_from_cart(bot, user_id, pid)
            await bot.answer_callback(cb_id, msg or "Удалено из корзины")
            if msg:
                await bot.send_message(f"🔄 {msg}", user_id=user_id)
            await show_user_cart(bot, user_id)
            return

        if payload == "cart_clear":
            await clear_user_cart_with_return(bot, user_id)
            await bot.answer_callback(cb_id, "Корзина очищена, товары возвращены в канал")
            await show_user_cart(bot, user_id)
            return

        if payload.startswith("checkout:"):
            mode = payload.split(":", 1)[1]
            summary = get_cart_summary(user_id)
            free_min = int(get_setting("free_delivery_min", "1500"))
            if not summary["items"]:
                await bot.answer_callback(cb_id, "Корзина пуста")
                return
            if mode == "delivery":
                if summary["total"] < free_min:
                    await bot.answer_callback(
                        cb_id,
                        f"Бесплатная доставка доступна при заказе от {free_min} руб.!",
                    )
                    return
                await bot.answer_callback(cb_id)
                open_dates = get_open_delivery_dates()
                if open_dates:
                    date_btns = [
                        [{"type": "callback", "text": f"📅 {d}", "payload": f"deliv_date:{idx}"}]
                        for idx, d in enumerate(open_dates)
                    ]
                    date_btns.append(
                        [{"type": "callback", "text": "💬 Согласовать дату с менеджером", "payload": "deliv_date:manager"}]
                    )
                    await bot.send_message(
                        text=(
                            "🚚 **Запись на бесплатную доставку**\n\n"
                            "Выберите удобную дату доставки на кнопке ниже:"
                        ),
                        user_id=user_id,
                        buttons=date_btns,
                    )
                    return

                USER_STATES[user_id] = {
                    "state": "await_delivery_address",
                    "user_name": user_name,
                    "user_username": user_username,
                    "chosen_date": "по согласованию с менеджером",
                }
                await bot.send_message(
                    text=(
                        "🚚 **Оформление бесплатной доставки**\n\n"
                        "Напишите в ответном сообщении **ваш точный адрес доставки и номер телефона для связи** (дату и время согласуем с менеджером):"
                    ),
                    user_id=user_id,
                )
                return
            else:
                await bot.answer_callback(cb_id)
                USER_STATES[user_id] = {
                    "state": "await_pickup_phone",
                    "user_name": user_name,
                    "user_username": user_username,
                }
                await bot.send_message(
                    text=(
                        "📍 **Оформление самовывоза**\n\n"
                        "Напишите в ответном сообщении **ваш номер телефона** (адрес самовывоза согласуем по связи с менеджером):"
                    ),
                    user_id=user_id,
                )
                return

        if payload.startswith("deliv_date:"):
            val = payload.split(":", 1)[1]
            open_dates = get_open_delivery_dates()
            if val == "manager" or not val.isdigit() or int(val) >= len(open_dates):
                chosen_date = "по согласованию с менеджером"
            else:
                chosen_date = open_dates[int(val)]
            await bot.answer_callback(cb_id, f"Выбрана дата: {chosen_date}")
            USER_STATES[user_id] = {
                "state": "await_delivery_address",
                "user_name": user_name,
                "user_username": user_username,
                "chosen_date": chosen_date,
            }
            await bot.send_message(
                text=(
                    f"📅 Выбрана доставка: **{chosen_date}**\n\n"
                    "Напишите в ответном сообщении **ваш точный адрес доставки и номер телефона для связи**:"
                ),
                user_id=user_id,
            )
            return

        if payload == "adm:new_prod" and is_admin(user_id):
            await bot.answer_callback(cb_id)
            add_admin(user_id, user_name)
            USER_STATES[user_id] = {"state": "adm_await_photo"}
            await bot.send_message(
                text=(
                    "📸 **Шаг 1 из 4: Фото товара**\n\n"
                    "Отправьте фотографию товара (можно сразу с подписью-названием):"
                ),
                user_id=user_id,
            )
            return

        if payload.startswith("adm_qty:") and is_admin(user_id):
            qty = int(payload.split(":", 1)[1])
            st = USER_STATES.get(user_id, {})
            if st.get("state") == "adm_await_qty":
                await bot.answer_callback(cb_id, f"Выбрано: {qty} шт.")
                await publish_new_product_from_admin(bot, user_id, st, qty)
            return

        if payload == "adm:list_prods" and is_admin(user_id):
            await bot.answer_callback(cb_id)
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT id, title, price, available_qty, total_qty FROM products ORDER BY id DESC LIMIT 15")
            rows = cur.fetchall()
            cur.execute(
                "SELECT c.user_name, p.title, c.qty FROM cart_items c JOIN products p ON p.id = c.product_id ORDER BY c.id DESC"
            )
            cart_rows = cur.fetchall()
            conn.close()
            if not rows:
                await bot.send_message("Товаров пока нет.", user_id=user_id)
                return
            lines = ["📦 **Последние товары:**\n"]
            btns = []
            for pid, title, price, avail, tot in rows:
                lines.append(f"• **#{pid} {title}** — {price} руб. (в наличии: **{avail}** из {tot} шт.)")
                btns.append(
                    [
                        {
                            "type": "callback",
                            "text": f"🔄 Вернуть +1 шт. в канал (#{pid} {title[:18]})",
                            "payload": f"adm_ret1:{pid}",
                        }
                    ]
                )
            if cart_rows:
                lines.append("\n🛒 **Сейчас лежит в корзинах (ещё не оформлено):**")
                for bname, ptitle, cqty in cart_rows:
                    lines.append(f"• {bname}: **{ptitle}** ({cqty} шт.)")
                btns.append(
                    [
                        {
                            "type": "callback",
                            "text": "🔄 Снять бронь из корзин и вернуть все в канал",
                            "payload": "adm:clear_all_carts",
                        }
                    ]
                )
            await bot.send_message("\n".join(lines), user_id=user_id, buttons=btns)
            return

        if payload == "adm:clear_all_carts" and is_admin(user_id):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT product_id, qty FROM cart_items")
            c_rows = cur.fetchall()
            cur.execute("DELETE FROM cart_items")
            pids_to_sync = set()
            for pid, qty in c_rows:
                cur.execute("UPDATE products SET available_qty = available_qty + ? WHERE id = ?", (qty, pid))
                pids_to_sync.add(int(pid))
            conn.commit()
            conn.close()
            for pid in pids_to_sync:
                await sync_channel_product_post(bot, pid)
            await bot.answer_callback(cb_id, "Неоформленные корзины очищены, товары возвращены в канал!")
            await bot.send_message(
                "✅ Все неоформленные брони из корзин сняты, товары снова опубликованы в канале «Цена ШОК»!",
                user_id=user_id,
            )
            return

        if payload.startswith("adm_unbook:") and is_admin(user_id):
            _, buyer_s, pid_s = payload.split(":")
            buyer_id, pid = int(buyer_s), int(pid_s)
            msg = await return_product_from_cart(bot, buyer_id, pid)
            if msg:
                await bot.answer_callback(cb_id, "Бронь снята, товар возвращён в канал!")
                await bot.send_message(f"✅ {msg}", user_id=user_id)
            else:
                await bot.answer_callback(cb_id, "Эта бронь уже была снята или оформлена в заказ.")
                await bot.send_message("ℹ️ Эта бронь уже была снята или переведена в оформленные заказы.", user_id=user_id)
            return

        if payload.startswith("adm_ret1:") and is_admin(user_id):
            pid = int(payload.split(":", 1)[1])
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("UPDATE products SET available_qty = available_qty + 1 WHERE id = ?", (pid,))
            conn.commit()
            conn.close()
            await sync_channel_product_post(bot, pid)
            await bot.answer_callback(cb_id, "Товар (+1 шт.) возвращён и обновлён в канале!")
            await bot.send_message(f"✅ Товар #{pid} (+1 шт.) возвращён в канал «Цена ШОК».", user_id=user_id)
            return

        if payload == "adm:orders" and is_admin(user_id):
            await bot.answer_callback(cb_id)
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT id, user_name, phone, delivery_address, total_amount, status FROM orders ORDER BY id DESC LIMIT 10"
            )
            rows = cur.fetchall()
            conn.close()
            if not rows:
                await bot.send_message("Заказов пока нет.", user_id=user_id)
                return
            for oid, bname, phone, addr, tot, st_val in rows:
                status_str = {"new": "🟢 Активен", "done": "✅ Выдан", "cancelled": "🔄 Отменён (возвращён в канал)"}.get(
                    st_val, st_val
                )
                b_list = []
                if st_val == "new":
                    b_list = [
                        [{"type": "callback", "text": f"✅ Выдан (#{oid})", "payload": f"ord_done:{oid}"}],
                        [
                            {
                                "type": "callback",
                                "text": f"🔄 Отказ — вернуть товары #{oid} в канал",
                                "payload": f"ord_cancel:{oid}",
                            }
                        ],
                    ]
                await bot.send_message(
                    text=(
                        f"📋 **Заказ №{oid}** ({status_str})\n"
                        f"👤 Покупатель: {bname} | 📞 {phone}\n"
                        f"📍 Получение: {addr}\n"
                        f"💰 Сумма: **{tot} руб.**"
                    ),
                    user_id=user_id,
                    buttons=b_list,
                )
            return

        if payload.startswith("ord_done:") and is_admin(user_id):
            oid = int(payload.split(":", 1)[1])
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("UPDATE orders SET status = 'done' WHERE id = ?", (oid,))
            conn.commit()
            conn.close()
            await bot.answer_callback(cb_id, f"Заказ №{oid} отмечен как выданный!")
            await bot.send_message(f"✅ Заказ №{oid} успешно закрыт (выдан клиенту).", user_id=user_id)
            return

        if payload.startswith("ord_cancel:") and is_admin(user_id):
            oid = int(payload.split(":", 1)[1])
            await bot.answer_callback(cb_id, f"Возвращаем товары заказа №{oid} в канал...")
            await cancel_order_and_restore_to_channel(bot, oid, user_id)
            return

        if payload == "adm:set_free_min" and is_admin(user_id):
            await bot.answer_callback(cb_id)
            USER_STATES[user_id] = {"state": "adm_await_free_min"}
            cur_min = get_setting("free_delivery_min", "1500")
            await bot.send_message(
                text=(
                    f"🚚 **Настройка порога бесплатной доставки** (сейчас: **от {cur_min} руб.**)\n\n"
                    "Выберите готовую сумму кнопкой ниже или **напишите любое число в рублях** в ответном сообщении (например `1500`):"
                ),
                user_id=user_id,
                buttons=[
                    [
                        {"type": "callback", "text": "1000 руб.", "payload": "adm_free_val:1000"},
                        {"type": "callback", "text": "1500 руб.", "payload": "adm_free_val:1500"},
                        {"type": "callback", "text": "2000 руб.", "payload": "adm_free_val:2000"},
                    ],
                    [
                        {"type": "callback", "text": "2500 руб.", "payload": "adm_free_val:2500"},
                        {"type": "callback", "text": "3000 руб.", "payload": "adm_free_val:3000"},
                    ],
                ],
            )
            return

        if payload.startswith("adm_free_val:") and is_admin(user_id):
            val = payload.split(":", 1)[1].strip()
            set_setting("free_delivery_min", val)
            USER_STATES.pop(user_id, None)
            await bot.answer_callback(cb_id, f"Бесплатная доставка: от {val} руб.")
            await bot.send_message(f"✅ Порог бесплатной доставки обновлён: **от {val} руб.**", user_id=user_id)
            await show_admin_menu(bot, user_id)
            return

        if payload == "adm:set_deliv_dates" and is_admin(user_id):
            await bot.answer_callback(cb_id)
            USER_STATES[user_id] = {"state": "adm_await_deliv_dates"}
            await bot.send_message(
                text=(
                    "📅 **Настройка открытых дат доставки**\n\n"
                    "Напишите доступные для записи даты и интервалы через запятую (например: `Пт 6 ноября 18:00-21:00, Сб 7 ноября 12:00-16:00`), "
                    "и покупатели смогут выбирать их по кнопкам при оформлении корзины.\n\n"
                    "Или нажмите кнопку ниже, чтобы дата доставки согласовывалась лично с вами (менеджером):"
                ),
                user_id=user_id,
                buttons=[
                    [
                        {
                            "type": "callback",
                            "text": "💬 Согласовывать лично с менеджером (без кнопок дат)",
                            "payload": "adm:clear_deliv_dates",
                        }
                    ]
                ],
            )
            return

        if payload == "adm:clear_deliv_dates" and is_admin(user_id):
            set_setting("delivery_dates", "")
            USER_STATES.pop(user_id, None)
            await bot.answer_callback(cb_id, "Режим: согласование даты доставки лично с менеджером")
            await bot.send_message(
                "✅ Открытые даты отключены. Теперь при оформлении доставки время согласуется лично с менеджером.",
                user_id=user_id,
            )
            await show_admin_menu(bot, user_id)
            return

    if utype == "message_created":
        msg = upd.get("message", {})
        recipient = msg.get("recipient", {})
        if recipient.get("chat_type") == "channel":
            ch_id = recipient.get("chat_id")
            if ch_id:
                set_setting("channel_chat_id", str(ch_id))
            return

        sender = msg.get("sender", {})
        user_id = sender.get("user_id")
        user_name = sender.get("name", "Покупатель")
        user_username = sender.get("username", "")
        body = msg.get("body", {})
        text = (body.get("text") or "").strip()
        attachments = body.get("attachments") or []

        if not user_id:
            return

        if text.lower() in ("/admin", "админ", "панель"):
            add_admin(user_id, user_name)
            USER_STATES.pop(user_id, None)
            await show_admin_menu(bot, user_id)
            return

        if text.lower() in ("/start", "корзина", "моя корзина"):
            USER_STATES.pop(user_id, None)
            await show_user_cart(bot, user_id)
            if is_admin(user_id):
                await show_admin_menu(bot, user_id)
            return

        st = USER_STATES.get(user_id)
        if st:
            state_name = st.get("state")

            if state_name == "await_book_qty":
                try:
                    qty = int(text)
                    if qty <= 0:
                        raise ValueError
                except ValueError:
                    await bot.send_message("Пожалуйста, введите количество цифрой (например: `2`):", user_id=user_id)
                    return
                pid = int(st["product_id"])
                err = await reserve_product_for_user(bot, user_id, user_name, pid, qty)
                if err:
                    await bot.send_message(f"⚠️ {err}", user_id=user_id)
                    return
                USER_STATES.pop(user_id, None)
                await bot.send_message(f"✅ Товар ({qty} шт.) забронирован и добавлен в вашу корзину!", user_id=user_id)
                await show_user_cart(bot, user_id)
                return

            if state_name == "await_delivery_address":
                if len(text) < 4:
                    await bot.send_message("Пожалуйста, укажите адрес доставки и телефон:", user_id=user_id)
                    return
                chosen_date = st.get("chosen_date", "по согласованию с менеджером")
                full_delivery_addr = f"{text} (Дата доставки: {chosen_date})"
                USER_STATES.pop(user_id, None)
                await finish_checkout(
                    bot,
                    user_id=user_id,
                    user_name=user_name,
                    user_username=user_username,
                    delivery_type="delivery",
                    delivery_address=full_delivery_addr,
                    phone_and_comment=text,
                )
                return

            if state_name == "await_pickup_phone":
                if len(text) < 4:
                    await bot.send_message("Пожалуйста, укажите ваш номер телефона для связи:", user_id=user_id)
                    return
                USER_STATES.pop(user_id, None)
                await finish_checkout(
                    bot,
                    user_id=user_id,
                    user_name=user_name,
                    user_username=user_username,
                    delivery_type="pickup",
                    delivery_address="Самовывоз (адрес по связи с менеджером)",
                    phone_and_comment=text,
                )
                return

            if state_name == "adm_await_photo":
                photo_payload = None
                for att in attachments:
                    if att.get("type") == "image":
                        photo_payload = att.get("payload")
                        break
                st["photo_payload"] = photo_payload
                if text:
                    st["title"] = text
                    st["state"] = "adm_await_price"
                    await bot.send_message(
                        "💰 **Шаг 3 из 4: Цена товара**\n\nВведите цену за 1 шт. в рублях (только число, например `450`):",
                        user_id=user_id,
                    )
                else:
                    st["state"] = "adm_await_title"
                    await bot.send_message(
                        "📝 **Шаг 2 из 4: Название и описание**\n\nВведите название и описание товара:",
                        user_id=user_id,
                    )
                return

            if state_name == "adm_await_title":
                if not text:
                    await bot.send_message("Введите название и описание товара текстом:", user_id=user_id)
                    return
                st["title"] = text
                st["state"] = "adm_await_price"
                await bot.send_message(
                    "💰 **Шаг 3 из 4: Цена товара**\n\nВведите цену за 1 шт. в рублях (только число, например `450`):",
                    user_id=user_id,
                )
                return

            if state_name == "adm_await_price":
                digits = "".join(ch for ch in text if ch.isdigit())
                if not digits:
                    await bot.send_message("Введите цену числом (например `450`):", user_id=user_id)
                    return
                st["price"] = int(digits)
                st["state"] = "adm_await_qty"
                await bot.send_message(
                    text=(
                        "📦 **Шаг 4 из 4: Количество в наличии**\n\n"
                        "Выберите количество на кнопке или напишите любое число штук в ответном сообщении:"
                    ),
                    user_id=user_id,
                    buttons=[
                        [
                            {"type": "callback", "text": "1 шт.", "payload": "adm_qty:1"},
                            {"type": "callback", "text": "2 шт.", "payload": "adm_qty:2"},
                            {"type": "callback", "text": "3 шт.", "payload": "adm_qty:3"},
                            {"type": "callback", "text": "5 шт.", "payload": "adm_qty:5"},
                            {"type": "callback", "text": "10 шт.", "payload": "adm_qty:10"},
                        ]
                    ],
                )
                return

            if state_name == "adm_await_qty":
                digits = "".join(ch for ch in text if ch.isdigit())
                if not digits or int(digits) <= 0:
                    await bot.send_message("Введите количество штук числом (например `1` или `5`):", user_id=user_id)
                    return
                await publish_new_product_from_admin(bot, user_id, st, int(digits))
                return

            if state_name == "adm_await_free_min":
                digits = "".join(ch for ch in text if ch.isdigit())
                if not digits:
                    await bot.send_message("Введите сумму числом (например `1000`):", user_id=user_id)
                    return
                set_setting("free_delivery_min", digits)
                USER_STATES.pop(user_id, None)
                await bot.send_message(f"✅ Порог бесплатной доставки обновлён: **от {digits} руб.**", user_id=user_id)
                await show_admin_menu(bot, user_id)
                return

            if state_name == "adm_await_deliv_dates":
                if text.strip() in ("0", "-", "нет", "Нет"):
                    set_setting("delivery_dates", "")
                    USER_STATES.pop(user_id, None)
                    await bot.send_message(
                        "✅ Открытые даты отключены. Теперь дата доставки согласуется лично с менеджером.",
                        user_id=user_id,
                    )
                else:
                    set_setting("delivery_dates", text.strip())
                    USER_STATES.pop(user_id, None)
                    open_dates = get_open_delivery_dates()
                    await bot.send_message(
                        f"✅ Открытые даты для записи на доставку сохранены:\n"
                        + "\n".join([f"• **{d}**" for d in open_dates]),
                        user_id=user_id,
                    )
                await show_admin_menu(bot, user_id)
                return

        if is_admin(user_id):
            only_digits = "".join(ch for ch in text if ch.isdigit())
            if only_digits and (text.strip().isdigit() or "достав" in text.lower()) and int(only_digits) >= 100:
                set_setting("free_delivery_min", only_digits)
                await bot.send_message(
                    f"✅ Порог бесплатной доставки обновлён: **от {only_digits} руб.**",
                    user_id=user_id,
                )
                await show_admin_menu(bot, user_id)
                return

        await show_user_cart(bot, user_id)
        if is_admin(user_id):
            await show_admin_menu(bot, user_id)


async def publish_new_product_from_admin(
    bot: MaxBotClient,
    admin_id: int,
    st: Dict[str, Any],
    qty: int,
) -> None:
    USER_STATES.pop(admin_id, None)
    title = st.get("title", "Товар")
    price = int(st.get("price", 0))
    photo_payload = st.get("photo_payload")
    ch_id = await bot.detect_channel_id()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO products (title, price, total_qty, available_qty, photo_payload, channel_chat_id, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            title,
            price,
            qty,
            qty,
            json.dumps(photo_payload, ensure_ascii=False) if photo_payload else None,
            ch_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    product_id = cur.lastrowid
    conn.commit()
    conn.close()

    await sync_channel_product_post(bot, product_id)
    await bot.send_message(
        text=(
            f"✅ **Товар #{product_id} опубликован в канале «Цена ШОК»!**\n\n"
            f"📦 **{title}**\n"
            f"💰 Цена: **{price} руб.** | В наличии: **{qty} шт.**"
        ),
        user_id=admin_id,
    )
    await show_admin_menu(bot, admin_id)


async def main() -> None:
    init_db()
    if not MAX_BOT_TOKEN:
        logger.error("Не задан MAX_BOT_TOKEN в .env!")
        return

    bot = MaxBotClient(MAX_BOT_TOKEN)
    await bot.start()
    ch_id = await bot.detect_channel_id()
    logger.info(f"Активный канал Цена ШОК: chat_id={ch_id}")
    logger.info("Запущена версия v6: бесплатная доставка от 1500 руб + кнопки быстрой смены порога")

    # Устанавливаем порог 1500 руб., возвращаем зависшие неоформленные корзины и обновляем активные посты
    if get_setting("notified_v6_free_1500", "0") != "1":
        set_setting("notified_v6_free_1500", "1")
        set_setting("free_delivery_min", "1500")
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT product_id, qty FROM cart_items")
        stuck_carts = cur.fetchall()
        cur.execute("DELETE FROM cart_items")
        for pid, qty in stuck_carts:
            cur.execute("UPDATE products SET available_qty = available_qty + ? WHERE id = ?", (qty, pid))
        conn.commit()
        cur.execute("SELECT id FROM products WHERE is_active = 1 AND available_qty > 0")
        active_pids = [r[0] for r in cur.fetchall()]
        conn.close()
        for pid in active_pids:
            try:
                await sync_channel_product_post(bot, pid)
            except Exception as e:
                logger.warning(f"Не удалось обновить пост #{pid}: {e}")
        try:
            await bot.send_message(
                "✅ Порог бесплатной доставки обновлён: **от 1500 руб.**",
                user_id=24725732,
            )
            await show_admin_menu(bot, 24725732)
        except Exception:
            pass

    marker: Optional[int] = None
    logger.info("Запущен Long Polling обновлений MAX API...")
    try:
        while True:
            params: Dict[str, Any] = {
                "timeout": 25,
                "types": "message_created,message_callback,bot_started,bot_added",
            }
            if marker is not None:
                params["marker"] = marker
            resp = await bot.request("GET", "/updates", params=params)
            if resp:
                if "marker" in resp and resp["marker"] is not None:
                    marker = resp["marker"]
                for upd in resp.get("updates", []):
                    try:
                        await process_update(bot, upd)
                    except Exception as e:
                        logger.exception(f"Ошибка обработки события: {e}")
            else:
                await asyncio.sleep(2)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
