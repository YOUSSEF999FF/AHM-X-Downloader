import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp
import os
import time

TOKEN = '8996530159:AAEIwyVSeMk6E7zlOLBpIMX79TKIc_EngFs'  # حط التوكن بتاعك هنا
bot = telebot.TeleBot(TOKEN)
user_urls = {}

# إعدادات محدثة لتخطي حظر يوتيوب على السيرفرات السحابية
YTDL_BASE_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'extractor_args': {
        'youtube': {
            'player_client': ['web_safari', 'web']
        }
    },
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
        'Accept-Language': 'en-US,en;q=0.9',
    }
}

def format_size(bytes_size):
    if not bytes_size or bytes_size == 0:
        return "0 MB"
    mb = bytes_size / (1024 * 1024)
    if mb >= 1024:
        return f"{mb/1024:.2f} GB"
    return f"{mb:.1f} MB"

def get_progress_bar(percent):
    filled = int(percent / 10)
    bar = '█' * filled + '░' * (10 - filled)
    return f"[{bar}] {percent}%"

def safe_edit(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        print(f"Safe edit error: {e}")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    try:
        bot.reply_to(message, "أهلاً بيك في بوت التحميل! ابعتلي رابط أي فيديو يوتيوب وهجيبلك كل الجودات ومساحتها 🚀")
    except Exception as e:
        print(f"Start command error: {e}")

@bot.message_handler(func=lambda message: 'http' in message.text)
def fetch_video_info(message):
    url = message.text.strip()
    chat_id = message.chat.id
    
    try:
        msg = bot.reply_to(message, "جاري فحص الفيديو واستخراج الجودات والمساحات... ⏳")
    except Exception:
        return

    ydl_opts = dict(YTDL_BASE_OPTS)
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        formats = info.get('formats', [])
        audio_size = 0
        for f in formats:
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                size = f.get('filesize') or f.get('filesize_approx') or 0
                if size > audio_size:
                    audio_size = size
        
        resolutions = {}
        for f in formats:
            height = f.get('height')
            if height and f.get('vcodec') != 'none':
                size = f.get('filesize') or f.get('filesize_approx') or 0
                total_size = (size + audio_size) if f.get('acodec') == 'none' else size
                if height not in resolutions or total_size > resolutions[height]:
                    resolutions[height] = total_size
        
        if not resolutions:
            safe_edit(chat_id, msg.message_id, "مقدرتش ألاقي جودات مناسبة للفيديو ده.")
            return
            
        user_urls[chat_id] = url
        markup = InlineKeyboardMarkup()
        for res in sorted(resolutions.keys()):
            size_text = format_size(resolutions[res])
            markup.add(InlineKeyboardButton(text=f"{res}p | {size_text}", callback_data=f"res_{res}"))
            
        safe_edit(chat_id, msg.message_id, "اختار الجودة اللي عايز تحمل بيها 👇:", reply_markup=markup)
        
    except Exception as e:
        print(f"Error in fetching: {e}")
        safe_edit(chat_id, msg.message_id, f"حصلت مشكلة في قراءة الرابط:\n`{str(e)[:150]}`", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('res_'))
def download_selected_quality(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    res = call.data.split('_')[1]
    url = user_urls.get(chat_id)
    
    if not url:
        try:
            bot.answer_callback_query(call.id, "الرابط انتهت صلاحيته، ابعته تاني.")
        except Exception:
            pass
        return
        
    try:
        bot.answer_callback_query(call.id, f"بدء تحميل جودة {res}p...")
    except Exception:
        pass

    last_update_time = [0]
    
    def progress_hook(d):
        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_update_time[0] > 3:
                try:
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    downloaded = d.get('downloaded_bytes', 0)
                    if total > 0:
                        percent = int((downloaded / total) * 100)
                        bar = get_progress_bar(percent)
                        speed = format_size(d.get('speed', 0)) + "/s"
                        text = (
                            f"📥 جاري التحميل بجودة **{res}p**\n\n"
                            f"{bar}\n\n"
                            f"🚀 السرعة: `{speed}`\n"
                            f"📊 المتبقي: `{format_size(total - downloaded)}`"
                        )
                        safe_edit(chat_id, message_id, text, parse_mode="Markdown")
                        last_update_time[0] = current_time
                except Exception:
                    pass
        elif d['status'] == 'finished':
            safe_edit(chat_id, message_id, "🔄 اكتمل التحميل!\n\n**جاري دمج الملفات... برجاء الانتظار ⏳**", parse_mode="Markdown")

    ydl_opts = dict(YTDL_BASE_OPTS)
    ydl_opts.update({
        'format': f'bestvideo[height<={res}]+bestaudio/best[height<={res}]',
        'outtmpl': f'video_{chat_id}.%(ext)s',
        'merge_output_format': 'mp4',
        'progress_hooks': [progress_hook]
    })
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            
        filename = f'video_{chat_id}.mp4'
        safe_edit(chat_id, message_id, "📤 جاري رفع الفيديو إلى تليجرام...")
        
        if os.path.exists(filename):
            with open(filename, 'rb') as video:
                bot.send_video(chat_id, video, timeout=300)
            os.remove(filename)
            
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
            
    except Exception as e:
        print(f"Error in download: {e}")
        safe_edit(chat_id, message_id, f"❌ خطأ أثناء التحميل:\n`{str(e)[:150]}`", parse_mode="Markdown")

if __name__ == '__main__':
    while True:
        try:
            print("Bot is starting/restarting...")
            bot.polling(non_stop=True, interval=1, timeout=60)
        except Exception as e:
            print(f"Polling error encountered: {e}")
            time.sleep(5)
