import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# ضع Token البوت هنا
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")


# ============================================================
# /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [
            InlineKeyboardButton("🎬 YouTube", callback_data="youtube")
        ],
        [
            InlineKeyboardButton("ℹ️ طريقة الاستخدام", callback_data="help")
        ]
    ]

    await update.message.reply_text(
        "🔥 أهلاً بيك في AHM X Downloader\n\n"
        "🎬 أرسل رابط فيديو YouTube هنا.\n"
        "وسأجهز لك خيارات التحميل.\n\n"
        "⚡ سريع • بسيط • مجاني",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ============================================================
# استقبال الرابط
# ============================================================

async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = update.message.text.strip()

    if "youtube.com" not in url and "youtu.be" not in url:
        await update.message.reply_text(
            "❌ الرابط غير مدعوم.\n\n"
            "📌 أرسل رابط YouTube صحيح."
        )
        return

    keyboard = [
        [
            InlineKeyboardButton("🎬 1080p", callback_data="1080"),
            InlineKeyboardButton("🎬 720p", callback_data="720"),
        ],
        [
            InlineKeyboardButton("🎬 480p", callback_data="480"),
            InlineKeyboardButton("🎬 360p", callback_data="360"),
        ],
        [
            InlineKeyboardButton("🎵 MP3", callback_data="mp3"),
        ],
        [
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel")
        ]
    ]

    # نحفظ الرابط مؤقتًا للمستخدم
    context.user_data["youtube_url"] = url

    await update.message.reply_text(
        "🔎 تم اكتشاف رابط YouTube!\n\n"
        "اختر الجودة المطلوبة 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ============================================================
# التعامل مع الأزرار
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "youtube":

        await query.message.reply_text(
            "🎬 أرسل رابط فيديو YouTube هنا."
        )
        return

    if data == "help":

        await query.message.reply_text(
            "📖 طريقة الاستخدام:\n\n"
            "1️⃣ أرسل رابط YouTube\n"
            "2️⃣ اختر الجودة\n"
            "3️⃣ انتظر تجهيز الملف\n"
            "4️⃣ سيصلك الملف هنا 📥"
        )
        return

    if data == "cancel":

        context.user_data.pop("youtube_url", None)

        await query.edit_message_text(
            "❌ تم إلغاء العملية."
        )
        return

    if data in ["1080", "720", "480", "360", "mp3"]:

        url = context.user_data.get("youtube_url")

        if not url:
            await query.edit_message_text(
                "❌ انتهت صلاحية الرابط.\n"
                "أرسل الرابط مرة أخرى."
            )
            return

        quality = data

        await query.edit_message_text(
            f"⏳ تم اختيار {quality}\n\n"
            "⚙️ جاري تجهيز التحميل...\n"
            "انتظر قليلًا..."
        )

        # ====================================================
        # هنا سنضيف محرك التحميل في الخطوة القادمة
        # ====================================================

        await query.message.reply_text(
            "🚧 محرك التحميل لم يتم تركيبه بعد.\n\n"
            "الواجهة أصبحت جاهزة، والخطوة القادمة هي إضافة "
            "yt-dlp + FFmpeg."
        )


# ============================================================
# تشغيل البوت
# ============================================================

def main():

    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        print("❌ ضع Bot Token أولاً.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_link
        )
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    print("🔥 AHM X Downloader is running...")

    application.run_polling()


if __name__ == "__main__":
    main()
