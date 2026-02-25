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
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
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

try:
    cur.execute("ALTER TABLE orders ADD COLUMN delivery_day TEXT")
except sqlite3.OperationalError:
    pass

try:
    cur.execute("ALTER TABLE orders ADD COLUMN delivery_slot TEXT")
except sqlite3.OperationalError:
    pass

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
    payment_checked_at TEXT,
    delivery_day TEXT,
    delivery_slot TEXT
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

def is_working_time():
    if TEST_MODE or not ENABLE_TIME_LIMIT:
        return True

    today = datetime.now(TIMEZONE).weekday()
    if today in [1, 2, 4, 5, 6]:
        return True
    return False

def get_target_delivery_day():
    day = datetime.now(TIMEZONE).weekday()
    if day in [1, 2]:
        return "thursday"
    if day in [4, 5, 6]:
        return "monday"
    return None

def create_order(user_id, food_key, food_name, qty, total, cutlery_qty, payment_method, delivery_day, delivery_slot):
    from random import randint
    today = datetime.now(TIMEZONE).strftime("%Y%m%d")
    order_no = f"CH-{today}-{randint(100,999)}"

    cur.execute("""
        INSERT INTO orders
        (order_no, user_id, food_key, food_name, qty, cutlery_qty, total,
         status, payment_method, created_at, delivery_day, delivery_slot)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
    """, (
        order_no,
        user_id,
        food_key,
        food_name,
        qty,
        cutlery_qty,
        total,
        payment_method,
        datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
        delivery_day,
        delivery_slot
    ))
    conn.commit()
    return order_no

def close_order(order_no, status):
    cur.execute("""
        UPDATE orders SET status=?, payment_checked_at=?
        WHERE order_no=?
    """, (
        status,
        datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
        order_no
    ))
    conn.commit()

# ---------- MENU ----------
def get_foods_for_target_day():
    target = get_target_delivery_day()
    if target == "monday":
        return {
            "farani": {"name": "🍮 فرنی", "price": 3.5},
            "salad": {"name": "🥗 سالاد ماکارونی", "price": 5},
            "ash": {"name": "🍲 آش رشته", "price": 6},
            "ghorme": {"name": "🍛 قرمه سبزی", "price": 8.5},
        }
    if target == "thursday":
        return {
            "farani": {"name": "🍮 فرنی", "price": 3.5},
            "salad": {"name": "🥗 سالاد ماکارونی", "price": 5},
            "ash": {"name": "🍲 آش رشته", "price": 6},
            "zereshk": {"name": "🍗 زرشک پلو با مرغ", "price": 9.5},
        }
    return {}

def delivery_slot_keyboard():
    buttons = []
    hour, minute = START_HOUR, 0
    while hour < END_HOUR:
        start = f"{hour:02d}:{minute:02d}"
        minute += 30
        if minute == 60:
            hour += 1
            minute = 0
        end = f"{hour:02d}:{minute:02d}"
        buttons.append([InlineKeyboardButton(f"⏰ {start} – {end}", callback_data=f"slot_{start}_{end}")])
    return InlineKeyboardMarkup(buttons)

# ---------- WEBHOOK ----------
app = Flask(__name__)
bot = Bot(BOT_TOKEN)
dp = None

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook_handler():
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return "OK", 200

@app.route("/")
def home():
    return "Bot is running!", 200

def main():
    global dp
    dp = Dispatcher(bot, None, workers=0)

    bot.delete_webhook(drop_pending_updates=True)
    bot.set_webhook(f"https://chaschni-bot.onrender.com/{BOT_TOKEN}")

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(callbacks))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

    port = int(os.environ.get("PORT", 8443))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
