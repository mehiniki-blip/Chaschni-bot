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

# ----- DELIVERY WINDOWS -----
DELIVERY_START_HOUR = 12
DELIVERY_END_HOUR = 17
SLOT_MINUTES = 30
SLOT_CAPACITY = 4

# runtime slot usage
slot_usage = {}  # { "2024-01-22_12:00-12:30": count }

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

# ---------- STATE ----------
user_state = {}

# ---------- UTILS ----------
def reset_user(uid):
    user_state.pop(uid, None)

def normalize_digits(text):
    persian = "۰۱۲۳۴۵۶۷۸۹"
    english = "0123456789"
    for p, e in zip(persian, english):
        text = text.replace(p, e)
    return text.strip()

def get_delivery_day_from_today():
    if TEST_MODE:
        return datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    today = datetime.now(TIMEZONE).weekday()
    # 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
    if today in [4,5,6]:  # Fri Sat Sun → Monday
        delta = (7 - today) % 7
        return (datetime.now(TIMEZONE) + timedelta(days=delta)).strftime("%Y-%m-%d")
    if today in [1,2]:  # Tue Wed → Thursday
        delta = (3 - today)
        return (datetime.now(TIMEZONE) + timedelta(days=delta)).strftime("%Y-%m-%d")
    return None

def ordering_allowed():
    return TEST_MODE or get_delivery_day_from_today() is not None

def generate_slots(delivery_date):
    slots = []
    start = datetime.strptime(delivery_date + f" {DELIVERY_START_HOUR}:00", "%Y-%m-%d %H:%M")
    end = datetime.strptime(delivery_date + f" {DELIVERY_END_HOUR}:00", "%Y-%m-%d %H:%M")
    while start < end:
        s = start.strftime("%H:%M")
        e = (start + timedelta(minutes=SLOT_MINUTES)).strftime("%H:%M")
        key = f"{delivery_date}_{s}-{e}"
        if slot_usage.get(key, 0) < SLOT_CAPACITY:
            slots.append((key, f"{s} – {e}"))
        start += timedelta(minutes=SLOT_MINUTES)
    return slots

# ---------- KEYBOARDS ----------
def persistent_menu():
    return ReplyKeyboardMarkup(
        [["🍽 شروع سفارش"], ["❌ لغو سفارش", "📞 تماس با ما"]],
        resize_keyboard=True
    )

def slot_keyboard(slots):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"slot_{key}")]
        for key, label in slots
    ])

# ---------- START ----------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 به Chaschni خوش آمدید!\n\n"
        "🍽 سرویس ما به صورت پیش‌سفارش انجام می‌شود.\n"
        "📦 سفارش‌ها در طول هفته ثبت می‌شوند\n"
        "🚚 و تحویل غذا فقط در روزهای دوشنبه و پنج‌شنبه انجام می‌گیرد.\n\n"
        "برای شروع سفارش، لطفاً از منوی زیر استفاده کنید.",
        reply_markup=persistent_menu()
    )

# ---------- CALLBACKS ----------
def callbacks(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    q.answer()

    st = user_state.get(uid)

    if q.data.startswith("slot_"):
        slot_key = q.data.replace("slot_", "")
        slot_usage[slot_key] = slot_usage.get(slot_key, 0) + 1
        st["delivery_slot"] = slot_key
        st["step"] = "pay"

        update.callback_query.edit_message_text(
            f"🕒 بازه انتخاب شد:\n{slot_key.split('_')[1]}\n\n"
            "💳 لطفاً پرداخت را انجام دهید."
        )

        total = st["food_total"] + (st["cutlery_qty"] * CUTLERY_PRICE)
        st["total"] = total

        context.bot.send_message(
            uid,
            f"💶 مبلغ نهایی: €{total}\n"
            "📌 به دلیل پیش‌سفارشی بودن سرویس، ثبت سفارش پس از پرداخت نهایی می‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 پرداخت PayPal", url=f"{PAYPAL_BASE_LINK}/{total}")],
                [InlineKeyboardButton("✔️ پرداخت انجام شد", callback_data="paid_paypal")]
            ])
        )
        return

# ---------- TEXT ----------
def handle_text(update: Update, context: CallbackContext):
    global TEST_MODE, EMERGENCY_MESSAGE

    uid = update.effective_user.id
    text = update.message.text
    st = user_state.get(uid)

    if EMERGENCY_MESSAGE and text == "🍽 شروع سفارش":
        update.message.reply_text(EMERGENCY_MESSAGE)
        return

    if uid == ADMIN_CHAT_ID and "غیر" in text and "تست" in text:
        TEST_MODE = False
        update.message.reply_text("⚪ حالت تست غیرفعال شد")
        return

    if uid == ADMIN_CHAT_ID and "فعال" in text and "تست" in text:
        TEST_MODE = True
        update.message.reply_text("🔵 حالت تست فعال شد")
        return

    if text == "🍽 شروع سفارش":
        if not ordering_allowed():
            update.message.reply_text("⛔ در حال حاضر سفارش‌گیری فعال نیست.")
            return

        delivery_day = get_delivery_day_from_today()
        user_state[uid] = {"step": "slot", "delivery_day": delivery_day}

        slots = generate_slots(delivery_day)
        if not slots:
            update.message.reply_text("⛔ ظرفیت تحویل تکمیل شده است.")
            reset_user(uid)
            return

        update.message.reply_text(
            f"📦 روز تحویل: {delivery_day}\n"
            "🕒 لطفاً بازه زمانی تحویل را انتخاب کنید:",
            reply_markup=slot_keyboard(slots)
        )
        return

    if text == "❌ لغو سفارش":
        reset_user(uid)
        update.message.reply_text("سفارش لغو شد.", reply_markup=persistent_menu())
        return

    if text == "📞 تماس با ما":
        update.message.reply_text(
            "ارتباط مستقیم:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 چت تلگرام", url=f"https://t.me/{CONTACT_USERNAME}")]
            ])
        )
        return

# ----------- WEBHOOK MODE -----------
app = Flask(__name__)
dp = None
bot = Bot(BOT_TOKEN)

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook_handler():
    global dp
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return "OK", 200

@app.route("/")
def home():
    return "Bot is running!", 200

def main():
    global dp
    dp = Dispatcher(bot, None, workers=0)

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(callbacks))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

    bot.set_webhook(f"https://chaschni-bot.onrender.com/{BOT_TOKEN}")

    port = int(os.environ.get("PORT", 8443))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
