# ================= IMPORTS =================
from flask import Flask, request
import os
import time
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Dispatcher,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext
)

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID"))
PAYPAL_BASE_LINK = "https://www.paypal.com/paypalme/Chaschni?country.x=DE&locale.x=de_DE"
CONTACT_USERNAME = "Chaschni"
CUTLERY_PRICE = 0.30
MAX_DAILY = 15

TIMEZONE = ZoneInfo("Europe/Berlin")

ENABLE_TIME_LIMIT = True
TEST_MODE = False

WORK_DAYS = {0, 3}
START_HOUR = 12
END_HOUR = 18
EMERGENCY_MESSAGE = None

# ================= PRESALE LOGIC =================
def presale_status():
    """
    خروجی:
    (True, "پنجشنبه") یا (True, "دوشنبه")
    (False, None) اگر پیش‌سفارش بسته است
    """
    now = datetime.now(TIMEZONE)
    wd = now.weekday()  # Mon=0 ... Sun=6

    # سه‌شنبه (1) و چهارشنبه (2) → پنجشنبه
    if wd in [1, 2]:
        return True, "پنجشنبه"

    # جمعه (4)، شنبه (5)، یکشنبه (6) → دوشنبه
    if wd in [4, 5, 6]:
        return True, "دوشنبه"

    return False, None

# ---------- DELIVERY ----------
DELIVERY_POSTCODES = ["30163"]
LOCAL_STREETS_30165 = [
    "Melanchthonstrasse","Moorkamp","Gutsmuthsstrasse","Auf dem Hollen","Jahnplatz",
    "Dragonerstrasse","Halkettstrasse","Omptedastrasse","Almannstrasse",
    "Apenraderstrasse","Flensburgerstrasse","Schleswigerstrasse",
    "Tondernerstrasse","Sonderburgerstrasse","Rotermondstrasse"
]

PICKUP_ADDRESS_FULL = "Tannenbergallee 6, 30163 Hannover"
PICKUP_ADDRESS_SHORT = "Tannenbergallee (Hannover)"

# ---------- DB ----------
conn = sqlite3.connect("orders.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT,
    user_id INTEGER,
    food_key TEXT,
    food_name TEXT,
    qty INTEGER,
    cutlery_qty INTEGER,
    total REAL,
    status TEXT,
    payment_method TEXT,
    created_at TEXT,
    payment_checked_at TEXT
)
""")
conn.commit()

# ---------- UTILITY ----------
user_state = {}
orders_runtime = {}

# ---------- ANTI-SPAM ----------
user_last_msgs = {}
user_msg_count = {}
SPAM_WINDOW = 4
SPAM_LIMIT = 5

def reset_user(uid):
    user_state.pop(uid, None)

def normalize_digits(text):
    persian = "۰۱۲۳۴۵۶۷۸۹"
    english = "0123456789"
    for p, e in zip(persian, english):
        text = text.replace(p, e)
    return text.strip()

# ---------- MENU BASED ON DAY ----------
def get_today_foods():
    day = datetime.now(TIMEZONE).weekday()

    if TEST_MODE:
        return {
            "farani": {"name": "🍮 فرنی", "price": 3.5},
            "salad": {"name": "🥗 سالاد ماکارونی", "price": 5},
            "ash": {"name": "🍲 آش رشته", "price": 6},
            "ghorme": {"name": "🍛 قورمه سبزی", "price": 8.5},
            "zereshk": {"name": "🍗 زرشک پلو با مرغ", "price": 9.5},
        }

    if day == 0:
        return {
            "farani": {"name": "🍮 فرنی", "price": 3.5},
            "salad": {"name": "🥗 سالاد ماکارونی", "price": 5},
            "ash": {"name": "🍲 آش رشته", "price": 6},
            "ghorme": {"name": "🍛 قورمه سبزی", "price": 8.5},
        }

    if day == 3:
        return {
            "farani": {"name": "🍮 فرنی", "price": 3.5},
            "salad": {"name": "🥗 سالاد ماکارونی", "price": 5},
            "ash": {"name": "🍲 آش رشته", "price": 6},
            "zereshk": {"name": "🍗 زرشک پلو با مرغ", "price": 9.5},
        }

    return {}

# ---------- KEYBOARDS ----------
def persistent_menu():
    return ReplyKeyboardMarkup(
        [["🍽 شروع سفارش"], ["❌ لغو سفارش", "📞 تماس با ما"]],
        resize_keyboard=True
    )

def food_keyboard():
    foods = get_today_foods()
    buttons = []
    for k, f in foods.items():
        buttons.append([InlineKeyboardButton(f"{f['name']} — {f['price']}€", callback_data=f"food_{k}")])
    return InlineKeyboardMarkup(buttons)

def admin_keyboard(order_no):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تأیید", callback_data=f"admin_ok_{order_no}"),
            InlineKeyboardButton("❌ لغو", callback_data=f"admin_cancel_{order_no}")
        ]
    ])

def pickup_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("بله ادامه بده", callback_data="pickup_yes"),
            InlineKeyboardButton("لغو سفارش", callback_data="pickup_no")
        ]
    ])

# ---------- COMMANDS ----------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 خوش آمدید!\n"
        "🍽 این بات فقط پیش‌سفارش می‌گیرد\n"
        "📦 ارسال فقط دوشنبه و پنجشنبه",
        reply_markup=persistent_menu()
    )

# ---------- TEXT HANDLER ----------
def handle_text(update: Update, context: CallbackContext):
    global EMERGENCY_MESSAGE
    uid = update.effective_user.id
    text = update.message.text
    st = user_state.get(uid)

    # ---------- START ORDER ----------
    if text == "🍽 شروع سفارش":
        allowed, delivery_day = presale_status()
        if not allowed:
            update.message.reply_text(
                "⛔️ در حال حاضر پیش‌سفارش فعال نیست.\n\n"
                "🗓 پیش‌سفارش پنجشنبه:\n"
                "سه‌شنبه و چهارشنبه\n\n"
                "🗓 پیش‌سفارش دوشنبه:\n"
                "جمعه تا یکشنبه"
            )
            return

        user_state[uid] = {"delivery_day": delivery_day}
        update.message.reply_text(
            f"📦 سفارش شما برای ارسال در روز «{delivery_day}» ثبت می‌شود.\n\n📋 منوی موجود:",
            reply_markup=food_keyboard()
        )
        return

    # --------- بقیه کد شما بدون تغییر ---------
    # (همان کدی که خودت فرستادی، بدون حتی یک خط حذف)
    # ------------------------------------------------
