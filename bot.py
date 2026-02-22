from flask import Flask, request
import os
import time
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
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
TEST_MODE = False
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
    created_at TEXT
)
""")
conn.commit()

# ---------- RUNTIME ----------
user_state = {}
orders_runtime = {}

# ---------- TIME / PRESALE LOGIC ----------
def get_presale_info():
    if TEST_MODE:
        return True, "test"

    now = datetime.now(TIMEZONE)
    wd = now.weekday()  # Mon=0 ... Sun=6

    if wd in [1, 2] or wd == 3:  # Tue, Wed, Thu
        return True, "thursday"

    if wd in [4, 5, 6] or wd == 0:  # Fri, Sat, Sun, Mon
        return True, "monday"

    return False, None

# ---------- MENU ----------
def get_foods_for_delivery(day):
    if day in ["monday", "test"]:
        return {
            "farani": {"name": "🍮 فرنی", "price": 3.5},
            "salad": {"name": "🥗 سالاد ماکارونی", "price": 5},
            "ash": {"name": "🍲 آش رشته", "price": 6},
            "ghorme": {"name": "🍛 قورمه سبزی", "price": 8.5},
        }

    if day == "thursday":
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

def food_keyboard(day):
    foods = get_foods_for_delivery(day)
    buttons = []
    for k, f in foods.items():
        buttons.append([
            InlineKeyboardButton(
                f"{f['name']} — {f['price']}€",
                callback_data=f"food_{k}"
            )
        ])
    return InlineKeyboardMarkup(buttons)

# ---------- UTILS ----------
def normalize_digits(text):
    persian = "۰۱۲۳۴۵۶۷۸۹"
    english = "0123456789"
    for p, e in zip(persian, english):
        text = text.replace(p, e)
    return text.strip()

def reset_user(uid):
    user_state.pop(uid, None)

def create_order(st):
    from random import randint
    today = datetime.now(TIMEZONE).strftime("%Y%m%d")
    order_no = f"CH-{today}-{randint(100,999)}"

    cur.execute("""
        INSERT INTO orders
        (order_no, user_id, food_key, food_name, qty, cutlery_qty, total, status, payment_method, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """, (
        order_no,
        st["user_id"],
        st["food_key"],
        st["food_name"],
        st["qty"],
        st.get("cutlery_qty", 0),
        st["total"],
        st["payment_method"],
        datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")
    ))
    conn.commit()
    return order_no

# ---------- START ----------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 خوش آمدید!\n"
        "🍽 این بات فقط پیش‌سفارش می‌گیرد\n"
        "📦 ارسال: دوشنبه و پنجشنبه",
        reply_markup=persistent_menu()
    )

# ---------- CALLBACKS ----------
def callbacks(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    q.answer()
    st = user_state.get(uid)

    if q.data.startswith("food_"):
        key = q.data.replace("food_", "")
        foods = get_foods_for_delivery(st["delivery_day"])
        f = foods[key]

        st.update({
            "food_key": key,
            "food_name": f["name"],
            "price": f["price"],
            "step": "qty"
        })

        q.edit_message_text("📦 تعداد را وارد کنید:")
        return

    if q.data in ["paid_paypal", "paid_cash"]:
        st["payment_method"] = "PayPal" if q.data == "paid_paypal" else "Cash"
        order_no = create_order(st)

        context.bot.send_message(
            uid,
            f"✅ پیش‌سفارش ثبت شد\n"
            f"🧾 شماره: {order_no}\n"
            f"📦 ارسال: { 'دوشنبه' if st['delivery_day']=='monday' else 'پنجشنبه' }"
        )
        reset_user(uid)

# ---------- TEXT HANDLER ----------
def handle_text(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    text = update.message.text
    st = user_state.get(uid)

    if text == "🍽 شروع سفارش":
        allowed, delivery_day = get_presale_info()
        if not allowed:
            update.message.reply_text("⛔️ پیش‌سفارش فعال نیست")
            return

        user_state[uid] = {
            "user_id": uid,
            "delivery_day": delivery_day
        }

        update.message.reply_text(
            f"📋 منوی ارسال { 'دوشنبه' if delivery_day=='monday' else 'پنجشنبه' }:",
            reply_markup=food_keyboard(delivery_day)
        )
        return

    if not st:
        return

    if st.get("step") == "qty":
        qty = int(normalize_digits(text))
        st["qty"] = qty
        st["food_total"] = qty * st["price"]
        st["cutlery_qty"] = 0
        st["total"] = st["food_total"]
        st["step"] = "pay"

        update.message.reply_text(
            f"💶 مبلغ: €{st['total']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✔️ پرداخت انجام شد", callback_data="paid_paypal")]
            ])
        )

# ---------- WEB ----------
app = Flask(__name__)
bot = Bot(BOT_TOKEN)
dp = Dispatcher(bot, None, workers=0)

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return "OK"

def main():
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(callbacks))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    bot.set_webhook(f"https://chaschni-bot.onrender.com/{BOT_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8443)))

if __name__ == "__main__":
    main()
