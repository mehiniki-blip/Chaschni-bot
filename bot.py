from flask import Flask, request
import os
import time
import threading
import sqlite3
import uuid
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

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
CHANNEL_USERNAME = "@Chaschnii"
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID"))
PAYPAL_BASE_LINK = "https://www.paypal.com/paypalme/Chaschni?country.x=DE&locale.x=de_DE"
CONTACT_USERNAME = "Chaschni"
CUTLERY_PRICE = 0.30
MAX_DAILY = 15

TIMEZONE = ZoneInfo("Europe/Berlin")

ENABLE_TIME_LIMIT = True      # حالت واقعی
TEST_MODE = False            # حالت تست

WORK_DAYS = {0, 3}            # دوشنبه=0 ، پنجشنبه=3
START_HOUR = 12
END_HOUR = 17
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
PICKUP_ADDRESS_SHORT = "List 30163 (Hannover)"

# ---------- DB ----------
conn = sqlite3.connect("orders.db", check_same_thread=False)
cur = conn.cursor()
# --- ADD DELIVERY TIME COLUMNS (SAFE MIGRATION) ---
try:
    cur.execute("ALTER TABLE orders ADD COLUMN delivery_day TEXT")
except sqlite3.OperationalError:
    pass  # column already exists

try:
    cur.execute("ALTER TABLE orders ADD COLUMN delivery_slot TEXT")
except sqlite3.OperationalError:
    pass  # column already exists

cur.execute("""
CREATE TABLE IF NOT EXISTS discount_codes (
    code TEXT PRIMARY KEY,
    percent INTEGER,
    max_use INTEGER,
    used_count INTEGER DEFAULT 0
)
""")
conn.commit()

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

# ---------- USERS TABLE ----------
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY
)
""")

# ---------- LOGS TABLE ----------
cur.execute("""
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT,
    created_at TEXT
)
""")

conn.commit()

# ---------- DISCOUNT USAGE ----------
cur.execute("""
CREATE TABLE IF NOT EXISTS discount_usage (
    user_id INTEGER,
    code TEXT,
    PRIMARY KEY (user_id, code)
)
""")
conn.commit()

# ---------- UTILITY ----------
user_state = {}
orders_runtime = {}
def get_remaining_stock(food_key, delivery_day):
    cur.execute("""
        SELECT SUM(qty) FROM orders
        WHERE food_key = ?
        AND delivery_day = ?
        AND status IN ('pending','approved')
    """, (food_key, delivery_day))
    
    sold = cur.fetchone()[0] or 0
    remaining = MAX_DAILY - sold
    return max(remaining, 0)
    

def get_slot_count(delivery_day, slot):
    cur.execute("""
        SELECT COUNT(DISTINCT order_no) FROM orders
        WHERE delivery_day = ?
          AND delivery_slot = ?
          AND status IN ('pending','approved')
    """, (delivery_day, slot))
    return cur.fetchone()[0] or 0


def send_payment_message(context, uid, st):

    if st.get("discount", 0) > 0:
        discount_text = f"🎁 تخفیف: {st['discount']}٪ (-€{round(st.get('discount_amount', 0),2)})"
    else:
        discount_text = ""

    base_total = st["food_total"] + (sum(i.get("cutlery_qty", 0) for i in st["items"]) * CUTLERY_PRICE)

    text = f"💰 مبلغ اولیه: €{round(base_total, 2)}\n"

    if st.get("discount", 0) > 0:
        text += f"🎁 تخفیف ({st['discount']}٪): -€{round(st.get('discount_amount', 0),2)}\n"

    text += f"💳 مبلغ نهایی قابل پرداخت: €{st['total']} (این مبلغ را پرداخت کنید)\n\n"

    text += (
        "⏳ شما فقط ۵ دقیقه برای پرداخت زمان دارید.\n"
        "❗ بعد از آن سفارش شما لغو خواهد شد.\n\n"
        "💳 پرداخت فقط از طریق PayPal انجام می‌شود.\n"
        "🙏 پس از پرداخت روی «پرداخت انجام شد» بزنید."
    )

    context.bot.send_message(uid, text)

    context.bot.send_message(
        chat_id=uid,
        text="💳 برای پرداخت روی دکمه زیر بزنید:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 پرداخت با PayPal", url=f"{PAYPAL_BASE_LINK}/{round(st['total'],2)}")],
            [InlineKeyboardButton("✅ پرداخت انجام شد", callback_data="paid_paypal")]
        ])
    )
    
# ---------- ANTI-SPAM ----------
user_last_msgs = {}     # آخرین زمان پیام کاربر
user_msg_count = {}     # تعداد پیام‌های اخیر
user_discount_attempts = {}
SPAM_WINDOW = 4         # بازه زمانی (ثانیه)
SPAM_LIMIT = 5          # حداکثر پیام مجاز در این بازه

def reset_user(uid):
    user_state.pop(uid, None)

def normalize_digits(text):
    persian = "۰۱۲۳۴۵۶۷۸۹"
    english = "0123456789"
    for p, e in zip(persian, english):
        text = text.replace(p, e)
    return text.strip()

def is_working_time():
    if TEST_MODE:
        return True

    if not ENABLE_TIME_LIMIT:
        return True

    now = datetime.now(TIMEZONE)
    day = now.weekday()
    hour = now.hour

    # سه‌شنبه → همیشه باز
    if day == 1:
        return True

    # چهارشنبه → فقط تا 18
    if day == 2:
        if hour < 18:
            return True
        return False

    # جمعه و شنبه → باز
    if day in [4, 5]:
        return True

    # یکشنبه → فقط تا 18
    if day == 6:
        if hour < 18:
            return True
        return False

    return False
    
def get_target_delivery_day():
    if TEST_MODE:
        return "monday"

    day = datetime.now(TIMEZONE).weekday()

    # سه‌شنبه، چهارشنبه → پنج‌شنبه
    if day in [1, 2]:
        return "thursday"

    # جمعه، شنبه، یکشنبه → دوشنبه
    if day in [4, 5, 6]:
        return "monday"

    return None  # دوشنبه یا پنج‌شنبه (روز تحویل → سفارش بسته)
    
def is_user_member(bot, user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False
        
def create_order(user_id, food_key, food_name, qty, total, cutlery_qty, payment_method, delivery_day, delivery_slot, order_no=None):
    from random import randint

    if order_no is None:
        today = datetime.now(TIMEZONE).strftime("%Y%m%d")
        rand = randint(100, 999)
        order_no = f"CH-{today}-{uuid.uuid4().hex[:6]}"

    cur.execute("""
        INSERT INTO orders
        (order_no, user_id, food_key, food_name, qty, cutlery_qty, total, status, payment_method, created_at, delivery_day, delivery_slot)
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

def safe_create_order(user_id, items, delivery_day, delivery_slot, total, payment_method, discount_code=None):
    try:
        conn.execute("BEGIN IMMEDIATE")  # 🔒 قفل دیتابیس

        # 1. چک موجودی
        for item in items:
            if item["food_key"] == "gift_farani":
                continue
            cur.execute("""
                SELECT SUM(qty) FROM orders
                WHERE food_key = ?
                AND delivery_day = ?
                AND status IN ('pending','approved')
            """, (item["food_key"], delivery_day))

            sold = cur.fetchone()[0] or 0
            if sold + item["qty"] > MAX_DAILY:
                conn.rollback()
                return False, "❌ موجودی غذا کافی نیست"

        # 2. چک ظرفیت تایم
        cur.execute("""
            SELECT COUNT(DISTINCT order_no) FROM orders
            WHERE delivery_day = ?
            AND delivery_slot = ?
            AND status IN ('pending','approved')
        """, (delivery_day, delivery_slot))

        count = cur.fetchone()[0] or 0

        if count >= 3:
            conn.rollback()
            return False, "❌ این بازه زمانی پر شده"

        
        # 🔒 بررسی و قفل تخفیف
        if discount_code:
            cur.execute("""
                SELECT percent, max_use, used_count
                FROM discount_codes
                WHERE code = ?
            """, (discount_code,))
            row = cur.fetchone()

            if not row:
                conn.rollback()
                return False, "❌ کد تخفیف نامعتبر شد"

            percent, max_use, used = row

            if used >= max_use:
                conn.rollback()
                return False, "❌ ظرفیت کد تخفیف تمام شد"
        
        # 3. ثبت سفارش
        from random import randint
        today = datetime.now(TIMEZONE).strftime("%Y%m%d")
        rand = randint(100, 999)
        order_no = f"CH-{today}-{uuid.uuid4().hex[:6]}"

        for item in items:
            cur.execute("""
                INSERT INTO orders
                (order_no, user_id, food_key, food_name, qty, cutlery_qty, total, status, payment_method, created_at, delivery_day, delivery_slot)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """, (
                order_no,
                user_id,
                item["food_key"],
                item["food_name"],
                item["qty"],
                item.get("cutlery_qty", 0),
                total,
                payment_method,
                datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                delivery_day,
                delivery_slot
            ))


            cur.execute("""
                INSERT OR IGNORE INTO discount_usage (user_id, code)
                VALUES (?, ?)
            """, (user_id, discount_code))
        conn.commit()
        return True, order_no

    except Exception as e:
        conn.rollback()
        return False, str(e)

def expire_pending_orders():
    cur.execute("""
        UPDATE orders
        SET status = 'expired'
        WHERE status = 'pending'
        AND datetime(created_at) < datetime('now','-5 minutes','localtime')
    """)
    conn.commit()

# ---------- MENU BASED ON DAY ----------
def get_foods_for_target_day():
    target = get_target_delivery_day()

    if target == "monday":
        return {
            "farani": {"name": "🍮 فرنی", "price": 3.5},
            "salad": {"name": "🥗 پروتینو (سالاد ماکارونی) ", "price": 5},
            "ash": {"name": "🍛 قیمه با برنج", "price": 8.5},
            "ghorme": {"name": "🍛🌿 قرمه سبزی با برنج", "price": 8.5},
            "gheyme_to_go": {"name": "🥡 قیمه (To Go)\u200f", "price": 4},
            "ghorme_to_go": {"name": "🥡 قرمه (To Go)\u200f", "price": 4},
        }

    if target == "thursday":
        return {
            "farani": {"name": "🍮 فرنی", "price": 3.5},
            "salad": {"name": "🥗 پروتینو (سالاد ماکارونی)", "price": 5},
            "ash": {"name": "🍛 قیمه با برنج", "price": 8.5},
            "zereshk": {"name": "🍛🌿 قرمه سبزی با برنج", "price": 8.5},
            "gheyme_to_go": {"name": "🥡 قیمه (To Go)\u200f", "price": 4},
            "ghorme_to_go": {"name": "🥡 قرمه (To Go)\u200f", "price": 4},
        }

    return {}

# ---------- KEYBOARDS ----------
def join_channel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/Chaschnii")],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_join")]
    ])



def persistent_menu():
    return ReplyKeyboardMarkup(
        [["🍽 شروع سفارش"], ["❌ لغو سفارش", "📞 تماس با ما"]],
        resize_keyboard=True
    )

def food_keyboard():
    foods = get_foods_for_target_day()
    buttons = []

    # ✅ اینجا باشه (فقط یکبار)
    target = get_target_delivery_day()

    if target == "monday":
        day = "دوشنبه"
    elif target == "thursday":
        day = "پنج‌شنبه"
    else:
        day = None
    if not day:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ فعلاً سفارشی فعال نیست", callback_data="noop")]
        ])    

    # 👇 حالا loop
    for k, f in foods.items():

        remaining = get_remaining_stock(k, day)

        # اگر موجودی تموم شده → اصلاً نمایش نده
        if remaining <= 0:
            label = f"{f['name']} — ❌ ناموجود"
            buttons.append([
                InlineKeyboardButton(label, callback_data="noop")
            ])
            continue

        label = f"{f['name']} — {f['price']}€"

        # اگر کمتر از 5 تا مونده → هشدار بده
        if remaining <= 5:
            label += f" ⏳ {remaining} باقی"

        buttons.append([
            InlineKeyboardButton(label, callback_data=f"food_{k}")
        ])

    # اگر هیچ غذایی موجود نبود
    if not buttons:
        buttons.append([
            InlineKeyboardButton("❌ موجودی امروز تمام شد", callback_data="noop")
        ])

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
            InlineKeyboardButton("📍 تحویل حضوری", callback_data="pickup_yes"),
            InlineKeyboardButton("❌ لغو سفارش", callback_data="pickup_no")
        ]
    ])


def delivery_slot_keyboard(delivery_day):
    buttons = []

    hour = START_HOUR
    minute = 0

    while hour < END_HOUR:
        start = f"{hour:02d}:{minute:02d}"

        minute += 30
        if minute == 60:
            hour += 1
            minute = 0

        end = f"{hour:02d}:{minute:02d}"
        slot = f"{start} – {end}"

        # ⛔ محدودیت ظرفیت (۳ سفارش)
        if get_slot_count(delivery_day, slot) >= 3:
            continue

        buttons.append([
            InlineKeyboardButton(
                f"⏰ {slot}",
                callback_data=f"slot_{start}_{end}"
            )
        ])

    if not buttons:
        buttons.append([
            InlineKeyboardButton("⛔ همه بازه‌ها پر شده‌اند", callback_data="noop")
        ])

    return InlineKeyboardMarkup(buttons)
# ---------- COMMANDS ----------
def send_welcome(bot, chat_id, is_admin=False):
    bot.send_message(
        chat_id,
    "👋 خوش آمدید به ربات تهیه غذا در هانوفر!\n\n"

    "🍽 سیستم سفارش‌دهی ما به‌صورت *پیش‌سفارش* انجام می‌شود.\n\n"

    "🚚 تحویل غذا فقط در روزهای:\n"
    "• دوشنبه\n"
    "• پنج‌شنبه\n\n"

    "🗓 ثبت سفارش:\n"
    "• سه‌شنبه و چهارشنبه → برای تحویل پنج‌شنبه\n"
    "• جمعه، شنبه و یکشنبه → برای تحویل دوشنبه\n\n"

    "⏰ آخرین زمان ثبت سفارش:\n"
    "روز قبل از تحویل تا ساعت 18\n\n"

    "🚗 محدوده ارسال: 30163 + برخی خیابان‌های 30165\n"
    "🎒 اگر خارج از محدوده باشید، امکان تحویل حضوری نیز وجود دارد.\n\n"

    "🙏 لطفاً سفارش خود را از قبل ثبت فرمایید.\n"
    "برای شروع، از دکمه‌های زیر استفاده کنید:",
        reply_markup=persistent_menu()
    )

    if is_admin:
        status = "🔵 تست فعال است" if TEST_MODE else "⚪ حالت واقعی فعال است"
        bot.send_message(
            chat_id,
            f"⚙️ پنل مدیریت\n{status}",
            reply_markup=ReplyKeyboardMarkup(
                [
                     ["📊 ریپورت"],
                     ["📊 گزارش فردا"],
                     ["🎁 مدیریت تخفیف"],
                     ["❌ حذف کد تخفیف"],
                     ["📊 تحلیل"],
                     ["📊 تحلیل رفتار"],
                     ["📣 ارسال پیام"],
                     ["📣 ارسال یادآوری تحویل"],
                     ["⚠️ پیام اضطراری", "🟢 حذف پیام اضطراری"],
                     ["🔵 فعال‌کردن تست", "⚪ غیرفعال‌کردن تست"]
                ],
                resize_keyboard=True
            )
        )

def start(update: Update, context: CallbackContext):
    uid = update.effective_user.id

    # ذخیره کاربر
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    conn.commit()

    # لاگ ورود
    cur.execute(
        "INSERT INTO logs (user_id, action, created_at) VALUES (?, ?, ?)",
        (uid, "start", datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()

    if not is_user_member(context.bot, uid):
        update.message.reply_text(
            "📢 برای استفاده از ربات، ابتدا عضو کانال ما شوید 🌱\n\n"
            "👇 بعد از عضویت، روی «بررسی عضویت» بزنید",
            reply_markup=join_channel_keyboard()
        )
        return

    send_welcome(
        context.bot,
        uid,
        uid == ADMIN_CHAT_ID
    )
    
    # ⚙️ پنل ادمین
    if update.effective_user.id == ADMIN_CHAT_ID:
        status = "🔵 تست فعال است" if TEST_MODE else "⚪ حالت واقعی فعال است"

        update.message.reply_text(
            f"⚙️ پنل مدیریت\n{status}",
            reply_markup=ReplyKeyboardMarkup(
                [
                     ["📊 ریپورت"],
                     ["📊 گزارش فردا"],
                     ["🎁 مدیریت تخفیف"],
                     ["❌ حذف کد تخفیف"],
                     ["📊 تحلیل"],
                     ["📊 تحلیل رفتار"],
                     ["📣 ارسال پیام"],
                     ["📣 ارسال یادآوری تحویل"],
                     ["⚠️ پیام اضطراری", "🟢 حذف پیام اضطراری"],
                     ["🔵 فعال‌کردن تست", "⚪ غیرفعال‌کردن تست"]
                ],
                resize_keyboard=True
            )
        )


# ---------- CALLBACK HANDLER ----------
def callbacks(update: Update, context: CallbackContext):
    expire_pending_orders()
    q = update.callback_query
    uid = q.from_user.id
    q.answer()

    st = user_state.get(uid)

    # ---------------- FOOD SELECTION ----------------
    if q.data.startswith("food_"):
        key = q.data.replace("food_", "")
        foods = get_foods_for_target_day()   # ✅ این خط اصلاح شد

        cur.execute(
            "INSERT INTO logs (user_id, action, created_at) VALUES (?, ?, ?)",
            (uid, "select_food", datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        
        if key not in foods:
            q.answer("این غذا در منوی امروز نیست", show_alert=True)
            return

        f = foods[key]
        if not user_state.get(uid):
            user_state[uid] = {
                "step": "qty",
                "items": []
            }

        user_state[uid]["current_item"] = {
            "food_key": key,
            "food_name": f["name"],
            "price": f["price"]
        }

        user_state[uid]["step"] = "qty"

        q.edit_message_text(
            f"{f['name']} انتخاب شد.\n"
            "📦 لطفاً تعداد موردنظر را وارد کنید:"
        )
        return
   
    if q.data == "check_join":
        if is_user_member(context.bot, uid):
            q.edit_message_text(
                "✅ عضویت شما تأیید شد 🌱\n\n"
                "خوش آمدید 👇"
            )

        # ⬅️ این خط کلیدی است
            send_welcome(
                context.bot,
                uid,
                uid == ADMIN_CHAT_ID
            )

        else:
            q.answer(show_alert=True)
            context.bot.send_message(
                uid,
                "❌ هنوز عضو کانال نیستید.\n\n"
                "📢 لطفاً ابتدا عضو کانال شوید 👇",
                reply_markup=join_channel_keyboard()
            )
        return
    
    if q.data == "no_discount":
        st = user_state.get(uid)

        if not st:
            q.answer("خطا", show_alert=True)
            return

        st["discount"] = 0
        st["discount_code"] = None

        total_cutlery = sum(i.get("cutlery_qty", 0) for i in st["items"])
        total = st["food_total"] + (total_cutlery * CUTLERY_PRICE)

        st["discount_amount"] = 0
        st["total"] = round(total, 2)

        q.edit_message_text("❌ بدون کد تخفیف ادامه داده شد")

        send_payment_message(context, uid, st)
        return
    
    # ---------------- CUTLERY YES ----------------
    if q.data == "cutlery_yes":
        st["step"] = "cutlery_qty"
        q.edit_message_text(
            f"🥄 هر عدد: {CUTLERY_PRICE}€\n"
            "لطفاً تعداد موردنیاز را وارد کنید:"
        )
        return

    # ---------------- CUTLERY NO ----------------
    if q.data == "cutlery_no":
        st["items"][-1]["cutlery_qty"] = 0
        st["step"] = "ask_more"

        q.edit_message_text(
            "🛒 آیا سفارش دیگری دارید؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ سفارش دیگر", callback_data="more_order")],
                [InlineKeyboardButton("✅ ادامه خرید", callback_data="continue_order")]
            ])
        )
        return

    # ---------------- PICKUP YES ----------------
    if q.data == "pickup_yes":
        st["delivery_method"] = "pickup"
        st["step"] = "fullname"
        q.edit_message_text("👤 لطفاً نام کامل خود را وارد کنید:")
        return

    # ---------------- PICKUP NO ----------------
    if q.data == "pickup_no":
        reset_user(uid)
        q.edit_message_text("❌ سفارش لغو شد.")
        context.bot.send_message(uid, "منوی اصلی:", reply_markup=persistent_menu())

        return

    # ---------------- PAYMENT CONFIRM ----------------
    if q.data == "paid_paypal":
        st = user_state.get(uid)
        if st and st.get("paid"):
            q.answer("⚠️ این سفارش قبلاً ثبت شده", show_alert=True)
            return
        

        # اگر state وجود نداشت
        if not st:
            q.answer("خطا در سفارش", show_alert=True)
            return

        # اگر زمان ذخیره نشده بود
        created_str = st.get("created_at")

        if not created_str:
            reset_user(uid)
            context.bot.send_message(uid, "❌ خطا در سفارش. لطفاً دوباره تلاش کنید.")
            return

        created_at = datetime.strptime(created_str, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)

        # اگر بیشتر از ۵ دقیقه گذشته بود
        if datetime.now(TIMEZONE) - created_at > timedelta(minutes=5):
            q.answer("⏰ زمان پرداخت تمام شد", show_alert=True)

            context.bot.send_message(
                uid,
                "⏰ زمان پرداخت شما تمام شد.\n\n"
                "❗ اگر پرداخت انجام داده‌اید، مبلغ شما تا دقایقی دیگر بازگردانده می‌شود.\n"
                "📩 در صورت نیاز با پشتیبانی تماس بگیرید."
            )

            # 👇 این قسمت جدید (برای ادمین)
            # ساخت متن غذاها 👇
            foods_text = "\n".join(
                f"🍽 {i['food_name']} × {i['qty']} | 🥄 {i.get('cutlery_qty', 0)}"
                for i in st["items"]
            )

            # بعدش پیام ادمین 👇
            context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ پرداخت نامشخص\n\n"
                f"👤 کاربر: {uid}\n"
                f"💰 مبلغ: €{st.get('total')}\n"
                f"📅 روز: {st.get('delivery_day')}\n"
                f"⏰ بازه: {st.get('delivery_slot')}\n\n"
                f"🍽 آیتم‌ها:\n{foods_text}\n\n"
                "❗ کاربر بعد از ۵ دقیقه پرداخت را زده\n"
                "👉 احتمال دارد پرداخت انجام شده باشد"
            )

            reset_user(uid)
            return

        # جلوگیری از دابل کلیک
        if st.get("paid"):
            q.answer("⚠️ این سفارش قبلاً ثبت شده", show_alert=True)
            return

        st["payment_method"] = "PayPal"

        # بررسی اولین سفارش
        cur.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (uid,))
        order_count = cur.fetchone()[0]
        first_order = order_count == 0
    
        # هدیه اولین سفارش
        if first_order:
            st["items"].append({
                "food_key": "gift_farani",
                "food_name": "🍮 فرنی (هدیه اولین سفارش)",
                "qty": 1,
                "price": 0,
                "food_total": 0,
                "cutlery_qty": 0
            })

        # ثبت امن سفارش
        success, result = safe_create_order(
            uid,
            st["items"],
            st["delivery_day"],
            st["delivery_slot"],
            st["total"],
            "PayPal",
            st.get("discount_code")
        )
        
        
        if not success:
            context.bot.send_message(uid, result)
            reset_user(uid)
            return

            
        cur.execute(
            "INSERT INTO logs (user_id, action, created_at) VALUES (?, ?, ?)",
            (uid, "paid", datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()

        # ✅ فقط بعد از موفقیت
        st["paid"] = True

        order_no = result
        order_nos = [order_no]

        import copy
        orders_runtime[order_no] = copy.deepcopy(st)
        orders_runtime[order_no]["user_id"] = uid

        foods_text = "\n".join(
            f"🍽 {i['food_name']} × {i['qty']} | 🥄 {i.get('cutlery_qty', 0)}"
            for i in st["items"]
        )

        total_cutlery = sum(
            i.get("cutlery_qty", 0) for i in st["items"]
        )

        discount_text = ""
        if st.get("discount", 0) > 0:
            discount_text = f"\n🎁 تخفیف: {st['discount']}٪ (-€{st.get('discount_amount', 0)})"
        
        base_total = st["food_total"] + (total_cutlery * CUTLERY_PRICE)

        msg = (
            f"💳 پرداخت ثبت شد.\n"
            f"🧾 شماره سفارش: {order_no}\n\n"
            f"{foods_text}\n"
            f"🥄 مجموع قاشق/چنگال: {total_cutlery}\n"
            f"📅 روز تحویل: {st['delivery_day']}\n"
            f"⏰ بازه تحویل: {st['delivery_slot']}\n\n"
            f"💰 مبلغ اولیه: €{round(base_total,2)}\n"
        )

        if st.get("discount", 0) > 0:
            msg += f"🎁 تخفیف ({st['discount']}٪): -€{round(st.get('discount_amount',0),2)}\n"
            
        msg += f"💳 مبلغ نهایی پرداخت‌ شده: €{st['total']}\n\n"

        msg += (
            "⏳ سفارش شما ثبت شد و در انتظار تأیید است.\n\n"
            "🕒 سفارش‌ها معمولاً در مدت کوتاهی تأیید می‌شوند.\n"
            "⚠️ در صورت ثبت خارج از ساعات کاری، صبح روز بعد تأیید می‌شود 🙏"
        )

        context.bot.send_message(uid, msg)

        # پیام ادمین
        admin_foods_text = "\n".join(
            f"🍽 {i['food_name']} × {i['qty']} | 🥄 {i.get('cutlery_qty', 0)}"
            for i in st["items"]
        )

        admin_total_cutlery = sum(
            i.get("cutlery_qty", 0) for i in st["items"]
        )

        discount_text = ""
        if st.get("discount", 0) > 0:
            discount_text = f"\n🎁 تخفیف: {st.get('discount',0)}٪ (-€{st.get('discount_amount',0)})"

        base_total = st["food_total"] + (admin_total_cutlery * CUTLERY_PRICE)

        admin_msg = (
            f"🆕 سفارش جدید\n\n"
            f"🧾 شماره سفارش: {order_no}\n"
            f"👤 نام: {st['fullname']}\n"
            f"📞 تلفن: {st['phone']}\n"
            f"📍 آدرس: {st['address']}\n"
            f"📮 کد پستی: {st['postcode']}\n"
            f"📅 روز تحویل: {st['delivery_day']}\n"
            f"⏰ بازه تحویل: {st['delivery_slot']}\n\n"
            f"{admin_foods_text}\n"
            f"🥄 مجموع قاشق/چنگال: {admin_total_cutlery}\n\n"
            f"💰 مبلغ اولیه: €{round(base_total,2)}\n"
        )

        if st.get("discount", 0) > 0:
            admin_msg += f"🎁 تخفیف ({st.get('discount',0)}٪): -€{round(st.get('discount_amount',0),2)}\n"

        admin_msg += f"💳 مبلغ دریافتی: €{st['total']}"

        context.bot.send_message(
            ADMIN_CHAT_ID,
            admin_msg,
            reply_markup=admin_keyboard(order_no)
        )
        reset_user(uid)
        return

    # ---------------- DELIVERY SLOT ----------------
    if q.data.startswith("slot_"):
        _, start, end = q.data.split("_")
        slot = f"{start} – {end}"

        if get_slot_count(st["delivery_day"], slot) >= 3:
            q.answer("❌ این بازه زمانی پر شده", show_alert=True)
            return

        st["delivery_slot"] = slot

        cur.execute(
            "INSERT INTO logs (user_id, action, created_at) VALUES (?, ?, ?)",
            (uid, "go_to_payment", datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()

    # محاسبه مبلغ نهایی
        total_cutlery = sum(i.get("cutlery_qty", 0) for i in st["items"])

        base_total = st["food_total"] + (total_cutlery * CUTLERY_PRICE)

        discount = st.get("discount", 0)
        discount_amount = base_total * discount / 100

        total = base_total - discount_amount

        st["discount_amount"] = round(discount_amount, 2)
        st["total"] = round(total, 2)
        
        # بررسی وجود کد تخفیف
        cur.execute("""
        SELECT code FROM discount_codes
        WHERE used_count < max_use
        LIMIT 1
        """)

        has_discount = cur.fetchone()

        if has_discount:
            st["step"] = "discount_code"

            context.bot.send_message(
                uid,
                "🎁 اگر کد تخفیف دارید وارد کنید\nیا روی دکمه زیر بزنید",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ کد ندارم", callback_data="no_discount")]
                ])
            )
            return

        send_payment_message(context, uid, st)
        return

    # ---------------- ADD MORE OR CONTINUE ORDER ----------------
    if q.data == "more_order":
        st = user_state.get(uid)
        if not st:
            q.answer()
            return

        st["step"] = "qty"
        q.edit_message_text("🍽 لطفاً غذای بعدی را انتخاب کنید:")
        context.bot.send_message(
            uid,
            "منوی غذا:",
            reply_markup=food_keyboard()
        )
        return

    if q.data == "continue_order":
        st = user_state.get(uid)
        if not st:
            q.answer()
            return

        st["step"] = "postcode"
        q.edit_message_text("📮 لطفاً کد پستی را وارد کنید:")
        return
    
    # ---------------- ADMIN APPROVAL ----------------
    if q.data.startswith("admin_"):

        # فقط ادمین
        if uid != ADMIN_CHAT_ID:
            q.answer("⛔ دسترسی ندارید", show_alert=True)
            return
            
        _, action, order_no = q.data.split("_")
        order = orders_runtime.get(order_no)

        if not order:
            q.answer("❌ اطلاعات سفارش پیدا نشد", show_alert=True)
            return
        

        cur.execute("""
            SELECT user_id, delivery_day, delivery_slot
            FROM orders
            WHERE order_no = ?
        """, (order_no,))
        row = cur.fetchone()

        if not row:
            q.answer("❌ سفارش پیدا نشد", show_alert=True)
            return

        user_id = row[0]

        if action == "ok":
            cur.execute("""
                UPDATE orders
                SET status = 'approved',
                    payment_checked_at = ?
                WHERE order_no = ?
            """, (
                datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                order_no
            ))
            conn.commit()

            delivery_text = (
                "🚗 روش دریافت: ارسال"
                if order["delivery_method"] == "delivery"
                else f"🎒 روش دریافت: تحویل حضوری\n📍 آدرس: {PICKUP_ADDRESS_FULL}"
            )

            approved_total_cutlery = sum(
                i.get("cutlery_qty", 0) for i in order["items"]
            )

            foods_text = "\n".join(
                f"🍽 {i['food_name']} × {i['qty']}"
                for i in order["items"]
            )

            msg = (
                "✅ سفارش شما تأیید شد 🙏\n\n"
                "🧾 خلاصه سفارش:\n"
                f"{foods_text}\n"
                f"🥄 مجموع قاشق/چنگال: {approved_total_cutlery}\n"
                f"📅 روز تحویل: {order['delivery_day']}\n"
                f"⏰ بازه تحویل: {order['delivery_slot']}\n"
                f"{delivery_text}\n"
                f"🏠 آدرس: {order['address']}\n"
                f"📞 تماس: {order['phone']}\n\n"
                f"💶 مبلغ کل: €{order['total']}\n\n"
                "🙏 ممنون از اعتماد شما"
            )

            context.bot.send_message(user_id, msg)
            q.edit_message_text(q.message.text + "\n\n✔️ تایید شد")

            # ✅ فقط اینجا پاک کن
            orders_runtime.pop(order_no, None)
            q.answer("✅ انجام شد")

        else:
            user_state[uid] = {
                "step": "admin_cancel_reason",
                "order_no": order_no,
                "target_user": user_id
            }

            q.edit_message_text(q.message.text + "\n\n📝 لطفاً دلیل لغو را بنویسید:")

            context.bot.send_message(uid, "✍️ لطفاً دلیل را بنویسید:")

        return
    # ---------------- REMINDER ----------------
    if q.data.startswith("remind_"):
        _, target = q.data.split("_")

        cur.execute("""
            SELECT 
                user_id,
                food_name,
                qty,
                cutlery_qty,
                delivery_day,
                delivery_slot
            FROM orders
            WHERE delivery_day = ?
              AND status = 'approved'
            ORDER BY user_id
        """, (
            "دوشنبه" if target == "monday" else "پنج‌شنبه",
        ))
        rows = cur.fetchall()

        if not rows:
            q.edit_message_text("هیچ سفارش تأییدشده‌ای برای یادآوری وجود ندارد.")
            return

        orders_map = {}

        for user_id, food, qty, cutlery, day, slot in rows:
            key = (user_id, day, slot)

            if key not in orders_map:
                orders_map[key] = {
                    "items": [],
                    "day": day,
                    "slot": slot
                }

            orders_map[key]["items"].append({
                "food": food,
                "qty": qty,
                "cutlery": cutlery
            })

        sent = 0

        for (user_id, day, slot), data in orders_map.items():
            delivery_label = f"فردا ({day})"
            foods_text = "\n".join(
                f"🍽 {i['food']} × {i['qty']} | 🥄 {i['cutlery']}"
                for i in data["items"]
            )

            total_cutlery = sum(i["cutlery"] for i in data["items"])

            msg = (
                "⏰ یادآوری تحویل غذا\n\n"
                f"{foods_text}\n"
                f"🥄 مجموع قاشق/چنگال: {total_cutlery}\n"
                f"📅 تحویل: {delivery_label}\n"
                f"⏰ بازه تحویل: {slot}\n\n"
                "🙏 لطفاً در بازه انتخاب‌شده آماده باشید"
            )

            context.bot.send_message(user_id, msg)
            sent += 1

        q.edit_message_text(f"✅ یادآوری برای {sent} سفارش ارسال شد")
        return
    if q.data == "remind_cancel":
        q.edit_message_text("❌ ارسال یادآوری لغو شد")
        return

# ---------- TEXT HANDLER ----------
def handle_text(update: Update, context: CallbackContext):
    global EMERGENCY_MESSAGE
    global TEST_MODE
    expire_pending_orders()
 
    uid = update.effective_user.id
    text = update.message.text
    st = user_state.get(uid)
    if st and st.get("step") == "admin_cancel_reason":
        reason = text

        order_no = st["order_no"]
        target_user = st["target_user"]

        close_order(order_no, "canceled")

        context.bot.send_message(
            target_user,
            f"❌ سفارش شما لغو شد.\n\n"
            f"📌 دلیل: {reason}\n\n"
            "💰 در صورت پرداخت، مبلغ تا دقایقی دیگر بازگردانده می‌شود."
        )

        update.message.reply_text("✅ سفارش لغو شد و دلیل ارسال شد.")

        reset_user(uid)
        return
        
    if text == "❌ لغو سفارش":
        reset_user(uid)
        update.message.reply_text("سفارش لغو شد.", reply_markup=persistent_menu())
        return
    
    
    # --- ANALYTICS (ADMIN ONLY) ---
    if uid == ADMIN_CHAT_ID and text == "📊 تحلیل رفتار":

        cur.execute("""
            SELECT action, COUNT(*) FROM logs
            GROUP BY action
        """)
        rows = cur.fetchall()

        msg = "📊 تحلیل رفتار کاربران:\n\n"

        for action, count in rows:
            msg += f"{action} → {count}\n"

        update.message.reply_text(msg)
        return


    # --- BROADCAST (ADMIN ONLY) ---
    if uid == ADMIN_CHAT_ID and text == "📣 ارسال پیام":
        user_state[uid] = {"step": "broadcast"}
        update.message.reply_text("✍️ متن پیام رو بفرست:")
        return


    if st and st.get("step") == "broadcast":

        msg = text

        cur.execute("SELECT user_id FROM users")
        users = cur.fetchall()

        sent = 0

        for u in users:
            try:
                context.bot.send_message(u[0], msg)
                sent += 1
            except:
                pass

        update.message.reply_text(f"✅ پیام به {sent} نفر ارسال شد")

        reset_user(uid)
        return
    
    if uid == ADMIN_CHAT_ID and text == "🎁 مدیریت تخفیف":
        user_state[uid] = {"step": "discount_code_create"}
        update.message.reply_text("✍️ کد تخفیف را وارد کنید:")
        return

    # حذف کد تخفیف
    if uid == ADMIN_CHAT_ID and text == "❌ حذف کد تخفیف":
        user_state[uid] = {"step": "delete_discount"}
        update.message.reply_text("🗑 کد موردنظر را وارد کنید:")
        return

    if st and st.get("step") == "delete_discount":
        code = text.upper()

        cur.execute("SELECT 1 FROM discount_codes WHERE code = ?", (code,))
        exists = cur.fetchone()

        if not exists:
            update.message.reply_text("❌ چنین کدی وجود ندارد")
            return

        cur.execute("DELETE FROM discount_codes WHERE code = ?", (code,))
        conn.commit()

        update.message.reply_text("✅ کد حذف شد")
        reset_user(uid)
        return
    
    if st and st.get("step") == "discount_code_create":
        st["code"] = text.upper()
        st["step"] = "discount_percent"
        update.message.reply_text("📊 درصد تخفیف (مثلاً 15):")
        return
    
    if st and st.get("step") == "discount_percent":
        if not text.isdigit():
            update.message.reply_text("❗ فقط عدد")
            return

        st["percent"] = int(text)
        st["step"] = "discount_limit"
        update.message.reply_text("🔢 تعداد استفاده:")
        return
    

    if st and st.get("step") == "discount_limit":
        if not text.isdigit():
            update.message.reply_text("❗ فقط عدد")
            return

        cur.execute("""
            INSERT OR REPLACE INTO discount_codes (code, percent, max_use, used_count)
            VALUES (?, ?, ?, 0)
        """, (
            st["code"],
            st["percent"],
            int(text)
        ))
        conn.commit()

        update.message.reply_text("✅ کد تخفیف ساخته شد")

        reset_user(uid)
        return
    
    if st and st.get("step") == "discount_code":
        code = text.strip().upper()
        attempts = user_discount_attempts.get(uid, 0)

        if attempts >= 5:
            update.message.reply_text("⛔ تلاش بیش از حد. بعداً امتحان کنید")
            return

        # ❌ کاربر کد ندارد (این باید همیشه اول چک شود)

        if "ندارم" in code or "no" in code:
            st["discount"] = 0
            st["discount_code"] = None

            # محاسبه مبلغ
            total_cutlery = sum(i.get("cutlery_qty", 0) for i in st["items"])
            total = st["food_total"] + (total_cutlery * CUTLERY_PRICE)

            st["discount_amount"] = 0
            st["total"] = round(total, 2)

            # پاک کردن attempts
            user_discount_attempts.pop(uid, None)
            st["step"] = "payment"
            send_payment_message(context, uid, st)
            return

        # ✅ بقیه کدها
        cur.execute("""
            SELECT percent, max_use, used_count
            FROM discount_codes
            WHERE code=?
        """, (code,))
        row = cur.fetchone()

        if not row:
            user_discount_attempts[uid] = attempts + 1
            update.message.reply_text("❌ کد نامعتبر")
            return

        percent, max_use, used = row
        if used >= max_use:
            update.message.reply_text("⛔ کد غیرفعال")
            return

                    # ✅ مصرف فوری کد (حل مشکل)
        cur.execute("""
        UPDATE discount_codes
        SET used_count = used_count + 1
        WHERE code = ?
        """, (code,))
        conn.commit()

        # چک استفاده قبلی
        cur.execute("""
            SELECT 1 FROM discount_usage
            WHERE user_id = ? AND code = ?
        """, (uid, code))

        if cur.fetchone():
            st["step"] = "discount_code"

            update.message.reply_text(
                "⛔ شما قبلاً از این کد استفاده کرده‌اید\n\n"
                "👉 اگر کد دیگری دارید وارد کنید\n"
                "یا روی دکمه زیر بزنید 👇",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ کد ندارم", callback_data="no_discount")]
                ])
            )
            return

        if used >= max_use:
            update.message.reply_text("⛔ کد غیرفعال")
            return

        # اعمال تخفیف
        st["discount"] = percent
        st["discount_code"] = code

        # محاسبه مبلغ
        total_cutlery = sum(i.get("cutlery_qty", 0) for i in st["items"])
        total = st["food_total"] + (total_cutlery * CUTLERY_PRICE)

        discount_amount = total * percent / 100
        total = total - discount_amount

        st["discount_amount"] = round(discount_amount, 2)
        st["total"] = round(total, 2)

        update.message.reply_text(
            f"✅ {percent}% تخفیف اعمال شد\n"
            f"💰 مبلغ جدید: €{st['total']}"
        )

        send_payment_message(context, uid, st)
        return
                        
    
    # ---------- ANTI-SPAM CHECK ----------
    now = time.time()

    # اگر کاربر سابقه ندارد → مقدار اولیه بساز
    if uid not in user_last_msgs:
        user_last_msgs[uid] = now
        user_msg_count[uid] = 1
    else:
        # اگر پیام جدید در فاصله کوتاه ارسال شده
        if now - user_last_msgs[uid] <= SPAM_WINDOW:
            user_msg_count[uid] += 1
        else:
            # اگر فاصله زیاد بود → شمارنده ریست شود
            user_msg_count[uid] = 1

        # آخرین زمان پیام آپدیت شود
        user_last_msgs[uid] = now

    # اگر کاربر بیشتر از حد مجاز پیام بده
    if uid != ADMIN_CHAT_ID and user_msg_count[uid] > SPAM_LIMIT:
        update.message.reply_text("⚠️ لطفاً پیام‌ها را پشت‌سرهم ارسال نکنید 🙏")
        return
    

    # اگر پیام اضطراری فعال است، اجازه شروع سفارش نده
    if EMERGENCY_MESSAGE and text == "🍽 شروع سفارش":
        update.message.reply_text(EMERGENCY_MESSAGE)
        return

    # فعال کردن پیام اضطراری
    if uid == ADMIN_CHAT_ID and text == "⚠️ پیام اضطراری":
        update.message.reply_text("لطفاً متن پیام اضطراری را وارد کنید:")
        user_state[uid] = {"step": "set_emergency"}
        return

    # حذف پیام اضطراری
    if uid == ADMIN_CHAT_ID and text == "🟢 حذف پیام اضطراری":
        EMERGENCY_MESSAGE = None
        update.message.reply_text("🟢 پیام اضطراری حذف شد ، سفارش‌گیری فعال است")
        return

    # دریافت متن پیام اضطراری
    if st and st.get("step") == "set_emergency":
        EMERGENCY_MESSAGE = text
        reset_user(uid)
        update.message.reply_text("⚠️ پیام اضطراری ثبت شد")
        return

    
    # --- ADMIN: DISABLE TEST MODE ---
    if uid == ADMIN_CHAT_ID and "تست" in text and "غیر" in text:
        TEST_MODE = False
        update.message.reply_text("⚪ حالت تست غیرفعال شد")
        return

    # --- ADMIN: ENABLE TEST MODE ---
    if uid == ADMIN_CHAT_ID and "تست" in text and "فعال" in text:
        TEST_MODE = True
        update.message.reply_text("🔵 حالت تست فعال شد")
        return


    # --- REPORT (ADMIN ONLY) ---
    if uid == ADMIN_CHAT_ID and text.strip() in ["📊 ریپورت", "ریپورت", "report", "/report"]:
        cur.execute("SELECT * FROM orders ORDER BY id DESC")
        rows = cur.fetchall()

        if not rows:
            update.message.reply_text("هیچ سفارشی ثبت نشده است.")
            return

        report = "📊 گزارش فروش:\n\n"
        for r in rows:
            report += (
                f"📌 سفارش: {r[1]}\n"
                f"👤 کاربر: {r[2]}\n"
                f"🍽 غذا: {r[4]} × {r[5]}\n"
                f"🥄 قاشق/چنگال: {r[6]}\n"
                f"💳 پرداخت: {r[9]}\n"
                f"💶 مبلغ: €{r[7]}\n"
                f"📅 زمان: {r[10]}\n"
                f"📦 وضعیت: {r[8]}\n"
                "---------------------------\n"
            )

        update.message.reply_text(report)
        return

    # --- REPORT TOMORROW FOOD ---
    if uid == ADMIN_CHAT_ID and text == "📊 گزارش فردا":

        target = get_target_delivery_day()

        if target == "monday":
            day_fa = "دوشنبه"
        elif target == "thursday":
            day_fa = "پنج‌شنبه"
        else:
            update.message.reply_text("امروز گزارش فعالی وجود ندارد.")
            return

        cur.execute("""
            SELECT food_name, SUM(qty), SUM(cutlery_qty)
            FROM orders
            WHERE delivery_day = ?
            AND status != 'canceled'
            GROUP BY food_name
        """, (day_fa,))

        rows = cur.fetchall()

        if not rows:
            update.message.reply_text("هیچ سفارشی ثبت نشده است.")
            return

        foods_text = ""
        total_cutlery = 0
        total_orders = 0

        for food, qty, cutlery in rows:
            foods_text += f"{food}: {qty}\n"
            total_cutlery += cutlery or 0
            total_orders += qty

        msg = (
            f"📊 گزارش غذا برای تحویل {day_fa}\n\n"
            f"{foods_text}\n"
            f"🥄 مجموع قاشق/چنگال: {total_cutlery}\n"
            f"📦 مجموع غذاها: {total_orders}"
        )

        update.message.reply_text(msg)
        return
    
    # --- ANALYTICS (ADMIN ONLY) ---
    if uid == ADMIN_CHAT_ID and text == "📊 تحلیل":

        # 1. غذای پرفروش
        cur.execute("""
            SELECT food_name, SUM(qty)
            FROM orders
            WHERE status = 'approved'
            GROUP BY food_name
            ORDER BY SUM(qty) DESC
        """)
        foods = cur.fetchall()

        food_text = "\n".join(
            f"{name} → {qty}" for name, qty in foods
        ) or "ندارد"

        # 2. تایم محبوب
        cur.execute("""
            SELECT delivery_slot, COUNT(*)
            FROM orders
            WHERE status = 'approved'
            GROUP BY delivery_slot
            ORDER BY COUNT(*) DESC
        """)
        slots = cur.fetchall()

        slot_text = "\n".join(
            f"{slot} → {count}" for slot, count in slots
        ) or "ندارد"

        # 3. روز پرفروش
        cur.execute("""
            SELECT delivery_day, COUNT(*)
            FROM orders
            WHERE status = 'approved'
            GROUP BY delivery_day
        """)
        days = cur.fetchall()

        day_text = "\n".join(
            f"{day} → {count}" for day, count in days
        ) or "ندارد"

        # ارسال گزارش
        msg = (
            "📊 تحلیل فروش:\n\n"
            "🍽 غذای پرفروش:\n"
            f"{food_text}\n\n"
            "⏰ تایم‌های محبوب:\n"
            f"{slot_text}\n\n"
            "📅 روزها:\n"
            f"{day_text}"
        )

        update.message.reply_text(msg)
        return

    
    # --- ADMIN: SEND DELIVERY REMINDER ---
    if uid == ADMIN_CHAT_ID and text == "📣 ارسال یادآوری تحویل":
        target = get_target_delivery_day()

        if target == "monday":
            day_fa = "دوشنبه"
        elif target == "thursday":
            day_fa = "پنج‌شنبه"
        else:
            update.message.reply_text("امروز روز تحویل نیست.")
            return

        update.message.reply_text(
            f"📣 ارسال پیام یادآوری برای تحویل {day_fa}\n"
            "آیا مطمئن هستید؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، ارسال کن", callback_data=f"remind_{target}")],
                [InlineKeyboardButton("❌ لغو", callback_data="remind_cancel")]
            ])
        )
        return    

       
    # MENU
    if text == "🍽 شروع سفارش":
        cur.execute(
            "INSERT INTO logs (user_id, action, created_at) VALUES (?, ?, ?)",
            (uid, "start_order", datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
        
        if not is_user_member(context.bot, uid):
            update.message.reply_text(
                "📢 برای ثبت سفارش، ابتدا عضو کانال ما شوید 👇",
                reply_markup=join_channel_keyboard()
            )
            return
            
        if not is_working_time():
            update.message.reply_text(
            "📦 سفارش‌گیری بسته است.\n\n"
            "🗓 لطفاً در روز و ساعت مجاز پیش‌سفارش اقدام فرمایید."
            )
            return

        target = get_target_delivery_day()

        if target == "monday":
            delivery_day = "دوشنبه"
        elif target == "thursday":
            delivery_day = "پنج‌شنبه"
        else:
            delivery_day = None

        user_state[uid] = {
            "step": "qty",
            "items": [],
            "delivery_day": delivery_day,
            "created_at": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")
        }
        target = get_target_delivery_day()

        if target == "monday":
            day_name = "دوشنبه"
        elif target == "thursday":
            day_name = "پنج‌شنبه"
        else:
            update.message.reply_text("امکان ثبت سفارش در حال حاضر وجود ندارد.")
            return

        update.message.reply_text(
            "🎉 هدیه ویژه برای مشتریان جدید\n"
            "🍮 با اولین سفارش یک فرنی رایگان دریافت کنید\n\n"
            f"📋 منوی {day_name}\n"
            f"⏰ لطفاً سفارش خود را قبل از روز تحویل ثبت کنید:"
        )

        update.message.reply_text(
        "لطفاً انتخاب کنید:",
            reply_markup=food_keyboard()
        )
        return

    # CANCEL
    if text == "❌ لغو سفارش":
        reset_user(uid)
        update.message.reply_text("سفارش لغو شد.", reply_markup=persistent_menu())
        return

    # CONTACT
    if text == "📞 تماس با ما":
        update.message.reply_text(
            "ارتباط مستقیم:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 چت تلگرام", url=f"https://t.me/{CONTACT_USERNAME}")]
            ])
        )
        return

    # NO STATE
    if not st:
        update.message.reply_text("برای شروع از منوی پایین استفاده کنید.")
        return

    # QTY
    if st["step"] == "qty":
        text = normalize_digits(text)
        if not text.isdigit():
            update.message.reply_text("لطفاً فقط عدد وارد کنید.")
            return

        qty = int(text)
        item = st["current_item"]
        # تعداد این غذا داخل همین سفارش فعلی
        already_in_cart = sum(
            i["qty"] for i in st["items"]
            if i["food_key"] == item["food_key"]
        )

        remaining = get_remaining_stock(
            item["food_key"],
            st.get("delivery_day")
        )

        remaining -= already_in_cart
       
    # جلوگیری از فروش بیشتر از ظرفیت روزانه
        if qty > remaining:
            if remaining <= 0:
                update.message.reply_text(f"🚫 موجودی {item['food_name']} تمام شد!")
            else:
                update.message.reply_text(f"⚠️ فقط {remaining} عدد {item['food_name']} باقی مانده است.")
            return

        if qty <= 0 or qty > MAX_DAILY:
            update.message.reply_text(f"حداکثر سفارش: {MAX_DAILY}")
            return

        item = st["current_item"]
        item["qty"] = qty
        item["food_total"] = qty * item["price"]
        item["cutlery_qty"] = None

        st["items"].append(item)
        st.pop("current_item")

        st["food_total"] = sum(i["food_total"] for i in st["items"])
        st["step"] = "cutlery_choice"

        update.message.reply_text(
            f"🥄 نیاز به قاشق/چنگال دارید؟ (هر عدد: €{CUTLERY_PRICE})",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("بله", callback_data="cutlery_yes"),
                 InlineKeyboardButton("خیر", callback_data="cutlery_no")]
            ])
        )
        return

    # CUTLERY QTY
    if st["step"] == "cutlery_qty":
        text = normalize_digits(text)
        if not text.isdigit():
            update.message.reply_text("لطفاً فقط عدد وارد کنید.")
            return

        c = int(text)

    # محدودیت تعداد قاشق/چنگال
        current_qty = st["items"][-1]["qty"]

        if c < 0 or c > current_qty:
            update.message.reply_text("❗ تعداد قاشق/چنگال نمی‌تواند بیشتر از تعداد همین غذا باشد.")
            return

        st["items"][-1]["cutlery_qty"] = c
        st["step"] = "ask_more"

        update.message.reply_text(
            "🛒 آیا سفارش دیگری دارید؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ سفارش دیگر", callback_data="more_order")],
                [InlineKeyboardButton("✅ ادامه خرید", callback_data="continue_order")]
            ])
        )
        return

    # POSTCODE
    if st["step"] == "postcode":
        pc = normalize_digits(text)

        if not pc.isdigit() or len(pc) != 5:
            update.message.reply_text("📮 کد پستی باید دقیقاً ۵ رقم و فقط عدد باشد.")
            return

        st["postcode"] = pc

        if pc == "30163":
            st["delivery_method"] = "delivery"
            st["step"] = "fullname"
            update.message.reply_text("👤 لطفاً نام کامل وارد کنید:")
            return

        if pc == "30165":
            st["delivery_method"] = "check_street"
            st["step"] = "street"
            update.message.reply_text("📌 لطفاً نام خیابان را وارد کنید:")
            return

        st["delivery_method"] = "pickup"
        st["step"] = "pickup_confirm"
        update.message.reply_text(
            f"🚫 خارج از محدوده ارسال.\n"
            f"🎒 تحویل حضوری از: {PICKUP_ADDRESS_SHORT}\n"
            "می‌خواهید ادامه دهید؟",
            reply_markup=pickup_keyboard()
        )
        return

    # STREET CHECK
    if st["step"] == "street":
        street = text.lower().replace("ß", "ss").replace(" ", "")
        valid = False

        for s in LOCAL_STREETS_30165:
            if street == s.lower().replace(" ", ""):
                valid = True
                break

        if valid:
            st["delivery_method"] = "delivery"
            st["step"] = "fullname"
            update.message.reply_text("👤 لطفاً نام کامل وارد کنید:")
            return

        st["delivery_method"] = "pickup"
        st["step"] = "pickup_confirm"
        update.message.reply_text(
            "🚫 این خیابان در محدوده نیست.\n"
            f"🎒 تحویل حضوری از {PICKUP_ADDRESS_SHORT}",
            reply_markup=pickup_keyboard()
        )
        return

    # FULLNAME
    if st["step"] == "fullname":
        st["fullname"] = text
        st["step"] = "phone"
        update.message.reply_text("📞 لطفاً شماره تماس را وارد کنید:")
        return

    # PHONE
    if st["step"] == "phone":
        phone = normalize_digits(text)

        if not phone.isdigit() or len(phone) < 8 or len(phone) > 15:
            update.message.reply_text(
            "📞 لطفاً شماره تماس معتبر وارد کنید.\n"
            "✔️ فقط عدد\n"
            "✔️ حداقل ۸ رقم"
            )
            return

        st["phone"] = phone

        if st["delivery_method"] == "delivery":
            st["step"] = "address"
            update.message.reply_text("🏠 لطفاً آدرس کامل را وارد کنید:")
            return
        else:
            st["address"] = "تحویل حضوری"
            st["step"] = "delivery_slot"

            target = get_target_delivery_day()
            if target == "monday":
                st["delivery_day"] = "دوشنبه"
            elif target == "thursday":
                st["delivery_day"] = "پنج‌شنبه"

            update.message.reply_text(
                f"⏰ لطفاً بازه زمانی تحویل غذا برای {st['delivery_day']} را انتخاب کنید:",
                reply_markup=delivery_slot_keyboard(st["delivery_day"])
            )
            return
   # ADDRESS
    if st["step"] == "address":
        st["address"] = text
        st["step"] = "delivery_slot"

        target = get_target_delivery_day()
        if target == "monday":
            st["delivery_day"] = "دوشنبه"
        elif target == "thursday":
            st["delivery_day"] = "پنج‌شنبه"
        else:
            update.message.reply_text("امکان ثبت سفارش در حال حاضر وجود ندارد.")
            reset_user(uid)
            return

        update.message.reply_text(
            f"⏰ لطفاً بازه زمانی تحویل غذا برای {st['delivery_day']} را انتخاب کنید:",
            reply_markup=delivery_slot_keyboard(st["delivery_day"])
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

    from queue import Queue

    update_queue = Queue()
    dp = Dispatcher(bot, update_queue, workers=4)

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(callbacks))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    bot.delete_webhook()

    threading.Thread(target=expire_loop, daemon=True).start()
    
    dp.start_polling()
    dp.idle()
    

    def expire_loop():
        while True:
            try:
                expire_pending_orders()
            except:
                pass
            time.sleep(60)

    
    


if __name__ == "__main__":
    main()




