# -*- coding: utf-8 -*-
"""
ربات مدیریت اشتراک برای پیام‌رسان روبیکا (Rubika)
ساخته‌شده با کتابخانه rubka  →  pip install rubka requests

نکات مهم قبل از اجرا:
1) توکن ربات را در متغیر محیطی RUBIKA_BOT_TOKEN قرار دهید (یا در فایل .env / تنظیمات Railway).
2) اولین کسی که دستور /start را بزند، به‌طور خودکار «مالک ربات» ثبت می‌شود و پنل مدیریت برایش باز می‌شود.
3) توکن گیت‌هاب، نام‌کاربری، ریپازیتوری و لینک Raw همگی از داخل «پنل مدیریت ⚙️» با یک دکمه قابل تنظیم/تغییر هستند
   و در دیتابیس ذخیره می‌شوند (نیازی به دستکاری کد نیست). مقادیر زیر فقط به‌عنوان مقدار پیش‌فرض اولیه استفاده می‌شوند.
"""

import os
import json
import base64
import sqlite3
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

from rubka import Robot
try:
    from rubka import Message  # اکثر نسخه‌ها
except ImportError:
    from rubka.context import Message  # برخی نسخه‌های قدیمی‌تر

from rubka.keypad import ChatKeypadBuilder
from rubka.button import InlineBuilder

# ==================== تنظیمات اولیه ====================
# طبق درخواست: تنها چیزی که داخل کد/متغیر محیطی قرار دارد، توکن خود ربات است.
# هر چیز دیگری (توکن گیت‌هاب، لینک Raw، شماره کارت و ...) فقط از داخل خودِ ربات
# (پنل مدیریت) وارد و در دیتابیس ذخیره می‌شود.
BOT_TOKEN = os.environ.get("RUBIKA_BOT_TOKEN", "PUT_YOUR_RUBIKA_BOT_TOKEN_HERE")

DB_PATH = "bot_database.db"

_db_lock = threading.Lock()

# ==================== دیتابیس SQLite ====================
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, days INTEGER, price TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, username TEXT, device_code TEXT,
            password TEXT, plan_title TEXT, plan_days INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bot_users (
            user_id TEXT PRIMARY KEY, username TEXT,
            first_name TEXT, joined_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS blocked_users (
            user_id TEXT PRIMARY KEY, blocked_at TEXT)""")
        conn.commit()
        conn.close()


def get_setting(key, default=None):
    with _db_lock:
        conn = get_conn()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    with _db_lock:
        conn = get_conn()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()


def get_owner_id():
    return get_setting("owner_id")


def is_owner(user_id):
    owner = get_owner_id()
    return owner is not None and str(owner) == str(user_id)


def register_bot_user(user_id, username, first_name):
    with _db_lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO bot_users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
            (str(user_id), username or "", first_name or "", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()


def is_blocked(user_id):
    with _db_lock:
        conn = get_conn()
        row = conn.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (str(user_id),)).fetchone()
        conn.close()
    return row is not None


def block_user(user_id):
    with _db_lock:
        conn = get_conn()
        conn.execute("INSERT OR REPLACE INTO blocked_users (user_id, blocked_at) VALUES (?, ?)",
                     (str(user_id), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()


def unblock_user(user_id):
    with _db_lock:
        conn = get_conn()
        conn.execute("DELETE FROM blocked_users WHERE user_id=?", (str(user_id),))
        conn.commit()
        conn.close()


# ==================== تنظیمات گیت‌هاب (فقط از داخل پنل ربات وارد می‌شود) ====================
def parse_raw_link(url):
    """
    از روی یک لینک Raw گیت‌هاب مثل:
    https://raw.githubusercontent.com/USERNAME/REPO/BRANCH/path/to/User.json
    به‌صورت خودکار username / repo / branch / path را استخراج می‌کند.
    """
    try:
        u = urlparse(url.strip())
        if "raw.githubusercontent.com" not in u.netloc:
            return None
        parts = [p for p in u.path.split("/") if p]
        if len(parts) < 4:
            return None
        owner, repo, branch = parts[0], parts[1], parts[2]
        path = "/".join(parts[3:])
        if not path:
            return None
        return {"owner": owner, "repo": repo, "branch": branch, "path": path}
    except Exception:
        return None


def gh_conf():
    raw_link = get_setting("raw_link", "") or ""
    parsed = parse_raw_link(raw_link) if raw_link else None
    return {
        "token": get_setting("github_token", "") or "",
        "username": parsed["owner"] if parsed else "",
        "repo": parsed["repo"] if parsed else "",
        "path": parsed["path"] if parsed else "",
        "branch": parsed["branch"] if parsed else "main",
        "raw_link": raw_link,
    }


def fetch_github_json():
    cfg = gh_conf()
    if not cfg["token"] or not cfg["username"] or not cfg["repo"]:
        return None, None
    url = f"https://api.github.com/repos/{cfg['username']}/{cfg['repo']}/contents/{cfg['path']}"
    headers = {"Authorization": f"token {cfg['token']}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
    except Exception:
        return None, None
    if res.status_code != 200:
        return None, None
    file_info = res.json()
    sha = file_info.get("sha")
    try:
        content = base64.b64decode(file_info["content"]).decode("utf-8")
        data = json.loads(content)
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []
    return data, sha


def save_github_json(data, sha, commit_msg="Update users"):
    cfg = gh_conf()
    if not cfg["token"] or not cfg["username"] or not cfg["repo"]:
        return False
    url = f"https://api.github.com/repos/{cfg['username']}/{cfg['repo']}/contents/{cfg['path']}"
    headers = {"Authorization": f"token {cfg['token']}", "Accept": "application/vnd.github.v3+json"}
    updated_content = json.dumps(data, indent=4, ensure_ascii=False)
    encoded_content = base64.b64encode(updated_content.encode("utf-8")).decode("utf-8")
    payload = {"message": commit_msg, "content": encoded_content, "sha": sha, "branch": cfg["branch"]}
    try:
        res = requests.put(url, headers=headers, json=payload, timeout=15)
        return res.status_code in (200, 201)
    except Exception:
        return False


def calculate_remaining_days(expiry_str):
    try:
        exp_date = datetime.strptime(expiry_str, "%d-%m-%Y")
        delta = exp_date - datetime.now()
        return max(0, delta.days + 1)
    except Exception:
        return 0


# ==================== وضعیت مکالمه (State Machine ساده) ====================
# user_state[user_id] = {"step": "...", **data}
user_state = {}


def clear_state(user_id):
    user_state.pop(str(user_id), None)


def set_state(user_id, step, **data):
    user_state[str(user_id)] = {"step": step, **data}


def get_state(user_id):
    return user_state.get(str(user_id))


# ==================== کیبوردها ====================
def main_keypad(user_id):
    b = ChatKeypadBuilder()
    rows = [
        b.row(b.button(id="menu_buy", text="🚀 خرید اشتراک")),
        b.row(
            b.button(id="menu_download", text="📥 دانلود برنامه"),
            b.button(id="menu_support", text="💬 پشتیبانی سریع"),
        ),
    ]
    if is_owner(user_id):
        b.row(b.button(id="menu_admin", text="⚙️ پنل مدیریت"))
    return b.build(resize_keyboard=True)


def admin_panel_keypad():
    ib = InlineBuilder()
    ib.row(ib.button_simple("admin_add_plan", "➕ افزودن پلن"))
    ib.row(ib.button_simple("admin_add_apk", "📱 تنظیم فایل دانلود"))
    ib.row(ib.button_simple("admin_list_users", "📋 اشتراک‌های فعال"))
    ib.row(ib.button_simple("admin_list_bot_users", "👥 لیست کاربران ربات"))
    ib.row(ib.button_simple("admin_list_blocked", "🚫 کاربران مسدود"))
    ib.row(ib.button_simple("admin_broadcast", "📢 پیام همگانی"))
    ib.row(ib.button_simple("admin_github_settings", "🐙 تنظیمات گیت‌هاب / لینک"))
    return ib.build()


def github_settings_keypad():
    ib = InlineBuilder()
    ib.row(ib.button_simple("gh_set_token", "🔑 تغییر توکن گیت‌هاب"))
    ib.row(ib.button_simple("gh_set_link", "🔗 تغییر لینک Raw"))
    ib.row(ib.button_simple("gh_set_card", "💳 تغییر شماره کارت"))
    ib.row(ib.button_simple("gh_show", "👁 نمایش تنظیمات فعلی"))
    return ib.build()


# ==================== ربات ====================
bot = Robot(token=BOT_TOKEN)


def safe_reply(message, text, **kwargs):
    try:
        message.reply(text, **kwargs)
    except Exception as e:
        print(f"[safe_reply error] {e}")


def safe_send(chat_id, text, **kwargs):
    try:
        bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        print(f"[safe_send error] {e}")


# -------------------- /start --------------------
@bot.on_message(commands=["start"])
def start(bot: Robot, message: Message):
    user_id = str(message.sender_id)
    try:
        name = bot.get_name(user_id) or "کاربر"
    except Exception:
        name = "کاربر"
    try:
        username = bot.get_username(user_id)
    except Exception:
        username = None

    first_time_owner = False
    if get_owner_id() is None:
        set_setting("owner_id", user_id)
        first_time_owner = True

    register_bot_user(user_id, username, name)

    if is_blocked(user_id) and not is_owner(user_id):
        safe_reply(message, "🚫 دسترسی شما به این ربات مسدود شده است.")
        return

    clear_state(user_id)

    welcome = f"سلام {name} عزیز! 👋\n\n✨ به ربات رسمی فروش اشتراک خوش آمدید."
    if first_time_owner:
        welcome += "\n\n👑 شما به‌عنوان مالک ربات ثبت شدید و پنل مدیریت برایتان فعال شد."

    safe_reply(message, welcome, chat_keypad=main_keypad(user_id), chat_keypad_type="New")


# -------------------- دانلود برنامه --------------------
def send_apk(message):
    file_id = get_setting("apk_file_id")
    caption = get_setting("apk_caption", "📦 آخرین نسخه برنامه")
    if file_id:
        try:
            message.reply_document(file_id=file_id, text=f"📱 {caption}")
        except Exception as e:
            safe_reply(message, f"❌ خطا در ارسال فایل: {e}")
    else:
        safe_reply(message, "❌ فایلی جهت دانلود قرار داده نشده است.")


# -------------------- پشتیبانی --------------------
def support_start(message):
    set_state(message.sender_id, "support_msg")
    safe_reply(message, "🎧 بخش پشتیبانی\n\nلطفاً پیام، سوال یا مشکل خود را ارسال کنید:")


def support_receive_msg(message):
    owner = get_owner_id()
    if not owner:
        safe_reply(message, "❌ در حال حاضر پشتیبانی در دسترس نیست.")
        clear_state(message.sender_id)
        return
    try:
        username = bot.get_username(message.sender_id)
    except Exception:
        username = None
    try:
        name = bot.get_name(message.sender_id)
    except Exception:
        name = "کاربر"

    ib = InlineBuilder()
    ib.row(ib.button_simple(f"reply_sup_{message.sender_id}", "💬 پاسخ به کاربر"))
    text = (
        f"📩 پیام پشتیبانی جدید\n"
        f"👤 کاربر: {name}\n"
        f"🆔 آیدی: {message.sender_id}\n"
        f"🏷 یوزرنیم: @{username if username else 'ندارد'}\n\n"
        f"💬 متن پیام:\n{message.text}"
    )
    safe_send(owner, text, inline_keypad=ib.build())
    safe_reply(message, "✅ پیام شما با موفقیت ارسال شد. به‌زودی پاسخ داده می‌شود.")
    clear_state(message.sender_id)


def support_send_reply(message, target_id):
    safe_send(target_id, f"💎 پاسخ پشتیبانی:\n\n{message.text}")
    safe_reply(message, "✨ پاسخ با موفقیت به کاربر ارسال شد.")
    clear_state(message.sender_id)


# -------------------- خرید اشتراک --------------------
def buy_start(message):
    with _db_lock:
        conn = get_conn()
        plans = conn.execute("SELECT * FROM plans").fetchall()
        conn.close()
    if not plans:
        safe_reply(message, "❌ در حال حاضر پلنی برای فروش تعریف نشده است.")
        return
    ib = InlineBuilder()
    for p in plans:
        ib.row(ib.button_simple(f"buy_plan_{p['id']}", f"{p['title']} | {p['days']} روز | {p['price']} تومان"))
    safe_reply(message, "🚀 لطفاً یکی از پلن‌های زیر را انتخاب کنید:", inline_keypad=ib.build())


def buy_choose_plan(message, plan_id):
    with _db_lock:
        conn = get_conn()
        plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        conn.close()
    if not plan:
        safe_reply(message, "❌ این پلن دیگر موجود نیست.")
        return
    set_state(message.sender_id, "buy_device_code", plan_title=plan["title"], plan_days=plan["days"])
    safe_reply(message, "📲 لطفاً کد دستگاه (Device Code) خود را ارسال کنید:")


def buy_get_device_code(message, st):
    st["device_code"] = message.text.strip()
    set_state(message.sender_id, "buy_password", **st)
    safe_reply(message, "🔑 لطفاً یک رمز عبور برای اشتراک خود انتخاب و ارسال کنید:")


def buy_get_password(message, st):
    st["password"] = message.text.strip()
    set_state(message.sender_id, "buy_receipt", **st)
    text = "🧾 لطفاً تصویر رسید پرداخت را ارسال کنید."
    card_number = get_setting("card_number", "")
    if card_number:
        text += f"\n\n💳 شماره کارت: {card_number}"
    safe_reply(message, text)


def buy_get_receipt(message, st):
    user_id = str(message.sender_id)
    try:
        username = bot.get_username(user_id)
    except Exception:
        username = None

    with _db_lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO pending_orders (user_id, username, device_code, password, plan_title, plan_days) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username or "", st["device_code"], st["password"], st["plan_title"], st["plan_days"]),
        )
        order_id = cur.lastrowid
        conn.commit()
        conn.close()

    owner = get_owner_id()
    if owner:
        try:
            bot.forward_message(from_chat_id=message.chat_id, message_id=message.message_id, to_chat_id=owner)
        except Exception as e:
            print(f"[forward receipt error] {e}")
        ib = InlineBuilder()
        ib.row(
            ib.button_simple(f"confirm_order_{order_id}", "✅ تایید"),
            ib.button_simple(f"reject_order_{order_id}", "❌ رد"),
        )
        info = (
            f"🧾 سفارش جدید #{order_id}\n"
            f"👤 کاربر: {user_id} (@{username if username else '---'})\n"
            f"📦 پلن: {st['plan_title']} ({st['plan_days']} روز)\n"
            f"📲 کد دستگاه: {st['device_code']}\n"
            f"🔑 رمز عبور: {st['password']}"
        )
        safe_send(owner, info, inline_keypad=ib.build())

    safe_reply(message, "✅ سفارش شما ثبت شد و پس از تایید ادمین، اشتراک برایتان فعال می‌شود.")
    clear_state(user_id)


def handle_order_decision(message, order_id, approve):
    with _db_lock:
        conn = get_conn()
        order = conn.execute("SELECT * FROM pending_orders WHERE id=?", (order_id,)).fetchone()
        conn.close()
    if not order:
        safe_reply(message, "❌ این سفارش دیگر موجود نیست.")
        return

    if approve:
        expiry = (datetime.now() + timedelta(days=order["plan_days"])).strftime("%d-%m-%Y")
        data, sha = fetch_github_json()
        if data is None:
            safe_reply(message, "❌ خطا در اتصال به گیت‌هاب. تنظیمات گیت‌هاب را بررسی کنید.")
            return
        entry = {
            "device_id": order["device_code"],
            "key": order["password"],
            "expirydate": expiry,
            "telegram_id": order["user_id"],
            "telegram_username": order["username"],
        }
        data = [u for u in data if u.get("device_id") != order["device_code"]]
        data.append(entry)
        if save_github_json(data, sha, f"Add {order['device_code']}"):
            safe_reply(message, f"✅ سفارش #{order_id} تایید و اشتراک فعال شد.")
            safe_send(order["user_id"],
                      f"🎉 اشتراک شما فعال شد!\n📅 تاریخ انقضا: {expiry}\n🔑 رمز عبور: {order['password']}")
        else:
            safe_reply(message, "❌ خطا در ذخیره‌سازی روی گیت‌هاب.")
            return
    else:
        safe_reply(message, f"❌ سفارش #{order_id} رد شد.")
        safe_send(order["user_id"], "❌ متاسفانه سفارش شما رد شد. برای اطلاعات بیشتر با پشتیبانی در تماس باشید.")

    with _db_lock:
        conn = get_conn()
        conn.execute("DELETE FROM pending_orders WHERE id=?", (order_id,))
        conn.commit()
        conn.close()


# -------------------- پنل مدیریت: پلن‌ها --------------------
def admin_add_plan_start(message):
    set_state(message.sender_id, "add_plan_title")
    safe_reply(message, "📝 عنوان پلن جدید را وارد کنید:")


def admin_add_plan_title(message, st):
    st["title"] = message.text.strip()
    set_state(message.sender_id, "add_plan_days", **st)
    safe_reply(message, "⏳ تعداد روزهای اشتراک را وارد کنید (فقط عدد):")


def admin_add_plan_days(message, st):
    if not message.text.strip().isdigit():
        safe_reply(message, "⚠️ لطفاً فقط عدد وارد کنید.")
        return
    st["days"] = int(message.text.strip())
    set_state(message.sender_id, "add_plan_price", **st)
    safe_reply(message, "💰 قیمت پلن را وارد کنید (تومان):")


def admin_add_plan_price(message, st):
    price = message.text.strip()
    with _db_lock:
        conn = get_conn()
        conn.execute("INSERT INTO plans (title, days, price) VALUES (?, ?, ?)", (st["title"], st["days"], price))
        conn.commit()
        conn.close()
    safe_reply(message, f"✅ پلن ذخیره شد:\n📌 {st['title']} | ⏳ {st['days']} روز | 💰 {price} تومان")
    clear_state(message.sender_id)


# -------------------- پنل مدیریت: فایل APK --------------------
def admin_add_apk_start(message):
    set_state(message.sender_id, "apk_file")
    safe_reply(message, "📤 لطفاً فایل جدید برنامه (APK) را ارسال کنید:")


def admin_add_apk_file(message):
    file_id = None
    try:
        if getattr(message, "file", None):
            file_id = getattr(message.file, "file_id", None) or message.file.get("file_id")
    except Exception:
        file_id = None
    if not file_id:
        safe_reply(message, "⚠️ فایل معتبر شناسایی نشد. لطفاً دوباره ارسال کنید.")
        return
    set_state(message.sender_id, "apk_caption", apk_file_id=file_id)
    safe_reply(message, "📝 کپشن/توضیحات فایل را وارد کنید:")


def admin_add_apk_caption(message, st):
    set_setting("apk_file_id", st["apk_file_id"])
    set_setting("apk_caption", message.text.strip())
    safe_reply(message, "✅ فایل و کپشن ذخیره شدند.")
    clear_state(message.sender_id)


# -------------------- پنل مدیریت: لیست اشتراک‌ها --------------------
def admin_list_users(message):
    data, _ = fetch_github_json()
    if data is None:
        safe_reply(message, "❌ خطا در دریافت اطلاعات از گیت‌هاب (تنظیمات را بررسی کنید).")
        return
    if not data:
        safe_reply(message, "🌐 هیچ اشتراک فعالی یافت نشد.")
        return

    safe_reply(message, f"📊 تعداد کل کاربران فعال: {len(data)} نفر")
    for u in data:
        if not isinstance(u, dict):
            continue
        dev_id = u.get("device_id", "نامشخص")
        key = u.get("key", "نامشخص")
        expiry = u.get("expirydate", "نامشخص")
        tg_id = u.get("telegram_id", "ثبت‌نشده")
        tg_user = u.get("telegram_username", "ندارد")
        rem_days = calculate_remaining_days(expiry)

        text = (
            f"📱 کد دستگاه: {dev_id}\n"
            f"🔑 رمز عبور: {key}\n"
            f"📅 انقضا: {expiry} ({rem_days} روز باقی‌مانده)\n"
            f"🆔 آیدی: {tg_id}\n"
            f"🏷 یوزرنیم: @{tg_user}"
        )
        ib = InlineBuilder()
        blocked = tg_id not in (None, "ثبت‌نشده") and is_blocked(tg_id)
        block_btn = ib.button_simple(f"block_toggle_{tg_id}", "✅ رفع مسدود" if blocked else "🚫 مسدود کردن")
        ib.row(
            ib.button_simple(f"edit_usr_{dev_id}", "✏️ ویرایش رمز"),
            ib.button_simple(f"del_usr_{dev_id}", "🗑 حذف"),
        )
        ib.row(block_btn)
        safe_reply(message, text, inline_keypad=ib.build())


def delete_user_callback(message, dev_id):
    data, sha = fetch_github_json()
    if not data:
        safe_reply(message, "❌ خطا در اجرای عملیات.")
        return
    new_data = [u for u in data if u.get("device_id") != dev_id]
    if save_github_json(new_data, sha, f"Delete {dev_id}"):
        safe_reply(message, f"🗑 کاربر با کد دستگاه {dev_id} حذف شد.")
    else:
        safe_reply(message, "❌ خطا در بروزرسانی گیت‌هاب.")


def edit_user_start(message, dev_id):
    set_state(message.sender_id, "edit_user_key", dev_id=dev_id)
    safe_reply(message, f"🔑 رمز عبور جدید را برای دستگاه {dev_id} وارد کنید:")


def edit_user_save_key(message, st):
    dev_id = st["dev_id"]
    new_key = message.text.strip()
    data, sha = fetch_github_json()
    if not data:
        safe_reply(message, "❌ خطا در بروزرسانی گیت‌هاب.")
        clear_state(message.sender_id)
        return
    updated = False
    for u in data:
        if u.get("device_id") == dev_id:
            u["key"] = new_key
            updated = True
            break
    if updated and save_github_json(data, sha, f"Edit key {dev_id}"):
        safe_reply(message, f"✅ رمز عبور جدید ({new_key}) برای دستگاه {dev_id} ذخیره شد.")
    else:
        safe_reply(message, "❌ خطا در ثبت اطلاعات جدید.")
    clear_state(message.sender_id)


# -------------------- پنل مدیریت: کاربران ربات / بلاک --------------------
def admin_list_bot_users(message):
    with _db_lock:
        conn = get_conn()
        users = conn.execute("SELECT * FROM bot_users ORDER BY joined_at DESC").fetchall()
        conn.close()
    if not users:
        safe_reply(message, "👥 هیچ کاربری ثبت نشده است.")
        return
    safe_reply(message, f"👥 تعداد کل کاربران ربات: {len(users)} نفر")
    for u in users:
        blocked = is_blocked(u["user_id"])
        text = f"🆔 {u['user_id']}\n🏷 @{u['username'] or 'ندارد'}\n👤 {u['first_name']}\n📅 عضویت: {u['joined_at']}"
        ib = InlineBuilder()
        ib.row(ib.button_simple(f"block_toggle_{u['user_id']}", "✅ رفع مسدود" if blocked else "🚫 مسدود کردن"))
        safe_reply(message, text, inline_keypad=ib.build())


def admin_list_blocked(message):
    with _db_lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM blocked_users").fetchall()
        conn.close()
    if not rows:
        safe_reply(message, "🚫 هیچ کاربر مسدودی وجود ندارد.")
        return
    for r in rows:
        ib = InlineBuilder()
        ib.row(ib.button_simple(f"block_toggle_{r['user_id']}", "✅ رفع مسدود"))
        safe_reply(message, f"🆔 {r['user_id']}\n⏱ تاریخ مسدودیت: {r['blocked_at']}", inline_keypad=ib.build())


def toggle_block(message, target_id):
    if is_blocked(target_id):
        unblock_user(target_id)
        safe_reply(message, f"✅ کاربر {target_id} رفع مسدود شد.")
    else:
        block_user(target_id)
        safe_reply(message, f"🚫 کاربر {target_id} مسدود شد.")


# -------------------- پنل مدیریت: پیام همگانی --------------------
def admin_broadcast_start(message):
    set_state(message.sender_id, "broadcast_msg")
    safe_reply(message, "📢 متن پیام همگانی را ارسال کنید:")


def admin_broadcast_send(message):
    with _db_lock:
        conn = get_conn()
        users = conn.execute("SELECT user_id FROM bot_users").fetchall()
        conn.close()
    text = message.text
    sent, failed = 0, 0
    for u in users:
        if is_blocked(u["user_id"]):
            continue
        try:
            bot.send_message(u["user_id"], f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1
    safe_reply(message, f"✅ پیام همگانی ارسال شد.\n📨 موفق: {sent} | ❌ ناموفق: {failed}")
    clear_state(message.sender_id)


# -------------------- پنل مدیریت: تنظیمات گیت‌هاب --------------------
def gh_show(message):
    cfg = gh_conf()
    card_number = get_setting("card_number", "") or "تنظیم نشده"
    text = (
        "🐙 تنظیمات فعلی:\n\n"
        f"🔗 لینک Raw: {cfg['raw_link'] or 'تنظیم نشده'}\n"
        f"   ↳ Username: {cfg['username'] or '—'} | Repo: {cfg['repo'] or '—'} | "
        f"برنچ: {cfg['branch']} | فایل: {cfg['path'] or '—'}\n"
        f"🔑 توکن گیت‌هاب: {'ثبت شده ✅' if cfg['token'] else 'ثبت نشده ❌'}\n"
        f"💳 شماره کارت: {card_number}"
    )
    safe_reply(message, text)


def gh_set_token_start(message):
    set_state(message.sender_id, "gh_token")
    safe_reply(message, "🔑 توکن جدید گیت‌هاب را ارسال کنید:")


def gh_set_link_start(message):
    set_state(message.sender_id, "gh_link")
    safe_reply(
        message,
        "🔗 لینک Raw فایل JSON را ارسال کنید.\n"
        "مثال:\nhttps://raw.githubusercontent.com/USERNAME/REPO/main/User.json",
    )


def gh_set_card_start(message):
    set_state(message.sender_id, "card_number")
    safe_reply(message, "💳 شماره کارت جدید را ارسال کنید:")


# ==================== هندلر اصلی پیام‌های متنی ====================
@bot.on_message()
def on_any_message(bot: Robot, message: Message):
    user_id = str(message.sender_id)

    # اجازه بده /start همیشه جدا مدیریت بشه (هندلر بالا)
    if message.text and message.text.strip() == "/start":
        return

    if is_blocked(user_id) and not is_owner(user_id):
        safe_reply(message, "🚫 دسترسی شما به این ربات مسدود شده است.")
        return

    st = get_state(user_id)

    # -------- مراحل مکالمه‌ای (state machine) --------
    if st:
        step = st["step"]
        try:
            if step == "support_msg":
                support_receive_msg(message)
            elif step == "support_reply":
                support_send_reply(message, st["target_id"])
            elif step == "buy_device_code":
                buy_get_device_code(message, st)
            elif step == "buy_password":
                buy_get_password(message, st)
            elif step == "buy_receipt":
                buy_get_receipt(message, st)
            elif step == "add_plan_title" and is_owner(user_id):
                admin_add_plan_title(message, st)
            elif step == "add_plan_days" and is_owner(user_id):
                admin_add_plan_days(message, st)
            elif step == "add_plan_price" and is_owner(user_id):
                admin_add_plan_price(message, st)
            elif step == "apk_file" and is_owner(user_id):
                admin_add_apk_file(message)
            elif step == "apk_caption" and is_owner(user_id):
                admin_add_apk_caption(message, st)
            elif step == "edit_user_key" and is_owner(user_id):
                edit_user_save_key(message, st)
            elif step == "broadcast_msg" and is_owner(user_id):
                admin_broadcast_send(message)
            elif step == "gh_token" and is_owner(user_id):
                set_setting("github_token", message.text.strip())
                safe_reply(message, "✅ توکن گیت‌هاب ذخیره شد.")
                clear_state(user_id)
            elif step == "gh_link" and is_owner(user_id):
                link = message.text.strip()
                if parse_raw_link(link):
                    set_setting("raw_link", link)
                    safe_reply(message, "✅ لینک Raw ذخیره شد.")
                    clear_state(user_id)
                else:
                    safe_reply(
                        message,
                        "⚠️ لینک معتبر نیست. باید دقیقاً شبیه این باشد:\n"
                        "https://raw.githubusercontent.com/USERNAME/REPO/main/User.json\n"
                        "دوباره ارسال کنید:",
                    )
            elif step == "card_number" and is_owner(user_id):
                set_setting("card_number", message.text.strip())
                safe_reply(message, "✅ شماره کارت ذخیره شد.")
                clear_state(user_id)
            else:
                clear_state(user_id)
        except Exception as e:
            print(f"[state handler error] {e}")
            safe_reply(message, "❌ خطایی رخ داد. عملیات لغو شد.")
            clear_state(user_id)
        return

    # -------- منوهای اصلی (متن ثابت، برای سازگاری بیشتر) --------
    text = (message.text or "").strip()
    if text == "🚀 خرید اشتراک":
        buy_start(message)
    elif text == "📥 دانلود برنامه":
        send_apk(message)
    elif text == "💬 پشتیبانی سریع":
        support_start(message)
    elif text == "⚙️ پنل مدیریت" and is_owner(user_id):
        safe_reply(message, "⚙️ پنل مدیریت ربات:", inline_keypad=admin_panel_keypad())
    elif text == "/cancel":
        clear_state(user_id)
        safe_reply(message, "عملیات لغو شد.", chat_keypad=main_keypad(user_id))


# ==================== هندلر دکمه‌ها (چه کیبورد چت، چه اینلاین) ====================
@bot.on_callback()
def on_any_callback(bot: Robot, message: Message):
    try:
        btn_id = message.aux_data.button_id
    except Exception:
        try:
            btn_id = message.aux_data.get("button_id")
        except Exception:
            return

    user_id = str(message.sender_id)

    if is_blocked(user_id) and not is_owner(user_id) and not btn_id.startswith("block_toggle_"):
        safe_reply(message, "🚫 دسترسی شما به این ربات مسدود شده است.")
        return

    try:
        if btn_id == "menu_buy":
            buy_start(message)
        elif btn_id == "menu_download":
            send_apk(message)
        elif btn_id == "menu_support":
            support_start(message)
        elif btn_id == "menu_admin" and is_owner(user_id):
            safe_reply(message, "⚙️ پنل مدیریت ربات:", inline_keypad=admin_panel_keypad())

        elif btn_id.startswith("buy_plan_"):
            buy_choose_plan(message, int(btn_id.replace("buy_plan_", "")))

        elif btn_id.startswith("reply_sup_") and is_owner(user_id):
            target_id = btn_id.replace("reply_sup_", "")
            set_state(user_id, "support_reply", target_id=target_id)
            safe_reply(message, f"🖊 پاسخ خود را برای کاربر ({target_id}) بنویسید:")

        elif btn_id.startswith("confirm_order_") and is_owner(user_id):
            handle_order_decision(message, int(btn_id.replace("confirm_order_", "")), True)
        elif btn_id.startswith("reject_order_") and is_owner(user_id):
            handle_order_decision(message, int(btn_id.replace("reject_order_", "")), False)

        elif btn_id == "admin_add_plan" and is_owner(user_id):
            admin_add_plan_start(message)
        elif btn_id == "admin_add_apk" and is_owner(user_id):
            admin_add_apk_start(message)
        elif btn_id == "admin_list_users" and is_owner(user_id):
            admin_list_users(message)
        elif btn_id == "admin_list_bot_users" and is_owner(user_id):
            admin_list_bot_users(message)
        elif btn_id == "admin_list_blocked" and is_owner(user_id):
            admin_list_blocked(message)
        elif btn_id == "admin_broadcast" and is_owner(user_id):
            admin_broadcast_start(message)
        elif btn_id == "admin_github_settings" and is_owner(user_id):
            safe_reply(message, "🐙 تنظیمات گیت‌هاب و لینک:", inline_keypad=github_settings_keypad())

        elif btn_id == "gh_set_token" and is_owner(user_id):
            gh_set_token_start(message)
        elif btn_id == "gh_set_link" and is_owner(user_id):
            gh_set_link_start(message)
        elif btn_id == "gh_set_card" and is_owner(user_id):
            gh_set_card_start(message)
        elif btn_id == "gh_show" and is_owner(user_id):
            gh_show(message)

        elif btn_id.startswith("edit_usr_") and is_owner(user_id):
            edit_user_start(message, btn_id.replace("edit_usr_", ""))
        elif btn_id.startswith("del_usr_") and is_owner(user_id):
            delete_user_callback(message, btn_id.replace("del_usr_", ""))
        elif btn_id.startswith("block_toggle_") and is_owner(user_id):
            toggle_block(message, btn_id.replace("block_toggle_", ""))
    except Exception as e:
        print(f"[callback handler error] {e}")
        safe_reply(message, "❌ خطایی رخ داد.")


# ==================== MAIN ====================
def main():
    init_db()
    print("Rubika bot is starting (polling)...")
    bot.run()


if __name__ == "__main__":
    main()
