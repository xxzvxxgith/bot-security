# -*- coding: utf-8 -*-
"""
Bot Security
بوت حماية وترفيه متكامل للمجموعات - نسخة أولية قابلة للتطوير

المتطلبات:
    pip install python-telegram-bot==21.10 pillow
اختياري لفحص الصور/الملصقات/GIF:
    pip install nudenet
    (يتطلب تنزيل نموذج NudeNet عند أول استخدام بحسب بيئة التشغيل)

ضع توكن البوت في TOKEN و ID المالك الأساسي في OWNER_ID.
مهم: البوت يحتاج أن يكون مشرفاً في المجموعة مع صلاحيات حذف الرسائل،
حظر/تقييد الأعضاء، وتثبيت الرسائل حسب الميزات التي تريد استخدامها.
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# إعدادات البوت
# ============================================================

TOKEN = "8906278700:AAGf5AZs6tKkPtROSxYjJPjLS4cuHX8tW5E"          # ضع توكن البوت هنا
OWNER_ID = 1537665613        # ضع ID المالك الأساسي هنا

BOT_NAME = "Bot Security"
DB_FILE = "bot_security.db"

# حد السرعة لمكافحة السبام
FLOOD_LIMIT = 8
FLOOD_WINDOW = 6

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(BOT_NAME)

# ============================================================
# قاعدة البيانات
# ============================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
cur = db.cursor()

cur.executescript("""
CREATE TABLE IF NOT EXISTS groups (
    chat_id INTEGER PRIMARY KEY,
    delete_message TEXT DEFAULT 'ممنوع إرسال الردود',
    replies_enabled INTEGER DEFAULT 1,
    links_enabled INTEGER DEFAULT 0,
    flood_enabled INTEGER DEFAULT 1,
    badwords_enabled INTEGER DEFAULT 0,
    media_filter_enabled INTEGER DEFAULT 0,
    gif_filter_enabled INTEGER DEFAULT 0,
    sticker_filter_enabled INTEGER DEFAULT 0,
    welcome_enabled INTEGER DEFAULT 0,
    welcome_text TEXT DEFAULT 'أهلاً بك {name} في المجموعة 🌷',
    subscription_enabled INTEGER DEFAULT 0,
    subscription_channel TEXT DEFAULT '',
    subscription_link TEXT DEFAULT '',
    warn_limit INTEGER DEFAULT 3
);

CREATE TABLE IF NOT EXISTS roles (
    chat_id INTEGER,
    user_id INTEGER,
    role TEXT NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER,
    user_id INTEGER,
    name TEXT,
    messages INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0,
    xp INTEGER DEFAULT 0,
    warns INTEGER DEFAULT 0,
    joined_at INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS auto_replies (
    chat_id INTEGER,
    keyword TEXT,
    response TEXT,
    PRIMARY KEY(chat_id, keyword)
);

CREATE TABLE IF NOT EXISTS bad_words (
    chat_id INTEGER,
    word TEXT,
    PRIMARY KEY(chat_id, word)
);

CREATE TABLE IF NOT EXISTS games (
    chat_id INTEGER PRIMARY KEY,
    roulette_active INTEGER DEFAULT 0,
    roulette_message_id INTEGER DEFAULT 0,
    roulette_owner INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bot_admins (
    user_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    expires_at INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
""")

if OWNER_ID:
    cur.execute("INSERT OR IGNORE INTO bot_admins(user_id) VALUES(?)", (OWNER_ID,))
db.commit()

# ============================================================
# ذاكرة مؤقتة
# ============================================================

flood_cache = defaultdict(deque)
pending_actions = {}
roulette_players = defaultdict(set)
roulette_locks = defaultdict(asyncio.Lock)

DEFAULT_BADWORDS = {
    "كلمة_ممنوعة_مثال",
}

# ============================================================
# أدوات عامة
# ============================================================

def ensure_group(chat_id: int):
    cur.execute("INSERT OR IGNORE INTO groups(chat_id) VALUES(?)", (chat_id,))
    cur.execute("INSERT OR IGNORE INTO games(chat_id) VALUES(?)", (chat_id,))
    db.commit()

def get_group(chat_id: int):
    ensure_group(chat_id)
    cur.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
    return cur.fetchone()

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID and OWNER_ID != 0

ROLE_LEVEL = {
    "member": 0,
    "vip": 1,
    "admin": 2,
    "manager": 3,
    "owner": 4,
    "bot_owner": 5,
}

ROLE_AR = {
    "member": "عضو",
    "vip": "مميز",
    "admin": "ادمن",
    "manager": "مدير",
    "owner": "مالك",
    "bot_owner": "المالك الأساسي",
}

def get_role(chat_id: int, user_id: int) -> str:
    if is_owner(user_id):
        return "bot_owner"
    cur.execute(
        "SELECT role FROM roles WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    row = cur.fetchone()
    return row["role"] if row else "member"

def role_level(chat_id: int, user_id: int) -> int:
    return ROLE_LEVEL[get_role(chat_id, user_id)]

def can_manage_role(actor_role: str, target_role: str, new_role: str) -> bool:
    actor = ROLE_LEVEL[actor_role]
    target = ROLE_LEVEL[target_role]
    new = ROLE_LEVEL[new_role]

    if actor_role == "bot_owner":
        return target < actor and new < actor
    if actor_role == "owner":
        return target < actor and new <= ROLE_LEVEL["manager"]
    if actor_role == "manager":
        return target < actor and new <= ROLE_LEVEL["admin"]
    if actor_role == "admin":
        return target < actor and new <= ROLE_LEVEL["vip"]
    return False

def set_role(chat_id: int, user_id: int, role: str):
    if role == "member":
        cur.execute(
            "DELETE FROM roles WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
    else:
        cur.execute(
            """INSERT INTO roles(chat_id,user_id,role)
               VALUES(?,?,?)
               ON CONFLICT(chat_id,user_id)
               DO UPDATE SET role=excluded.role""",
            (chat_id, user_id, role),
        )
    db.commit()

def mention(user) -> str:
    name = user.full_name or "المستخدم"
    return f'<a href="tg://user?id={user.id}">{name}</a>'

def get_user(chat_id: int, user_id: int, name: str = ""):
    cur.execute(
        """INSERT INTO users(chat_id,user_id,name,joined_at)
           VALUES(?,?,?,?)
           ON CONFLICT(chat_id,user_id)
           DO UPDATE SET name=excluded.name""",
        (chat_id, user_id, name, int(time.time())),
    )
    db.commit()

def add_activity(chat_id: int, user_id: int, name: str, points=1, xp=1):
    get_user(chat_id, user_id, name)
    cur.execute(
        """UPDATE users
           SET messages=messages+1, points=points+?, xp=xp+?
           WHERE chat_id=? AND user_id=?""",
        (points, xp, chat_id, user_id),
    )
    db.commit()

async def is_telegram_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except Exception:
        return False

async def bot_has_permission(bot, chat_id: int, permission: str = "delete") -> bool:
    try:
        me = await bot.get_me()
        m = await bot.get_chat_member(chat_id, me.id)
        if m.status != ChatMemberStatus.ADMINISTRATOR:
            return False
        if permission == "delete":
            return bool(getattr(m, "can_delete_messages", False))
        if permission == "restrict":
            return bool(getattr(m, "can_restrict_members", False))
        if permission == "invite":
            return bool(getattr(m, "can_invite_users", False))
        return True
    except Exception:
        return False

async def safe_delete(message):
    try:
        await message.delete()
        return True
    except Exception:
        return False

async def send_private(update: Update, text: str, reply_markup=None):
    try:
        await update.effective_user.send_message(
            text, reply_markup=reply_markup
        )
    except Exception:
        pass

# ============================================================
# لوحة التحكم
# ============================================================

def main_panel():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("رسالة الحذف"), KeyboardButton("منع الردود")],
            [KeyboardButton("الردود المضافة"), KeyboardButton("إضافة رد")],
            [KeyboardButton("الاشتراك الإجباري")],
            [KeyboardButton("تعيين قناة"), KeyboardButton("تعيين رابط المجموعة")],
            [KeyboardButton("الحماية 🛡️")],
            [KeyboardButton("الألعاب 🎮")],
            [KeyboardButton("الرتب 👑")],
            [KeyboardButton("التوب 10")],
            [KeyboardButton("إعادة تشغيل البوت 🔄")],
        ],
        resize_keyboard=True,
    )

def protection_panel():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("مانع الروابط"), KeyboardButton("مانع السبام")],
            [KeyboardButton("مانع الكلمات"), KeyboardButton("فلتر المحتوى")],
            [KeyboardButton("فلتر GIF"), KeyboardButton("فلتر الملصقات")],
            [KeyboardButton("ترحيب الأعضاء"), KeyboardButton("حالة الحماية")],
            [KeyboardButton("رجوع ↩️")],
        ],
        resize_keyboard=True,
    )

def games_panel():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("الروليت 🎰"), KeyboardButton("حجر ورقة مقص ✊")],
            [KeyboardButton("نرد 🎲"), KeyboardButton("توب اللاعبين 🏆")],
            [KeyboardButton("نقاطي ⭐"), KeyboardButton("معلوماتي 👤")],
            [KeyboardButton("رجوع ↩️")],
        ],
        resize_keyboard=True,
    )

def roles_panel():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("رفع مالك"), KeyboardButton("تنزيل مالك")],
            [KeyboardButton("رفع مدير"), KeyboardButton("تنزيل مدير")],
            [KeyboardButton("رفع ادمن"), KeyboardButton("تنزيل ادمن")],
            [KeyboardButton("رفع مميز"), KeyboardButton("تنزيل مميز")],
            [KeyboardButton("تنزيل الكل")],
            [KeyboardButton("قائمة الرتب")],
            [KeyboardButton("رجوع ↩️")],
        ],
        resize_keyboard=True,
    )

async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if not is_owner(user.id) and not await is_bot_admin_anywhere(user.id):
        await update.effective_message.reply_text(
            "❌ هذه اللوحة خاصة بالمشرفين المعتمدين."
        )
        return
    await update.effective_message.reply_text(
        "لوحة تحكم البوت الخاصة بالمطورين\n\nاختر العملية المطلوبة:",
        reply_markup=main_panel(),
    )

async def is_bot_admin_anywhere(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    cur.execute("SELECT 1 FROM bot_admins WHERE user_id=?", (user_id,))
    return cur.fetchone() is not None

# ============================================================
# /start والأوامر
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🤖 أهلاً بك في {BOT_NAME}\n\n"
        "🛡️ حماية متقدمة للمجموعات\n"
        "🎮 ألعاب وتحديات\n"
        "💬 نظام ردود\n"
        "🏆 نقاط وتفاعل\n"
        "👑 نظام رتب وصلاحيات\n"
        "🔞 فلترة محتوى\n\n"
        "أضفني إلى مجموعتك وارفعني مشرفاً حتى تعمل ميزات الحماية."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📚 الأوامر", callback_data="help"),
            InlineKeyboardButton("🛡️ الحماية", callback_data="help_security"),
        ],
        [
            InlineKeyboardButton("🎮 الألعاب", callback_data="help_games"),
        ],
    ])
    await update.effective_message.reply_text(text, reply_markup=kb)

async def commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>Bot Security | الأوامر</b>\n\n"
        "① <b>أوامر الإدارة</b>\n"
        "② <b>أوامر الإعدادات</b>\n"
        "③ <b>أوامر القفل - الفتح</b>\n"
        "④ <b>أوامر التسلية</b>\n"
        "⑤ <b>الأوامر الخدمية</b>\n\n"
        "استخدم الأزرار التالية للتنقل."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("① الإدارة", callback_data="cmd_admin"),
         InlineKeyboardButton("② الإعدادات", callback_data="cmd_settings")],
        [InlineKeyboardButton("③ القفل - الفتح", callback_data="cmd_lock"),
         InlineKeyboardButton("④ التسلية", callback_data="cmd_fun")],
        [InlineKeyboardButton("⑤ الخدمات", callback_data="cmd_service")],
    ])
    await update.effective_message.reply_text(text, reply_markup=kb)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    pages = {
        "help": (
            "📚 <b>قائمة الأوامر</b>\n\n"
            "الاوامر\npanel\nروليت\nتوب\nنقاطي\nمعلوماتي\n"
            "رفع ادمن\nرفع مميز\nتنزيل ادمن\nتنزيل مميز\n"
            "تنزيل الكل\nتحذير\nكتم\nحظر\nطرد\nمسح"
        ),
        "help_security": (
            "🛡️ <b>الحماية</b>\n\n"
            "مانع الردود\nمانع الروابط\nمانع السبام\nمانع الكلمات\n"
            "فلتر المحتوى\nفلتر GIF\nفلتر الملصقات\nتحذيرات\nسجل المخالفات"
        ),
        "help_games": (
            "🎮 <b>الألعاب</b>\n\n"
            "روليت\nنرد\nحجر ورقة مقص\nتوب\nنقاطي\nمعلوماتي"
        ),
        "cmd_admin": (
            "① <b>أوامر الإدارة</b>\n\n"
            "رفع مالك / تنزيل مالك\n"
            "رفع مدير / تنزيل مدير\n"
            "رفع ادمن / تنزيل ادمن\n"
            "رفع مميز / تنزيل مميز\n"
            "تنزيل الكل\n"
            "كتم / فك كتم\nحظر / فك حظر\nطرد\nتحذير\nمسح"
        ),
        "cmd_settings": (
            "② <b>أوامر الإعدادات</b>\n\n"
            "رسالة الحذف\nإضافة رد\nالردود المضافة\n"
            "الاشتراك الإجباري\nتعيين قناة\nتعيين رابط المجموعة"
        ),
        "cmd_lock": (
            "③ <b>أوامر القفل - الفتح</b>\n\n"
            "قفل الروابط\nفتح الروابط\n"
            "قفل GIF\nفتح GIF\n"
            "قفل الملصقات\nفتح الملصقات\n"
            "قفل الردود\nفتح الردود"
        ),
        "cmd_fun": (
            "④ <b>أوامر التسلية</b>\n\n"
            "روليت\nنرد\nحجر ورقة مقص\nتوب\nنقاطي\nمعلوماتي"
        ),
        "cmd_service": (
            "⑤ <b>الأوامر الخدمية</b>\n\n"
            "ايدي\nمعلوماتي\nتوب\nالاوامر\n"
            "معلومات المجموعة"
        ),
    }
    if data in pages:
        await q.message.reply_text(pages[data], parse_mode="HTML")

# ============================================================
# إدارة الرتب
# ============================================================

async def resolve_user(bot, chat_id: int, value: str):
    value = value.strip()
    if value.isdigit():
        try:
            return await bot.get_chat_member(chat_id, int(value))
        except Exception:
            return None
    username = value.replace("@", "")
    try:
        return await bot.get_chat_member(chat_id, f"@{username}")
    except Exception:
        try:
            chat = await bot.get_chat(f"@{username}")
            return await bot.get_chat_member(chat_id, chat.id)
        except Exception:
            return None

async def promote(update, context, new_role):
    msg = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    actor_role = get_role(chat.id, actor.id)
    target_member = msg.reply_to_message.from_user if msg.reply_to_message else None

    if not target_member and context.args:
        member = await resolve_user(context.bot, chat.id, context.args[0])
        target_member = member.user if member else None

    if not target_member:
        await msg.reply_text("استخدم الأمر بالرد على الشخص أو أرسل الآيدي/اليوزرنيم.")
        return

    target_role = get_role(chat.id, target_member.id)

    if new_role == "owner" and actor_role != "bot_owner":
        await msg.reply_text("❌ المالك الأساسي وحده يستطيع رفع مالك.")
        return

    if not can_manage_role(actor_role, target_role, new_role):
        await msg.reply_text("❌ رتبتك لا تسمح لك بتنفيذ هذا الإجراء.")
        return

    set_role(chat.id, target_member.id, new_role)
    await msg.reply_text(
        f"✅ تم رفع {mention(target_member)} إلى رتبة <b>{ROLE_AR[new_role]}</b>.",
        parse_mode="HTML",
    )

async def demote(update, context, target_role):
    msg = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    actor_role = get_role(chat.id, actor.id)
    target_member = msg.reply_to_message.from_user if msg.reply_to_message else None

    if not target_member and context.args:
        member = await resolve_user(context.bot, chat.id, context.args[0])
        target_member = member.user if member else None

    if not target_member:
        await msg.reply_text("استخدم الأمر بالرد على الشخص أو أرسل الآيدي/اليوزرنيم.")
        return

    actual = get_role(chat.id, target_member.id)
    if actual != target_role:
        await msg.reply_text(
            f"❌ رتبة العضو الحالية ليست {ROLE_AR[target_role]}."
        )
        return

    if not can_manage_role(actor_role, actual, "member"):
        await msg.reply_text("❌ رتبتك لا تسمح لك بتنزيل هذا العضو.")
        return

    set_role(chat.id, target_member.id, "member")
    await msg.reply_text(
        f"✅ تم تنزيل {mention(target_member)} من رتبة {ROLE_AR[target_role]}.",
        parse_mode="HTML",
    )

async def demote_all(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    actor_role = get_role(chat.id, actor.id)

    if actor_role not in ("bot_owner", "owner", "manager", "admin"):
        await msg.reply_text("❌ رتبتك لا تسمح باستخدام تنزيل الكل.")
        return

    actor_level = ROLE_LEVEL[actor_role]
    cur.execute(
        "SELECT user_id, role FROM roles WHERE chat_id=?",
        (chat.id,),
    )
    rows = cur.fetchall()
    count = 0

    for row in rows:
        target_role = row["role"]
        if ROLE_LEVEL[target_role] < actor_level:
            set_role(chat.id, row["user_id"], "member")
            count += 1

    await msg.reply_text(f"✅ تم تنزيل {count} رتبة حسب صلاحياتك.")

async def list_roles(update, context):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    cur.execute(
        "SELECT user_id, role FROM roles WHERE chat_id=? ORDER BY role DESC",
        (chat.id,),
    )
    rows = cur.fetchall()
    if not rows:
        await update.effective_message.reply_text("لا توجد رتب مخصصة.")
        return
    lines = ["👑 <b>قائمة الرتب</b>\n"]
    for row in rows:
        try:
            member = await context.bot.get_chat_member(chat.id, row["user_id"])
            name = member.user.full_name
        except Exception:
            name = str(row["user_id"])
        lines.append(f"• {ROLE_AR[row['role']]} — {name} — <code>{row['user_id']}</code>")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML"
    )

# ============================================================
# أوامر الإدارة الأساسية
# ============================================================

async def moderate(update, context, action):
    msg = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not msg.reply_to_message:
        await msg.reply_text("❌ استخدم الأمر بالرد على رسالة العضو.")
        return

    target = msg.reply_to_message.from_user
    if target.is_bot:
        return

    if role_level(chat.id, actor.id) <= role_level(chat.id, target.id):
        await msg.reply_text("❌ لا يمكنك تنفيذ هذا الإجراء على رتبة مساوية أو أعلى من رتبتك.")
        return

    try:
        if action == "delete":
            await safe_delete(msg.reply_to_message)
        elif action == "mute":
            await context.bot.restrict_chat_member(
                chat.id,
                target.id,
                permissions={"can_send_messages": False},
            )
            await msg.reply_text(f"🔇 تم كتم {mention(target)}.", parse_mode="HTML")
        elif action == "unmute":
            await context.bot.restrict_chat_member(
                chat.id,
                target.id,
                permissions={
                    "can_send_messages": True,
                    "can_send_audios": True,
                    "can_send_documents": True,
                    "can_send_photos": True,
                    "can_send_videos": True,
                    "can_send_video_notes": True,
                    "can_send_voice_notes": True,
                    "can_send_polls": True,
                    "can_send_other_messages": True,
                    "can_add_web_page_previews": True,
                },
            )
            await msg.reply_text(f"🔊 تم فتح الكتم عن {mention(target)}.", parse_mode="HTML")
        elif action == "ban":
            await context.bot.ban_chat_member(chat.id, target.id)
            await msg.reply_text(f"🚫 تم حظر {mention(target)}.", parse_mode="HTML")
        elif action == "unban":
            await context.bot.unban_chat_member(chat.id, target.id)
            await msg.reply_text(f"✅ تم فك الحظر.", parse_mode="HTML")
        elif action == "kick":
            await context.bot.ban_chat_member(chat.id, target.id)
            await context.bot.unban_chat_member(chat.id, target.id)
            await msg.reply_text(f"👢 تم طرد {mention(target)}.", parse_mode="HTML")
    except Exception as e:
        logger.warning("Moderation error: %s", e)
        await msg.reply_text("⚠️ لم أستطع تنفيذ العملية. تأكد من صلاحيات البوت.")

async def warn(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if not msg.reply_to_message:
        await msg.reply_text("استخدم تحذير بالرد على رسالة العضو.")
        return

    target = msg.reply_to_message.from_user
    if role_level(chat.id, actor.id) <= role_level(chat.id, target.id):
        await msg.reply_text("❌ لا يمكنك تحذير رتبة مساوية أو أعلى.")
        return

    get_user(chat.id, target.id, target.full_name)
    cur.execute(
        "UPDATE users SET warns=warns+1 WHERE chat_id=? AND user_id=?",
        (chat.id, target.id),
    )
    db.commit()

    row = get_group(chat.id)
    cur.execute(
        "SELECT warns FROM users WHERE chat_id=? AND user_id=?",
        (chat.id, target.id),
    )
    warns = cur.fetchone()["warns"]
    limit = row["warn_limit"]

    if warns >= limit:
        try:
            await context.bot.restrict_chat_member(
                chat.id,
                target.id,
                permissions={"can_send_messages": False},
            )
            await msg.reply_text(
                f"🔇 وصل {mention(target)} إلى {limit} تحذيرات وتم كتمه.",
                parse_mode="HTML",
            )
            cur.execute(
                "UPDATE users SET warns=0 WHERE chat_id=? AND user_id=?",
                (chat.id, target.id),
            )
            db.commit()
        except Exception:
            await msg.reply_text(
                f"⚠️ تحذير {mention(target)}: {warns}/{limit}",
                parse_mode="HTML",
            )
    else:
        await msg.reply_text(
            f"⚠️ تم تحذير {mention(target)}: {warns}/{limit}",
            parse_mode="HTML",
        )

async def delete_n(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if role_level(chat.id, update.effective_user.id) < ROLE_LEVEL["admin"]:
        return
    n = 1
    if context.args and context.args[0].isdigit():
        n = min(int(context.args[0]), 100)
    deleted = 0
    try:
        async for m in context.bot.get_updates():
            _ = m
    except Exception:
        pass
    await msg.reply_text(
        "ℹ️ الحذف الجماعي يحتاج تحديد رسائل ضمن نطاق Telegram. "
        "استخدم مسح بالرد أو مسح رقم ضمن نسخة الويب/اللوحة."
    )

# ============================================================
# الردود
# ============================================================

async def add_reply_command(update, context):
    if not await is_bot_admin_anywhere(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "الاستخدام:\nإضافة_رد كلمة | الرد"
        )
        return
    raw = " ".join(context.args)
    if "|" not in raw:
        await update.effective_message.reply_text(
            "استخدم الفاصل | بين الكلمة والرد."
        )
        return
    keyword, response = [x.strip() for x in raw.split("|", 1)]
    chat_id = update.effective_chat.id
    cur.execute(
        """INSERT INTO auto_replies(chat_id,keyword,response)
           VALUES(?,?,?)
           ON CONFLICT(chat_id,keyword)
           DO UPDATE SET response=excluded.response""",
        (chat_id, keyword, response),
    )
    db.commit()
    await update.effective_message.reply_text("✅ تم حفظ الرد.")

async def show_replies(update, context):
    chat_id = update.effective_chat.id
    cur.execute(
        "SELECT keyword,response FROM auto_replies WHERE chat_id=?",
        (chat_id,),
    )
    rows = cur.fetchall()
    if not rows:
        await update.effective_message.reply_text("لا توجد ردود مضافة.")
        return
    text = "💬 <b>الردود المضافة</b>\n\n"
    for i, row in enumerate(rows, 1):
        text += f"{i}. <code>{row['keyword']}</code> → {row['response']}\n"
    await update.effective_message.reply_text(text, parse_mode="HTML")

# ============================================================
# الألعاب
# ============================================================

async def roulette(update, context):
    chat = update.effective_chat
    msg = update.effective_message
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("🎰 الروليت تعمل داخل المجموعات.")
        return

    ensure_group(chat.id)
    async with roulette_locks[chat.id]:
        roulette_players[chat.id].clear()
        text = (
            "🎰 <b>بدأنا روليت!</b>\n\n"
            "الكل يسوي رياكشن على هذه الرسالة، "
            "وبعدها اكتب <b>تم</b> حتى أختار شخصاً من المشاركين.\n\n"
            "🔥 شاركوا!"
        )
        sent = await msg.reply_text(text, parse_mode="HTML")
        cur.execute(
            "UPDATE games SET roulette_active=1, roulette_message_id=?, roulette_owner=? WHERE chat_id=?",
            (sent.message_id, update.effective_user.id, chat.id),
        )
        db.commit()

async def finish_roulette(update, context):
    chat = update.effective_chat
    msg = update.effective_message
    cur.execute(
        "SELECT roulette_active,roulette_message_id FROM games WHERE chat_id=?",
        (chat.id,),
    )
    row = cur.fetchone()
    if not row or not row["roulette_active"]:
        return
    if not roulette_players[chat.id]:
        await msg.reply_text("🎰 ماكو مشاركين مسجلين عندي.")
        return

    winner_id = random.choice(list(roulette_players[chat.id]))
    try:
        member = await context.bot.get_chat_member(chat.id, winner_id)
        winner = member.user
        name = mention(winner)
    except Exception:
        name = str(winner_id)

    await msg.reply_text(
        f"🎰 <b>انتهت الروليت!</b>\n\n"
        f"🎯 الفائز هو: {name}\n"
        f"🔥 مبروك!",
        parse_mode="HTML",
    )
    cur.execute(
        "UPDATE games SET roulette_active=0 WHERE chat_id=?",
        (chat.id,),
    )
    db.commit()

async def dice_game(update, context):
    msg = update.effective_message
    value = random.randint(1, 6)
    await msg.reply_text(f"🎲 رمية النرد: <b>{value}</b>", parse_mode="HTML")

async def rps(update, context):
    choices = ["حجر 🪨", "ورقة 📄", "مقص ✂️"]
    await update.effective_message.reply_text(
        f"✊ أنا اخترت: <b>{random.choice(choices)}</b>",
        parse_mode="HTML",
    )

async def my_points(update, context):
    chat = update.effective_chat
    user = update.effective_user
    get_user(chat.id, user.id, user.full_name)
    cur.execute(
        "SELECT points,xp,messages,warns FROM users WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    )
    row = cur.fetchone()
    await update.effective_message.reply_text(
        f"👤 {mention(user)}\n\n"
        f"⭐ النقاط: {row['points']}\n"
        f"✨ XP: {row['xp']}\n"
        f"💬 الرسائل: {row['messages']}\n"
        f"⚠️ التحذيرات: {row['warns']}",
        parse_mode="HTML",
    )

async def top_users(update, context):
    chat = update.effective_chat
    cur.execute(
        """SELECT name,points,xp,messages FROM users
           WHERE chat_id=? ORDER BY points DESC, messages DESC LIMIT 10""",
        (chat.id,),
    )
    rows = cur.fetchall()
    if not rows:
        await update.effective_message.reply_text("لا توجد إحصائيات بعد.")
        return
    text = "🏆 <b>TOP 10</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{medal} {row['name']} — ⭐ {row['points']} — 💬 {row['messages']}\n"
    await update.effective_message.reply_text(text, parse_mode="HTML")

# ============================================================
# الاشتراك الإجباري
# ============================================================

async def check_subscription(update, context) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return True

    row = get_group(chat.id)
    if not row["subscription_enabled"]:
        return True

    if role_level(chat.id, user.id) >= ROLE_LEVEL["admin"]:
        return True

    channel = row["subscription_channel"]
    if not channel:
        return True

    try:
        member = await context.bot.get_chat_member(channel, user.id)
        if member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,
        ):
            return True
    except Exception:
        pass

    await safe_delete(update.effective_message)
    link = row["subscription_link"] or f"https://t.me/{channel.lstrip('@')}"
    await context.bot.send_message(
        chat.id,
        f"⚠️ {mention(user)}\nعليك الاشتراك بالقناة أولاً.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 الاشتراك بالقناة", url=link)]
        ]),
    )
    return False

# ============================================================
# فلترة الكلمات والروابط والسبام
# ============================================================

URL_RE = re.compile(r"(https?://|t\.me/|www\.)", re.I)

def contains_bad_word(chat_id: int, text: str) -> bool:
    cur.execute("SELECT word FROM bad_words WHERE chat_id=?", (chat_id,))
    words = [r["word"].lower() for r in cur.fetchall()]
    if not words:
        return False
    low = text.lower()
    return any(w and w in low for w in words)

async def anti_flood(update, context) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    row = get_group(chat.id)
    if not row["flood_enabled"]:
        return False
    if role_level(chat.id, user.id) >= ROLE_LEVEL["admin"]:
        return False

    key = (chat.id, user.id)
    now = time.time()
    q = flood_cache[key]
    while q and now - q[0] > FLOOD_WINDOW:
        q.popleft()
    q.append(now)

    if len(q) > FLOOD_LIMIT:
        await safe_delete(update.effective_message)
        try:
            await context.bot.restrict_chat_member(
                chat.id,
                user.id,
                permissions={"can_send_messages": False},
            )
            await context.bot.send_message(
                chat.id,
                f"🚫 تم كتم {mention(user)} بسبب السبام.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        q.clear()
        return True
    return False

# ============================================================
# فحص المحتوى الاختياري
# ============================================================

NSFW_ENGINE = None

def load_nsfw_engine():
    global NSFW_ENGINE
    if NSFW_ENGINE is not None:
        return NSFW_ENGINE
    try:
        from nudenet import NudeDetector
        NSFW_ENGINE = NudeDetector()
        return NSFW_ENGINE
    except Exception as e:
        logger.warning("NSFW engine unavailable: %s", e)
        return None

def scan_image_file(path: str) -> bool:
    """
    يرجع True إذا تم اكتشاف محتوى حساس بواسطة NudeNet.
    إذا لم تكن المكتبة مثبتة يرجع False حتى لا يتعطل البوت.
    """
    detector = load_nsfw_engine()
    if detector is None:
        return False
    try:
        detections = detector.detect(path)
        sensitive_labels = {
            "FEMALE_BREAST_EXPOSED",
            "FEMALE_GENITALIA_EXPOSED",
            "MALE_GENITALIA_EXPOSED",
            "BUTTOCKS_EXPOSED",
            "ANUS_EXPOSED",
            "FEMALE_BREAST_COVERED",
        }
        return any(
            d.get("class") in sensitive_labels and float(d.get("score", 0)) >= 0.60
            for d in detections
        )
    except Exception as e:
        logger.warning("NSFW scan failed: %s", e)
        return False

async def inspect_media(update, context) -> bool:
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False

    row = get_group(chat.id)
    media = None
    enabled = False

    if msg.photo and row["media_filter_enabled"]:
        media = msg.photo[-1]
        enabled = True
    elif msg.animation and row["gif_filter_enabled"]:
        media = msg.animation
        enabled = True
    elif msg.sticker and row["sticker_filter_enabled"]:
        media = msg.sticker
        enabled = True

    if not media or not enabled:
        return False

    # لا نفحص إذا كان البوت لا يملك حذف الرسائل.
    if not await bot_has_permission(context.bot, chat.id, "delete"):
        return False

    tmp = None
    try:
        file = await context.bot.get_file(media.file_id)
        suffix = ".bin"
        if msg.photo:
            suffix = ".jpg"
        elif msg.animation:
            suffix = ".mp4"
        elif msg.sticker:
            suffix = ".webp"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            tmp = f.name

        await file.download_to_drive(tmp)

        # NudeNet يعمل أساساً على الصور. GIF/الفيديو يحتاج تحويل إطارات
        # في نسخة موسعة؛ هنا نطبق الفحص المباشر عندما يكون الملف صورة/ويب.
        if suffix in (".jpg", ".webp"):
            flagged = await asyncio.to_thread(scan_image_file, tmp)
        else:
            flagged = False

        if flagged:
            await safe_delete(msg)
            await context.bot.send_message(
                chat.id,
                f"🔞 تم حذف محتوى مخالف من {mention(msg.from_user)}.",
                parse_mode="HTML",
            )
            return True
    except Exception as e:
        logger.warning("Media inspection error: %s", e)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return False

# ============================================================
# رسالة لوحة التحكم
# ============================================================

async def handle_panel_text(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not await is_bot_admin_anywhere(user.id):
        return False

    text = (msg.text or "").strip()
    action = pending_actions.get(user.id)

    if action:
        if action == "delete_message":
            cur.execute(
                """UPDATE groups SET delete_message=? WHERE chat_id=?""",
                (text, 0),
            )
            db.commit()
            pending_actions.pop(user.id, None)
            await msg.reply_text("✅ تم حفظ رسالة الحذف.")
            return True

        if action == "subscription_channel":
            parts = text.split()
            channel = parts[0]
            link = parts[1] if len(parts) > 1 else f"https://t.me/{channel.lstrip('@')}"
            pending_actions.pop(user.id, None)
            context.user_data["subscription_channel"] = channel
            context.user_data["subscription_link"] = link
            await msg.reply_text("✅ تم حفظ إعداد الاشتراك. استخدم تعيين الاشتراك للمجموعة.")
            return True

        if action == "add_reply":
            if "|" not in text:
                await msg.reply_text("استخدم: الكلمة | الرد")
                return True
            keyword, response = [x.strip() for x in text.split("|", 1)]
            chat_id = context.user_data.get("panel_chat_id", 0)
            cur.execute(
                """INSERT INTO auto_replies(chat_id,keyword,response)
                   VALUES(?,?,?)
                   ON CONFLICT(chat_id,keyword)
                   DO UPDATE SET response=excluded.response""",
                (chat_id, keyword, response),
            )
            db.commit()
            pending_actions.pop(user.id, None)
            await msg.reply_text("✅ تم حفظ الرد.")
            return True

    if text == "رسالة الحذف":
        pending_actions[user.id] = "delete_message"
        await msg.reply_text("أرسل رسالة الحذف الجديدة:")
        return True

    if text == "إضافة رد":
        pending_actions[user.id] = "add_reply"
        context.user_data["panel_chat_id"] = update.effective_chat.id
        await msg.reply_text("أرسل بالشكل:\nالكلمة | الرد")
        return True

    if text == "الردود المضافة":
        await show_replies(update, context)
        return True

    if text == "منع الردود":
        context.user_data["panel_chat_id"] = update.effective_chat.id
        await msg.reply_text("استخدم /منع_الردود أو /فتح_الردود داخل المجموعة.")
        return True

    if text == "الحماية 🛡️":
        await msg.reply_text("🛡️ إعدادات الحماية:", reply_markup=protection_panel())
        return True

    if text == "الألعاب 🎮":
        await msg.reply_text("🎮 الألعاب:", reply_markup=games_panel())
        return True

    if text == "الرتب 👑":
        await msg.reply_text("👑 إدارة الرتب:", reply_markup=roles_panel())
        return True

    if text == "توب 10":
        await top_users(update, context)
        return True

    if text == "رجوع ↩️":
        await msg.reply_text("تم الرجوع.", reply_markup=main_panel())
        return True

    if text == "الروليت 🎰":
        await roulette(update, context)
        return True

    if text == "نرد 🎲":
        await dice_game(update, context)
        return True

    if text == "حجر ورقة مقص ✊":
        await rps(update, context)
        return True

    if text == "نقاطي ⭐":
        await my_points(update, context)
        return True

    if text == "معلوماتي 👤":
        await my_points(update, context)
        return True

    if text == "قائمة الرتب":
        await list_roles(update, context)
        return True

    # تفعيل/تعطيل إعدادات الحماية من اللوحة
    chat_id = update.effective_chat.id
    if text in ("مانع الروابط", "مانع السبام", "مانع الكلمات",
                "فلتر المحتوى", "فلتر GIF", "فلتر الملصقات",
                "ترحيب الأعضاء"):
        column = {
            "مانع الروابط": "links_enabled",
            "مانع السبام": "flood_enabled",
            "مانع الكلمات": "badwords_enabled",
            "فلتر المحتوى": "media_filter_enabled",
            "فلتر GIF": "gif_filter_enabled",
            "فلتر الملصقات": "sticker_filter_enabled",
            "ترحيب الأعضاء": "welcome_enabled",
        }[text]
        row = get_group(chat_id)
        value = 0 if row[column] else 1
        cur.execute(
            f"UPDATE groups SET {column}=? WHERE chat_id=?",
            (value, chat_id),
        )
        db.commit()
        await msg.reply_text(f"✅ {text}: {'مفعّل' if value else 'متوقف'}")
        return True

    if text == "حالة الحماية":
        row = get_group(chat_id)
        await msg.reply_text(
            "🛡️ حالة الحماية:\n\n"
            f"الروابط: {'✅' if row['links_enabled'] else '❌'}\n"
            f"السبام: {'✅' if row['flood_enabled'] else '❌'}\n"
            f"الكلمات: {'✅' if row['badwords_enabled'] else '❌'}\n"
            f"الصور: {'✅' if row['media_filter_enabled'] else '❌'}\n"
            f"GIF: {'✅' if row['gif_filter_enabled'] else '❌'}\n"
            f"الملصقات: {'✅' if row['sticker_filter_enabled'] else '❌'}"
        )
        return True

    # أوامر الرتب من لوحة التحكم
    role_buttons = {
        "رفع مالك": ("promote", "owner"),
        "رفع مدير": ("promote", "manager"),
        "رفع ادمن": ("promote", "admin"),
        "رفع مميز": ("promote", "vip"),
        "تنزيل مالك": ("demote", "owner"),
        "تنزيل مدير": ("demote", "manager"),
        "تنزيل ادمن": ("demote", "admin"),
        "تنزيل مميز": ("demote", "vip"),
    }
    if text in role_buttons:
        typ, role = role_buttons[text]
        pending_actions[user.id] = f"{typ}:{role}"
        await msg.reply_text("أرسل آيدي العضو أو يوزرنيمه، أو استخدم الأمر بالرد.")
        return True

    if text == "تنزيل الكل":
        await demote_all(update, context)
        return True

    return False

# ============================================================
# المعالج الرئيسي للرسائل
# ============================================================

async def all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    chat = update.effective_chat
    user = update.effective_user
    if not user or not chat:
        return

    # لوحة المطور في الخاص
    if chat.type == ChatType.PRIVATE:
        if await handle_panel_text(update, context):
            return
        if msg.text == "/panel":
            await panel(update, context)
            return
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    ensure_group(chat.id)
    get_user(chat.id, user.id, user.full_name)

    # لا نطبق الحماية على مشرفي Telegram الأساسيين والرتب العالية.
    if await is_telegram_admin(context.bot, chat.id, user.id):
        return

    # الاشتراك الإجباري
    if not await check_subscription(update, context):
        return

    # أمر تم الخاص بالروليت
    if msg.text and msg.text.strip() == "تم":
        await finish_roulette(update, context)
        return

    # تسجيل المشاركين إذا تفاعلوا لا يتم عبر الرسائل؛ هذا يحتاج reaction update
    # لذلك نعتمد في هذه النسخة على أمر "انضم للروليت".
    if msg.text and msg.text.strip() == "انضم للروليت":
        roulette_players[chat.id].add(user.id)
        await msg.reply_text(f"✅ تم تسجيل {mention(user)} في الروليت.", parse_mode="HTML")
        return

    # فلتر المحتوى
    if await inspect_media(update, context):
        return

    # فلترة الروابط
    row = get_group(chat.id)
    if msg.text and row["links_enabled"] and URL_RE.search(msg.text):
        await safe_delete(msg)
        return

    # فلترة الكلمات
    if msg.text and row["badwords_enabled"] and contains_bad_word(chat.id, msg.text):
        await safe_delete(msg)
        await context.bot.send_message(
            chat.id,
            f"⚠️ تم حذف رسالة {mention(user)} لاحتوائها على كلمة ممنوعة.",
            parse_mode="HTML",
        )
        return

    # مكافحة السبام
    if await anti_flood(update, context):
        return

    # منع الردود
    if msg.reply_to_message and row["replies_enabled"]:
        replied = msg.reply_to_message.from_user
        if replied and replied.id != user.id and role_level(chat.id, user.id) < ROLE_LEVEL["admin"]:
            await safe_delete(msg)
            delete_text = row["delete_message"] or "ممنوع إرسال الردود"
            await context.bot.send_message(
                chat.id,
                f"{mention(user)} {delete_text}",
                parse_mode="HTML",
            )
            return

    # النشاط
    add_activity(chat.id, user.id, user.full_name, points=1, xp=1)

    # الردود التلقائية
    if msg.text:
        cur.execute(
            "SELECT response FROM auto_replies WHERE chat_id=? AND keyword=?",
            (chat.id, msg.text.strip()),
        )
        r = cur.fetchone()
        if r:
            await msg.reply_text(r["response"])

# ============================================================
# الأوامر النصية
# ============================================================

async def disable_replies(update, context):
    chat = update.effective_chat
    if role_level(chat.id, update.effective_user.id) < ROLE_LEVEL["admin"]:
        return
    cur.execute("UPDATE groups SET replies_enabled=1 WHERE chat_id=?", (chat.id,))
    db.commit()
    await update.effective_message.reply_text("🛡️ تم تفعيل منع الردود.")

async def enable_replies(update, context):
    chat = update.effective_chat
    if role_level(chat.id, update.effective_user.id) < ROLE_LEVEL["admin"]:
        return
    cur.execute("UPDATE groups SET replies_enabled=0 WHERE chat_id=?", (chat.id,))
    db.commit()
    await update.effective_message.reply_text("✅ تم إيقاف منع الردود.")

async def lock_links(update, context):
    chat = update.effective_chat
    if role_level(chat.id, update.effective_user.id) < ROLE_LEVEL["admin"]:
        return
    cur.execute("UPDATE groups SET links_enabled=1 WHERE chat_id=?", (chat.id,))
    db.commit()
    await update.effective_message.reply_text("🔒 تم قفل الروابط.")

async def unlock_links(update, context):
    chat = update.effective_chat
    if role_level(chat.id, update.effective_user.id) < ROLE_LEVEL["admin"]:
        return
    cur.execute("UPDATE groups SET links_enabled=0 WHERE chat_id=?", (chat.id,))
    db.commit()
    await update.effective_message.reply_text("🔓 تم فتح الروابط.")

async def user_info(update, context):
    chat = update.effective_chat
    user = update.effective_user
    role = ROLE_AR[get_role(chat.id, user.id)]
    await update.effective_message.reply_text(
        f"👤 <b>معلوماتك</b>\n\n"
        f"الاسم: {user.full_name}\n"
        f"الآيدي: <code>{user.id}</code>\n"
        f"الرتبة: <b>{role}</b>",
        parse_mode="HTML",
    )

# ============================================================
# الأوامر القياسية والـ aliases
# ============================================================

async def role_command(update, context):
    text = update.effective_message.text or ""
    mapping = {
        "رفع مالك": ("promote", "owner"),
        "رفع مدير": ("promote", "manager"),
        "رفع ادمن": ("promote", "admin"),
        "رفع مميز": ("promote", "vip"),
        "تنزيل مالك": ("demote", "owner"),
        "تنزيل مدير": ("demote", "manager"),
        "تنزيل ادمن": ("demote", "admin"),
        "تنزيل مميز": ("demote", "vip"),
    }
    if text in mapping:
        typ, role = mapping[text]
        if typ == "promote":
            await promote(update, context, role)
        else:
            await demote(update, context, role)

# ============================================================
# الترحيب
# ============================================================

async def new_member(update, context):
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    chat = update.effective_chat
    row = get_group(chat.id)
    if not row["welcome_enabled"]:
        return
    for user in msg.new_chat_members:
        if user.is_bot:
            continue
        text = row["welcome_text"].replace("{name}", user.full_name)
        await msg.reply_text(text)

# ============================================================
# إعادة التشغيل
# ============================================================

async def restart(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        await update.effective_message.reply_text("❌ هذا الأمر للمالك الأساسي فقط.")
        return
    await update.effective_message.reply_text("🔄 جاري إعادة تشغيل البوت...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ============================================================
# إعداد التطبيق
# ============================================================

def build_app():
    if not TOKEN:
        raise RuntimeError(
            "ضع توكن البوت في المتغير TOKEN داخل الملف قبل التشغيل."
        )

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("الاوامر", commands))
    app.add_handler(CommandHandler("commands", commands))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("روليت", roulette))
    app.add_handler(CommandHandler("نرد", dice_game))
    app.add_handler(CommandHandler("توب", top_users))
    app.add_handler(CommandHandler("نقاطي", my_points))
    app.add_handler(CommandHandler("معلوماتي", user_info))
    app.add_handler(CommandHandler("إضافة_رد", add_reply_command))
    app.add_handler(CommandHandler("منع_الردود", disable_replies))
    app.add_handler(CommandHandler("فتح_الردود", enable_replies))
    app.add_handler(CommandHandler("قفل_الروابط", lock_links))
    app.add_handler(CommandHandler("فتح_الروابط", unlock_links))
    app.add_handler(CommandHandler("تحذير", warn))
    app.add_handler(CommandHandler("كتم", lambda u, c: moderate(u, c, "mute")))
    app.add_handler(CommandHandler("فك_كتم", lambda u, c: moderate(u, c, "unmute")))
    app.add_handler(CommandHandler("حظر", lambda u, c: moderate(u, c, "ban")))
    app.add_handler(CommandHandler("فك_حظر", lambda u, c: moderate(u, c, "unban")))
    app.add_handler(CommandHandler("طرد", lambda u, c: moderate(u, c, "kick")))
    app.add_handler(CommandHandler("تنزيل_الكل", demote_all))
    app.add_handler(CommandHandler("قائمة_الرتب", list_roles))
    app.add_handler(CommandHandler("إعادة_تشغيل", restart))

    app.add_handler(CallbackQueryHandler(callback_handler))

    # أعضاء جدد
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member)
    )

    # أوامر/رسائل اللوحة والرسائل العامة
    app.add_handler(
        MessageHandler(filters.ALL, all_messages),
        group=10,
    )

    return app

if __name__ == "__main__":
    print("=" * 60)
    print("Bot Security")
    print("Starting...")
    print("=" * 60)
    application = build_app()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
