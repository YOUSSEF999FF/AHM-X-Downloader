import os
import asyncio
import tempfile
from pathlib import Path

import yt_dlp

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
# BOT TOKEN
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [
            InlineKeyboardButton(
                "🎬 YouTube",
                callback_data="youtube"
            )
        ],
        [
            InlineKeyboardButton(
                "ℹ️ طريقة الاستخدام",
                callback_data="help"
            )
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
# RECEIVE URL
# ============================================================

async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = update.message.text.strip()

    if "youtube.com" not in url and "youtu.be" not in url:
        await update.message.reply_text(
            "❌ الرابط غير مدعوم.\n\n"
            "📌 أرسل رابط YouTube صحيح."
        )
        return

    context.user_data["youtube_url"] = url

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

    await update.message.reply_text(
        "🔎 تم اكتشاف رابط YouTube!\n\n"
        "🎚️ اختر الجودة المطلوبة 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ============================================================
# DOWNLOAD FUNCTION
# ============================================================

def download_video(url, quality, folder):

    output = str(Path(folder) / "%(title)s.%(ext)s")

    if quality == "mp3":

        options = {
            "format": "bestaudio/best",
            "outtmpl": output,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ],
        }

    else:

        height = int(quality)

        options = {
            "format": (
                f"bestvideo[height<={height}]"
                f"+bestaudio/best[height<={height}]"
            ),
            "outtmpl": output,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = list(Path(folder).glob("*"))

    if not files:
        raise RuntimeError("Download completed but no file was found.")

    # Ignore temporary files
    files = [
        f for f in files
        if not f.name.endswith((".part", ".ytdl"))
    ]

    if not files:
        raise RuntimeError("No completed file was found.")

    return files[0]


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    data = query.data

    # --------------------------------------------------------
    # YouTube
    # --------------------------------------------------------

    if data == "youtube":

        await query.message.reply_text(
            "🎬 أرسل رابط فيديو YouTube هنا."
        )

        return

    # --------------------------------------------------------
    # Help
    # --------------------------------------------------------

    if data == "help":

        await query.message.reply_text(
            "📖 طريقة الاستخدام:\n\n"
            "1️⃣ أرسل رابط YouTube\n"
            "2️⃣ اختر الجودة\n"
            "3️⃣ انتظر تجهيز الملف\n"
            "4️⃣ سيصلك الملف هنا 📥"
        )

        return

    # --------------------------------------------------------
    # Cancel
    # --------------------------------------------------------

    if data == "cancel":

        context.user_data.pop("youtube_url", None)

        await query.edit_message_text(
            "❌ تم إلغاء العملية."
        )

        return

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    if data in ["1080", "720", "480", "360", "mp3"]:

        url = context.user_data.get("youtube_url")

        if not url:

            await query.edit_message_text(
                "❌ انتهت صلاحية الرابط.\n"
                "أرسل الرابط مرة أخرى."
            )

            return

        quality = data

        if quality == "mp3":
            quality_name = "🎵 MP3"
        else:
            quality_name = f"🎬 {quality}p"

        await query.edit_message_text(
            f"⏳ تم اختيار {quality_name}\n\n"
            "🔎 جاري تجهيز التحميل...\n"
            "انتظر قليلًا ⏱️"
        )

        temp_dir = tempfile.mkdtemp(prefix="ahmx_")

        try:

            file_path = await asyncio.to_thread(
                download_video,
                url,
                quality,
                temp_dir
            )

            file_size = file_path.stat().st_size

            # Telegram Bot API upload limit
            if file_size > 50 * 1024 * 1024:

                await query.message.reply_text(
                    "⚠️ الملف كبير جدًا لإرساله مباشرة عبر البوت.\n\n"
                    f"📦 الحجم: {file_size / (1024 * 1024):.1f} MB\n\n"
                    "جرّب جودة أقل."
                )

                return

            await query.message.reply_text(
                "✅ تم تجهيز الملف!\n\n"
                "📤 جاري إرساله..."
            )

            if quality == "mp3":

                with open(file_path, "rb") as audio:

                    await query.message.reply_audio(
                        audio=audio,
                        title=file_path.stem[:64]
                    )

            else:

                with open(file_path, "rb") as video:

                    await query.message.reply_video(
                        video=video,
                        supports_streaming=True,
                        caption="🔥 AHM X Downloader"
                    )

            await query.message.reply_text(
                "✅ تم التحميل بنجاح 🔥"
            )

        except Exception as e:

            print("DOWNLOAD ERROR:", repr(e))

            await query.message.reply_text(
                "❌ حصل خطأ أثناء التحميل.\n\n"
                "جرّب رابطًا آخر أو جودة أقل."
            )

        finally:

            # Cleanup temporary files
            try:

                for file in Path(temp_dir).glob("*"):
                    file.unlink(missing_ok=True)

                Path(temp_dir).rmdir()

            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================

def main():

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

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


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()        return

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
