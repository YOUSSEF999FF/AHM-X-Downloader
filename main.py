import telebot
import requests
import os

TOKEN = '8996530159:AAEIwyVSeMk6E7zlOLBpIMX79TKIc_EngFs'  # استبدل ده بتوكن البوت بتاعك
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "أهلاً بيك يا أحمد! البوت شغال بالنظام الجديد السريع 🚀\nارسل رابط الفيديو وسيتم تحميلة فوراً.")

@bot.message_handler(func=lambda message: 'http' in message.text)
def download_video(message):
    url = message.text.strip()
    chat_id = message.chat.id
    msg = bot.reply_to(message, "جاري جلب الفيديو وتخطي حظر يوتيوب... ⏳")

    try:
        # طلب رابط التحميل من Cobalt API
        api_url = "https://api.cobalt.tools/"
        payload = {
            "url": url,
            "videoQuality": "720"  # جودة 720p ممتازة ومناسبة
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        response = requests.post(api_url, json=payload, headers=headers)
        data = response.json()

        if response.status_code == 200 and data.get("status") in ["tunnel", "redirect", "picker"]:
            download_link = data.get("url")
            
            bot.edit_message_text("📥 جاري تنزيل الفيديو ورفعه للتليجرام...", chat_id, msg.message_id)

            # تحميل الملف سحابياً
            video_req = requests.get(download_link, stream=True)
            filename = f"video_{chat_id}.mp4"

            with open(filename, 'wb') as f:
                for chunk in video_req.iter_content(chunk_size=8192):
                    f.write(chunk)

            # رفع الفيديو للمستخدم
            with open(filename, 'rb') as video_file:
                bot.send_video(chat_id, video_file)

            # تنظيف الملفات الموقتة
            os.remove(filename)
            bot.delete_message(chat_id, msg.message_id)

        else:
            error_msg = data.get("text", "مقدرتش اجيب رابط مباشر للفيديو ده.")
            bot.edit_message_text(f"❌ خطأ: {error_msg}", chat_id, msg.message_id)

    except Exception as e:
        print(f"Error: {e}")
        bot.edit_message_text("❌ حصل خطأ أثناء معالجة الرابط، جرب رابط تاني.", chat_id, msg.message_id)

if __name__ == '__main__':
    bot.polling(non_stop=True)
