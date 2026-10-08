"""
ربات تلگرام «فیلتر چارت» برای گروه ترید
python-telegram-bot >= 20

جریان کار:
1. عضو گروه عکس چارت را می‌فرستد.
2. ربات همان عکس را (با نام فرستنده و کپشن اصلی) دوباره ارسال می‌کند، کیبورد اینلاین را زیرش می‌گذارد
   و پیام اصلی را پاک می‌کند. (ربات نمی‌تواند پیام دیگران را ویرایش کند و کیبورد هم نمی‌تواند به پیام
   دیگران اضافه شود؛ برای همین باید عکس را دوباره بفرستد.)
3. فقط فرستنده‌ی عکس می‌تواند دکمه‌ها را بزند؛ هر کلیک وضعیت فیلتر را بین ✅ و 🔴 عوض می‌کند.
4. با «ثبت نهایی» کپشن همان عکس ویرایش می‌شود و خلاصه‌ی فیلترها نمایش داده می‌شود.

وضعیت دکمه‌ها داخل callback_data هر دکمه است و با ری‌استارت از بین نمی‌رود.

آمار:
- هر بار که «ثبت نهایی» یا «فقط توضیح» زده شود، نتیجه در فایل stats.db (SQLite) ذخیره می‌شود.
- دستورها: «آمار» (انتخاب بازه)، «آمار روزانه»، «آمار هفتگی»، «آمار ماهانه»
  یا /stats  /daily  /weekly  /monthly
- فقط چارت‌های «ثبت نهایی»شده شمرده می‌شوند، نه «فقط توضیح».
- بازه‌ها بر اساس وقت تهران است: روز از ۰۰:۰۰، هفته از شنبه، ماه بر اساس ماه شمسی.
"""

import html
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("trade-filter-bot")

# ----------------------------------------------------------------------------
# تنظیمات
# ----------------------------------------------------------------------------

# فهرست فیلترها. برای افزودن فیلتر، یک خط جدید با یک id تازه اضافه کنید.
# برای حذف، خط را پاک کنید. id هر فیلتر را هرگز عوض یا دوباره استفاده نکنید،
# چون کیبورد پیام‌های قدیمی بر پایه‌ی همین id کار می‌کند. ترتیب نمایش همان ترتیب لیست است.
FILTERS = [
    {"id": 0, "name": "اسپایک"},
    {"id": 1, "name": "لگ ۳"},
    {"id": 2, "name": "درایو یک روی پاز"},
    {"id": 3, "name": "هم جهت با روند"},
    {"id": 4, "name": "تناسب درایوها"},
    {"id": 5, "name": "فیبو"},
    {"id": 6, "name": "RSI"},
]

COLUMNS = 2  # تعداد دکمه در هر ردیف
ON_MARK = "✅"  # وضعیت مثبت
OFF_MARK = "🔴"  # وضعیت منفی / پیش‌فرض
SUBMIT_TEXT = "📌 ثبت نهایی"
SKIP_TEXT = "💬 فقط توضیح"  # بدون فیلتر: فقط کیبورد برداشته می‌شود

# اگر True باشد پیام اصلی عضو پاک می‌شود (ربات باید ادمین با مجوز حذف پیام باشد).
DELETE_ORIGINAL = True

# اگر بخواهید ربات فقط در گروه‌های مشخص کار کند: آیدی‌ها را با کاما در این متغیر محیطی بگذارید
# مثال: ALLOWED_CHAT_IDS=-1001234567890,-1009876543210   (خالی = همه‌ی چت‌ها)
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
}

# فقط این کاربرها می‌توانند آمار بگیرند (آیدی عددی تلگرام، با کاما جدا شود). خالی = همه‌ی اعضا.
# آیدی خودتان را با دستور /myid از ربات بگیرید. مثال: STATS_ADMIN_IDS=123456789
STATS_ADMIN_IDS = {
    int(x) for x in os.environ.get("STATS_ADMIN_IDS", "").split(",") if x.strip()
}
# یا مستقیم همین‌جا:  STATS_ADMIN_IDS |= {123456789}

CAPTION_LIMIT = 1024  # سقف کپشن در تلگرام

# فایل پایگاه‌داده‌ی آمار (کنار bot.py ساخته می‌شود). از این فایل بکاپ بگیرید و پاکش نکنید.
DB_PATH = os.environ.get("STATS_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "stats.db"
)

# وقت تهران (از ۱۴۰۱ ساعت تابستانی لغو شده؛ ثابت +۳:۳۰)
TEHRAN = timezone(timedelta(hours=3, minutes=30))

MAX_PEOPLE_SHOWN = 30  # حداکثر تعداد نفرات در لیست آمار

# ----------------------------------------------------------------------------
# توابع کمکی
# ----------------------------------------------------------------------------

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(n: int) -> str:
    """عدد را با ارقام فارسی برمی‌گرداند."""
    return str(n).translate(_FA_DIGITS)


def is_on(mask: int, filter_id: int) -> bool:
    return bool((mask >> filter_id) & 1)


def build_keyboard(owner_id: int, mask: int) -> InlineKeyboardMarkup:
    buttons = []
    for f in FILTERS:
        mark = ON_MARK if is_on(mask, f["id"]) else OFF_MARK
        buttons.append(
            InlineKeyboardButton(
                f"{mark} {f['name']}",
                callback_data=f"t|{owner_id}|{mask}|{f['id']}",
            )
        )
    rows = [buttons[i : i + COLUMNS] for i in range(0, len(buttons), COLUMNS)]
    rows.append(
        [
            InlineKeyboardButton(SUBMIT_TEXT, callback_data=f"ok|{owner_id}|{mask}"),
            InlineKeyboardButton(SKIP_TEXT, callback_data=f"sk|{owner_id}|{mask}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_summary(mask: int) -> str:
    positives = [f["name"] for f in FILTERS if is_on(mask, f["id"])]
    negatives = [f["name"] for f in FILTERS if not is_on(mask, f["id"])]

    def block(title: str, items: list) -> list:
        lines = [title]
        lines += [f"• {html.escape(n)}" for n in items] or ["• —"]
        return lines

    lines = block("✅ فیلترهای مثبت:", positives)
    lines.append("")
    lines += block("❌ فیلترهای منفی:", negatives)
    lines.append("")
    lines.append(f"تعداد فیلترهای مثبت: {fa(len(positives))} از {fa(len(FILTERS))}")
    return "\n".join(lines)


PERSIAN_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def gregorian_to_jalali(gy: int, gm: int, gd: int):
    """تبدیل تاریخ میلادی به شمسی (الگوریتم حسابی استاندارد)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_str(dt: datetime) -> str:
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return fa(f"{jy}/{jm:02d}/{jd:02d}")


def period_start(period: str, now: datetime) -> datetime:
    """شروع بازه: روز از ۰۰:۰۰، هفته از شنبه، ماه از اول ماه شمسی."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return today
    if period == "week":
        return today - timedelta(days=(today.weekday() - 5) % 7)  # شنبه = ۵
    _, _, jd = gregorian_to_jalali(today.year, today.month, today.day)
    return today - timedelta(days=jd - 1)


# ----------------------------------------------------------------------------
# پایگاه‌داده‌ی آمار
# ----------------------------------------------------------------------------


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS charts (
                   chat_id    INTEGER NOT NULL,
                   message_id INTEGER NOT NULL,
                   user_id    INTEGER NOT NULL,
                   user_name  TEXT,
                   status     TEXT NOT NULL,      -- final | skipped
                   positives  INTEGER,
                   done_at    INTEGER NOT NULL,   -- زمان یونیکس (UTC)
                   PRIMARY KEY (chat_id, message_id)
               )"""
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_charts_done ON charts (chat_id, status, done_at)"
        )


def record_result(chat_id, message_id, user_id, user_name, status, positives=None) -> None:
    """ثبت نتیجه؛ خطای دیتابیس نباید کار ربات را خراب کند."""
    try:
        with db_conn() as db:
            db.execute(
                "INSERT OR REPLACE INTO charts "
                "(chat_id, message_id, user_id, user_name, status, positives, done_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, user_id, user_name, status, positives, int(time.time())),
            )
    except sqlite3.Error:
        logger.exception("ذخیره‌ی آمار ناموفق بود")


def count_positives(mask: int) -> int:
    return sum(1 for f in FILTERS if is_on(mask, f["id"]))


PERIOD_TITLES = {"day": "روزانه", "week": "هفتگی", "month": "ماهانه"}
_MEDALS = ["🥇", "🥈", "🥉"]


def build_stats_text(chat_id: int, period: str) -> str:
    now = datetime.now(TEHRAN)
    start = period_start(period, now)
    if period == "day":
        label = f"امروز ({jalali_str(now)})"
    elif period == "week":
        label = f"این هفته (از شنبه {jalali_str(start)})"
    else:
        jy, jm, _ = gregorian_to_jalali(start.year, start.month, start.day)
        label = f"این ماه ({PERSIAN_MONTHS[jm - 1]} {fa(jy)})"

    with db_conn() as db:
        rows = db.execute(
            "SELECT user_id, user_name FROM charts "
            "WHERE chat_id = ? AND status = 'final' AND done_at >= ? "
            "ORDER BY done_at",
            (chat_id, int(start.timestamp())),
        ).fetchall()

    counts: Counter = Counter()
    names = {}
    for uid, name in rows:
        counts[uid] += 1
        names[uid] = name  # آخرین نام ثبت‌شده

    total = sum(counts.values())
    lines = [f"📊 <b>آمار {PERIOD_TITLES[period]}</b>", label, ""]
    lines.append(f"✅ چارت‌های ثبت نهایی‌شده: <b>{fa(total)}</b>")

    if total:
        lines += ["", "👥 سهم هر فرد:"]
        ranked = counts.most_common()
        for i, (uid, c) in enumerate(ranked[:MAX_PEOPLE_SHOWN]):
            rank = _MEDALS[i] if i < len(_MEDALS) else f"{fa(i + 1)}."
            name = html.escape(names.get(uid) or "بدون‌نام")
            pct = round(c * 100 / total)
            lines.append(f"{rank} {name} — {fa(c)} چارت ({fa(pct)}٪)")
        rest = ranked[MAX_PEOPLE_SHOWN:]
        if rest:
            lines.append(f"و {fa(len(rest))} نفر دیگر ({fa(sum(c for _, c in rest))} چارت)")
    else:
        lines += ["", "هنوز چارتی ثبت نهایی نشده است."]

    lines += ["", "چارت‌هایی که «فقط توضیح» شده‌اند شمرده نمی‌شوند."]
    return "\n".join(lines)


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("📅 روزانه", callback_data="st|day"),
            InlineKeyboardButton("🗓 هفتگی", callback_data="st|week"),
            InlineKeyboardButton("📆 ماهانه", callback_data="st|month"),
        ]]
    )


def header_for(user) -> str:
    return f"👤 <b>{html.escape(user.full_name)}</b>"


# ----------------------------------------------------------------------------
# هندلرها
# ----------------------------------------------------------------------------


async def on_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = msg.from_user
    # ادمین ناشناس / ارسال از طرف کانال: فرستنده‌ی واقعی مشخص نیست
    if user is None or msg.sender_chat is not None:
        return
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return

    caption = header_for(user)
    if msg.caption:
        caption += "\n" + msg.caption_html
    if len(caption) > CAPTION_LIMIT:
        caption = header_for(user)

    kwargs = dict(
        chat_id=msg.chat_id,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=build_keyboard(user.id, 0),
        message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
    )
    try:
        if msg.photo:
            await context.bot.send_photo(photo=msg.photo[-1].file_id, **kwargs)
        else:  # فایل تصویری ارسال‌شده به‌صورت Document
            await context.bot.send_document(document=msg.document.file_id, **kwargs)
    except TelegramError:
        logger.exception("ارسال مجدد چارت ناموفق بود")
        return

    if DELETE_ORIGINAL:
        try:
            await msg.delete()
        except TelegramError:
            logger.warning(
                "حذف پیام اصلی ممکن نشد. ربات را ادمین کنید و مجوز Delete messages بدهید."
            )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split("|")
    try:
        action, owner_id, mask = parts[0], int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    # فقط فرستنده‌ی چارت
    if query.from_user.id != owner_id:
        await query.answer(
            "فقط کسی که چارت را ارسال کرده می‌تواند این دکمه‌ها را تغییر دهد.",
            show_alert=True,
        )
        return

    if action == "t":  # تغییر وضعیت یک فیلتر
        try:
            mask ^= 1 << int(parts[3])
            await query.edit_message_reply_markup(build_keyboard(owner_id, mask))
        except (IndexError, ValueError):
            pass
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        await query.answer()

    elif action == "ok":  # ثبت نهایی
        record_result(
            query.message.chat.id, query.message.message_id, owner_id,
            query.from_user.full_name, "final", count_positives(mask),
        )
        summary = build_summary(mask)
        base = getattr(query.message, "caption_html", None) or header_for(query.from_user)
        caption = f"{base}\n\n{summary}"
        if len(caption) > CAPTION_LIMIT:
            caption = f"{header_for(query.from_user)}\n\n{summary}"
        try:
            await query.edit_message_caption(
                caption=caption, parse_mode=ParseMode.HTML, reply_markup=None
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        await query.answer("ثبت شد ✅")

    elif action == "sk":  # فقط توضیح: کپشن دست‌نخورده می‌ماند و فقط کیبورد برداشته می‌شود
        record_result(
            query.message.chat.id, query.message.message_id, owner_id,
            query.from_user.full_name, "skipped",
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        await query.answer("به‌صورت توضیح ثبت شد")


# ----------------------------------------------------------------------------
# هندلرهای آمار
# ----------------------------------------------------------------------------

STATS_TEXT_RE = re.compile(r"^\s*آمار(?:\s+(روزانه|هفتگی|ماهانه))?\s*$")
_WORD_TO_PERIOD = {"روزانه": "day", "هفتگی": "week", "ماهانه": "month"}
_CMD_TO_PERIOD = {"daily": "day", "weekly": "week", "monthly": "month"}


def _chat_allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


def _stats_allowed(user) -> bool:
    """فقط مدیران آمار (اگر تنظیم شده باشند)."""
    return not STATS_ADMIN_IDS or (user is not None and user.id in STATS_ADMIN_IDS)


async def _send_stats(msg, period) -> None:
    if not _chat_allowed(msg.chat_id) or not _stats_allowed(msg.from_user):
        return  # بی‌صدا نادیده گرفته می‌شود تا گروه شلوغ نشود
    if period is None:
        await msg.reply_text("آمار کدام بازه؟", reply_markup=stats_keyboard())
    else:
        await msg.reply_text(
            build_stats_text(msg.chat_id, period),
            parse_mode=ParseMode.HTML,
            reply_markup=stats_keyboard(),
        )


async def on_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    cmd = (msg.text or "").split()[0].lstrip("/").split("@")[0].lower()
    await _send_stats(msg, _CMD_TO_PERIOD.get(cmd))


async def on_stats_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    m = STATS_TEXT_RE.match(msg.text or "")
    word = m.group(1) if m else None
    await _send_stats(msg, _WORD_TO_PERIOD.get(word))


async def on_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _stats_allowed(query.from_user):
        await query.answer("فقط مدیر ربات می‌تواند آمار را ببیند.", show_alert=True)
        return
    period = (query.data or "").split("|")[-1]
    if period not in PERIOD_TITLES or not _chat_allowed(query.message.chat.id):
        await query.answer()
        return
    try:
        await query.edit_message_text(
            build_stats_text(query.message.chat.id, period),
            parse_mode=ParseMode.HTML,
            reply_markup=stats_keyboard(),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    await query.answer()


async def on_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """آیدی عددی کاربر را نشان می‌دهد (برای تنظیم STATS_ADMIN_IDS)."""
    msg = update.effective_message
    if msg.from_user is None or msg.sender_chat is not None:
        await msg.reply_text("برای دیدن آیدی، حالت ناشناس (anonymous) را خاموش کنید.")
        return
    await msg.reply_text(f"آیدی عددی شما: {msg.from_user.id}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("خطا در پردازش آپدیت", exc_info=context.error)


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

    init_db()
    if STATS_ADMIN_IDS:
        logger.info("آمار فقط برای %d کاربر مجاز است", len(STATS_ADMIN_IDS))
    else:
        logger.warning("STATS_ADMIN_IDS تنظیم نشده؛ آمار برای همه‌ی اعضا باز است.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("myid", on_myid))
    app.add_handler(CommandHandler(["stats", "daily", "weekly", "monthly"], on_stats_command))
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.UpdateType.MESSAGE
            & filters.Regex(STATS_TEXT_RE),
            on_stats_text,
        )
    )
    app.add_handler(CallbackQueryHandler(on_stats_button, pattern=r"^st\|"))
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & filters.UpdateType.MESSAGE,
            on_chart,
        )
    )
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(t|ok|sk)\|"))
    app.add_error_handler(on_error)

    logger.info("ربات در حال اجراست...")
    app.run_polling(allowed_updates=["message", "callback_query"], bootstrap_retries=-1)


if __name__ == "__main__":
    main()
