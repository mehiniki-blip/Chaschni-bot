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

ENABLE_TIME_LIMIT = True      # حالت واقعی
TEST_MODE = False            # حالت تست

WORK_DAYS = {0, 3}            # دوشنبه=0 ، پنجشنبه=3
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
    return now.weekday() in WORK_DAYS and START_HOUR <= now.hour < END_HOUR

def create_order(user_id, food_key, food_name, qty, total, cutlery_qty, payment_method):
    from random import randint

    today = datetime.now(TIMEZONE).strftime("%Y%m%d")
    rand = randint(100, 999)
    order_no = f"CH-{today}-{rand}"

    cur.execute("""
        INSERT INTO orders
        (order_no, user_id, food_key, food_name, qty, cutlery_qty, total, status, payment_method, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """, (
        order_no,
        user_id,
        food_key,
        food_name,
        qty,
        cutlery_qty,
        total,
        payment_method,
        datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")
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
        "🚗 تحویل فقط در 30163 + برخی خیابان‌های 30165\n"
        "📍 بزودی سراسر هانوفر\n\n"
        "برای شروع لطفاً از دکمه‌های زیر استفاده کنید:",
        reply_markup=persistent_menu()
    )
# دکمه‌های مخصوص ادمین
    if update.effective_user.id == ADMIN_CHAT_ID:
        update.message.reply_text(
        "⚙️ پنل مدیریت:",
            reply_markup=ReplyKeyboardMarkup(
            [
                ["📊 ریپورت"],
                ["⚠️ پیام اضطراری", "🟢 حذف پیام اضطراری"]
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
        foods = get_today_foods()

        f = foods[key]
        user_state[uid] = {
            "step": "qty",
            "food_key": key,
            "food_name": f["name"],
            "price": f["price"]
        }

        q.edit_message_text(
            f"{f['name']} انتخاب شد.\n"
            "📦 لطفاً تعداد موردنظر را وارد کنید:"
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
        st["cutlery_qty"] = 0
        st["step"] = "postcode"
        q.edit_message_text("📮 لطفاً کد پستی را وارد کنید:")
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
    if q.data in ["paid_paypal", "paid_cash"]:
        st = user_state.get(uid)

        if q.data == "paid_paypal":
            st["payment_method"] = "PayPal"
        else:
            st["payment_method"] = "Cash"

        order_no = create_order(
            uid,
            st["food_key"],
            st["food_name"],
            st["qty"],
            st["total"],
            st.get("cutlery_qty", 0),
            st["payment_method"]
        )

        orders_runtime[order_no] = st
        orders_runtime[order_no]["user_id"] = uid

        context.bot.send_message(
            uid,
            f"💳 پرداخت ثبت شد.\n"
            f"🧾 شماره سفارش: {order_no}\n\n"
            f"🍽 {st['food_name']} × {st['qty']}\n"
            f"🥄 قاشق/چنگال: {st.get('cutlery_qty',0)}\n"
            f"💶 مبلغ کل: €{st['total']}\n\n"
    "⏳ سفارش شما در انتظار تأیید ادمین است."
        )

        # ADMIN MESSAGE
        context.bot.send_message(
            ADMIN_CHAT_ID,
            f"⚠️ سفارش جدید برای بررسی\n\n"
            f"شماره سفارش: {order_no}\n"
            f"👤 نام: {st['fullname']}\n"
            f"📞 تلفن: {st['phone']}\n"
            f"📍 آدرس: {st['address']}\n"
            f"📮 کد پستی: {st['postcode']}\n"
            f"💳 پرداخت: {st['payment_method']}\n\n"
            f"🍽 غذا: {st['food_name']} × {st['qty']}\n"
            f"🥄 قاشق/چنگال: {st.get('cutlery_qty',0)}\n"
            f"💶 مبلغ نهایی: €{st['total']}",
            reply_markup=admin_keyboard(order_no)
        )

        reset_user(uid)
        return

    # ---------------- ADMIN APPROVAL ----------------
    if q.data.startswith("admin_"):
        _, action, order_no = q.data.split("_")
        order = orders_runtime.get(order_no)
        user_id = order["user_id"]

        if action == "ok":
            close_order(order_no, "approved")

            # unified message
            msg = (
                "🍽 سفارش شما تأیید شد!\n"
                "⏳ زمان آماده‌سازی حدود ۲۰–۲۵ دقیقه\n\n"
                "🚗 سفارش شما ارسال می‌شود." if order["delivery_method"] == "delivery"
                else
                "🍽 سفارش شما تأیید شد!\n"
                "⏳ زمان آماده‌سازی حدود ۲۰–۲۵ دقیقه\n\n"
                f"📍 لطفاً برای تحویل حضوری به این آدرس مراجعه کنید:\n{PICKUP_ADDRESS_FULL}"
            )

            context.bot.send_message(user_id, msg)
            q.edit_message_text(q.message.text + "\n\n✔️ تایید شد")

        else:
            close_order(order_no, "canceled")
            context.bot.send_message(user_id, "❌ سفارش شما لغو شد.")
            q.edit_message_text(q.message.text + "\n\n❌ لغو شد")

        orders_runtime.pop(order_no, None)
        return

# ---------- TEXT HANDLER ----------
def handle_text(update: Update, context: CallbackContext):
    global EMERGENCY_MESSAGE   # ← باید اینجا باشد
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
    if user_msg_count[uid] > SPAM_LIMIT:
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

    # REPORT (ADMIN ONLY)
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
       
    # MENU
    if text == "🍽 شروع سفارش":
        if not is_working_time():
            update.message.reply_text(
            "🔥 امروز منویی موجود نیست!\n"
            "📅 سرویس فقط دوشنبه و پنج‌شنبه\n"
            "⏰ ساعت 12:00 تا 18:00"
            )
            return

        update.message.reply_text("📋 منوی امروز:")
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
        # چک ظرفیت روزانه غذا
        cur.execute("""
            SELECT SUM(qty) FROM orders
            WHERE food_key = ? AND date(created_at) = date('now', 'localtime')
        """, (st["food_key"],))
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

        st["qty"] = qty
        st["food_total"] = qty * st["price"]
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
        if c < 0 or c > st["qty"]:
            update.message.reply_text("❗ تعداد قاشق/چنگال نمی‌تواند بیشتر از تعداد غذا باشد.")
            return

        st["cutlery_qty"] = c
        st["step"] = "postcode"
        update.message.reply_text("📮 لطفاً کد پستی را وارد کنید:")
        return

    # POSTCODE
    if st["step"] == "postcode":
        pc = normalize_digits(text)
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
        st["phone"] = text

        if st["delivery_method"] == "delivery":
            st["step"] = "address"
            update.message.reply_text("🏠 لطفاً آدرس کامل را وارد کنید:")
        else:
            st["address"] = "تحویل حضوری"
            st["step"] = "pay"

            total = st["food_total"] + (st["cutlery_qty"] * CUTLERY_PRICE)
            st["total"] = total

            update.message.reply_text(
                f"💶 مبلغ نهایی: €{total}\n"
                "💳 لطفاً روش پرداخت را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 پرداخت PayPal", url=f"{PAYPAL_BASE_LINK}/{total}")],
                    [InlineKeyboardButton("✔️ پرداخت انجام شد", callback_data="paid_paypal")]
                ])
            )
        return

    # ADDRESS
    if st["step"] == "address":
        st["address"] = text
        st["step"] = "pay"

        total = st["food_total"] + (st["cutlery_qty"] * CUTLERY_PRICE)
        st["total"] = total

        update.message.reply_text(
            f"💶 مبلغ نهایی: €{total}\n"
            "💳 لطفاً روش پرداخت را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 پرداخت PayPal", url=f"{PAYPAL_BASE_LINK}/{total}")],
                [InlineKeyboardButton("💵 پرداخت نقدی در محل", callback_data="paid_cash")],
                [InlineKeyboardButton("✔️ پرداخت انجام شد", callback_data="paid_paypal")]
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

    WEBHOOK_URL = f"https://chaschni-bot.onrender.com/{BOT_TOKEN}"
    bot.set_webhook(WEBHOOK_URL)

    port = int(os.environ.get("PORT", 8443))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()




