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
PICKUP_ADDRESS_SHORT = "Tannenbergallee (Hannover)"

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
def get_remaining_stock(food_key):
    cur.execute("""
        SELECT SUM(qty) FROM orders
        WHERE food_key = ?
          AND date(created_at) = date('now', 'localtime')
          AND status != 'canceled'
    """, (food_key,))
    sold = cur.fetchone()[0] or 0
    remaining = MAX_DAILY - sold
    return max(remaining, 0)

def get_slot_count(delivery_day, slot):
    cur.execute("""
        SELECT COUNT(*) FROM orders
        WHERE delivery_day = ?
          AND delivery_slot = ?
          AND status != 'canceled'
    """, (delivery_day, slot))
    return cur.fetchone()[0] or 0
    
# ---------- ANTI-SPAM ----------
user_last_msgs = {}     # آخرین زمان پیام کاربر
user_msg_count = {}     # تعداد پیام‌های اخیر
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
        
def create_order(user_id, food_key, food_name, qty, total, cutlery_qty, payment_method, delivery_day, delivery_slot):
    from random import randint

    today = datetime.now(TIMEZONE).strftime("%Y%m%d")
    rand = randint(100, 999)
    order_no = f"CH-{today}-{rand}"

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

# ---------- MENU BASED ON DAY ----------
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

    for k, f in foods.items():
        remaining = get_remaining_stock(k)

        # اگر موجودی تموم شده → اصلاً نمایش نده
        if remaining <= 0:
            continue

        label = f"{f['name']} — {f['price']}€"

        # اگر کمتر از 5 تا مونده → هشدار بده
        if remaining <= 5:
            label += f"\n⏳ فقط {remaining} عدد باقی مانده"

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
                     ["📣 ارسال یادآوری تحویل"],
                     ["⚠️ پیام اضطراری", "🟢 حذف پیام اضطراری"],
                     ["🔵 فعال‌کردن تست", "⚪ غیرفعال‌کردن تست"]
                ],
                resize_keyboard=True
            )
        )

def start(update: Update, context: CallbackContext):
    uid = update.effective_user.id

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
                     ["📣 ارسال یادآوری تحویل"],
                     ["⚠️ پیام اضطراری", "🟢 حذف پیام اضطراری"],
                     ["🔵 فعال‌کردن تست", "⚪ غیرفعال‌کردن تست"]
                ],
                resize_keyboard=True
            )
        )


# ---------- CALLBACK HANDLER ----------
def callbacks(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    q.answer()

    st = user_state.get(uid)

    # ---------------- FOOD SELECTION ----------------
    if q.data.startswith("food_"):
        key = q.data.replace("food_", "")
        foods = get_foods_for_target_day()   # ✅ این خط اصلاح شد

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

        if q.data == "paid_paypal":
            st["payment_method"] = "PayPal"

        order_nos = []

        for item in st["items"]:
            order_no = create_order(
                uid,
                item["food_key"],
                item["food_name"],
                item["qty"],
                st["total"],
                item.get("cutlery_qty", 0),
                st["payment_method"],
                st["delivery_day"],
                st["delivery_slot"]
            )
            order_nos.append(order_no)

        import copy

        for order_no in order_nos:
            orders_runtime[order_no] = copy.deepcopy(st)
            orders_runtime[order_no]["user_id"] = uid
        foods_text = "\n".join(
            f"🍽 {i['food_name']} × {i['qty']} | 🥄 {i.get('cutlery_qty', 0)}"
            for i in st["items"]
        )

        total_cutlery = sum(
            i.get("cutlery_qty", 0) for i in st["items"]
        )

        context.bot.send_message(
            uid,
            f"💳 پرداخت ثبت شد.\n"
            f"🧾 شماره سفارش: {', '.join(order_nos)}\n\n"
            f"{foods_text}\n"
            f"🥄 مجموع قاشق/چنگال: {total_cutlery}\n"
            f"📅 روز تحویل: {st['delivery_day']}\n"
            f"⏰ بازه تحویل: {st['delivery_slot']}\n"
            f"💶 مبلغ کل: €{st['total']}\n\n"
            "⏳ سفارش شما در انتظار تأیید ادمین است."
        )
        foods_text = "\n".join(
            f"🍽 {i['food_name']} × {i['qty']}"
            for i in st["items"]
        )

        admin_foods_text = "\n".join(
            f"🍽 {i['food_name']} × {i['qty']} | 🥄 {i.get('cutlery_qty', 0)}"
            for i in st["items"]
        )

        admin_total_cutlery = sum(
            i.get("cutlery_qty", 0) for i in st["items"]
        )
        context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 سفارش جدید\n\n"
            f"🧾 شماره سفارش: {order_no}\n"
            f"👤 نام: {st['fullname']}\n"
            f"📞 تلفن: {st['phone']}\n"
            f"📍 آدرس: {st['address']}\n"
            f"📮 کد پستی: {st['postcode']}\n"
            f"📅 روز تحویل: {st['delivery_day']}\n"
            f"⏰ بازه تحویل: {st['delivery_slot']}\n\n"
            f"{admin_foods_text}\n"
            f"🥄 مجموع قاشق/چنگال: {admin_total_cutlery}\n"
            f"💶 مبلغ کل: €{st['total']}",
            reply_markup=admin_keyboard(order_no)
        )
        return

    # ---------------- DELIVERY SLOT ----------------
    if q.data.startswith("slot_"):
        _, start, end = q.data.split("_")
        st["delivery_slot"] = f"{start} – {end}"

    # محاسبه مبلغ نهایی
        total_cutlery = sum(
            i.get("cutlery_qty", 0) for i in st["items"]
        )

        total = st["food_total"] + (total_cutlery * CUTLERY_PRICE)
        st["total"] = total
        st["step"] = "pay"

        q.edit_message_text(
            f"✅ بازه تحویل انتخاب شد:\n"
            f"⏰ {start} – {end}\n\n"
            f"💰 مبلغ نهایی: €{total}\n\n"
            "💳 پرداخت فقط از طریق PayPal انجام می‌شود.\n"
            "🙏 پس از پرداخت روی «پرداخت انجام شد» بزنید."
        )

        context.bot.send_message(
            chat_id=uid,
            text="لینک پرداخت:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 پرداخت با PayPal", url=f"{PAYPAL_BASE_LINK}/{total}")],
                [InlineKeyboardButton("✅ پرداخت انجام شد", callback_data="paid_paypal")]
            ])
        )
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
        _, action, order_no = q.data.split("_")
        order = orders_runtime.get(order_no)
        user_id = order["user_id"]

        if action == "ok":
            cur.execute("""
                UPDATE orders
                SET status = 'approved',
                    payment_checked_at = ?
                WHERE user_id = ?
                  AND delivery_day = ?
                  AND delivery_slot = ?
                  AND status = 'pending'
            """, (
                datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                order["user_id"],
                order["delivery_day"],
                order["delivery_slot"]
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

        else:
            close_order(order_no, "canceled")
            context.bot.send_message(user_id, "❌ سفارش شما لغو شد.")
            q.edit_message_text(q.message.text + "\n\n❌ لغو شد")

        reset_user(user_id)
        orders_runtime.pop(order_no, None)
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
 
    uid = update.effective_user.id
    text = update.message.text
    st = user_state.get(uid)
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
        
        if not is_user_member(context.bot, uid):
            update.message.reply_text(
                "📢 برای ثبت سفارش، ابتدا عضو کانال ما شوید 👇",
                reply_markup=join_channel_keyboard()
            )
            return
            
        if not is_working_time():
            update.message.reply_text(
            "📦 سفارش‌گیری امروز بسته است.\n\n"
            "🚚 امروز فقط تحویل سفارش‌های ثبت‌شده انجام می‌شود.\n\n"
            "🗓 لطفاً در روزهای مجاز پیش‌سفارش اقدام فرمایید."
            )
            return

        target = get_target_delivery_day()

        if target == "monday":
            day_name = "دوشنبه"
        elif target == "thursday":
            day_name = "پنج‌شنبه"
        else:
            update.message.reply_text("امکان ثبت سفارش در حال حاضر وجود ندارد.")
            return

        update.message.reply_text(
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
        # چک ظرفیت روزانه غذا
        cur.execute("""
            SELECT SUM(qty) FROM orders
            WHERE food_key = ? AND date(created_at) = date('now', 'localtime')
        """, (item["food_key"],))
        sold_today = cur.fetchone()[0] or 0

        remaining = MAX_DAILY - sold_today
# جلوگیری از فروش بیشتر از ظرفیت روزانه
        if qty > remaining:
            if remaining <= 0:
                update.message.reply_text(f"🚫 موجودی امروز {st['food_name']} تمام شد!")
            else:
                update.message.reply_text(f"⚠️ فقط {remaining} عدد {st['food_name']} باقی مانده است.")
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
        total_qty = sum(i["qty"] for i in st["items"])
        if c < 0 or c > total_qty:
            update.message.reply_text("❗ تعداد قاشق/چنگال نمی‌تواند بیشتر از تعداد غذا باشد.")
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

    dp = Dispatcher(bot, None, workers=0)

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(callbacks))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

    WEBHOOK_URL = f"https://chaschni-bot.onrender.com/{BOT_TOKEN}"
    bot.set_webhook(WEBHOOK_URL)

    port = int(os.environ.get("PORT", 8443))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()




