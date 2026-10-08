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

وضعیت در پیام نگهداری نمی‌شود؛ داخل callback_data هر دکمه است (بدون دیتابیس، با ری‌استارت هم از بین نمی‌رود).
"""

import html
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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

# اگر True باشد پیام اصلی عضو پاک می‌شود (ربات باید ادمین با مجوز حذف پیام باشد).
DELETE_ORIGINAL = True

# اگر بخواهید ربات فقط در گروه‌های مشخص کار کند: آیدی‌ها را با کاما در این متغیر محیطی بگذارید
# مثال: ALLOWED_CHAT_IDS=-1001234567890,-1009876543210   (خالی = همه‌ی چت‌ها)
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
}

CAPTION_LIMIT = 1024  # سقف کپشن در تلگرام

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
        [InlineKeyboardButton(SUBMIT_TEXT, callback_data=f"ok|{owner_id}|{mask}")]
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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("خطا در پردازش آپدیت", exc_info=context.error)


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

    app = Application.builder().token(token).build()
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & filters.UpdateType.MESSAGE,
            on_chart,
        )
    )
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(t|ok)\|"))
    app.add_error_handler(on_error)

    logger.info("ربات در حال اجراست...")
    app.run_polling(allowed_updates=["message", "callback_query"], bootstrap_retries=-1)


if __name__ == "__main__":
    main()
