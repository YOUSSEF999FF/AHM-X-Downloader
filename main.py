import os
import random
import time
from threading import Thread
from flask import Flask
import telebot

# إنشاء سيرفر وهمي لتجاوز فحص Port الخاص بـ Render المجاني
app = Flask('')

@app.route('/')
def home():
    return "Bot is running 24/7!"

def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()

# التوكن الخاص بك
TOKEN = "8845591454:AAEKrxoIJQFt-xbPHT8CzLN1Mix7ii0NLho"
bot = telebot.TeleBot(TOKEN)

ROASTS = [
    "يا ساتر! حاسس إن السيرفر هنج من كثرة الـ IQ العالي اللي في الرسالة دي 🧠⚡",
    "نصيحة أخوية: فكر مرتين قبل ما تكتب المرة الجاية 😅",
    "ما شاء الله، إجابة غير متوقعة ومحدش طلبها أساساً! 🎯",
]

@bot.message_handler(content_types=['new_chat_members'])
def welcome_new_member(message):
    for member in message.new_chat_members:
        welcome_text = f"نورت الجروب يا {member.first_name}! 🎉\nالقوانين بسيطة: ممنوع الإعلانات، وممنوع تزعل الأدمن، والشاي بـ 5 جنيه ☕😂"
        bot.reply_to(message, welcome_text)

@bot.message_handler(commands=['ban'])
def ban_user(message):
    if not message.reply_to_message:
        bot.reply_to(message, "اعمل رد (Reply) على رسالة الشخص اللي عايز تطرده! 🎯")
        return
    target = message.reply_to_message.from_user
    try:
        bot.ban_chat_member(message.chat.id, target.id)
        bot.reply_to(message, f"تم إرسال العضو {target.first_name} لكوكب آخر بدون عودة! 🚀👋")
    except Exception:
        bot.reply_to(message, "تأكد إن البوت أدمن ومعه صلاحية الحظر يا غالي! 😅")

@bot.message_handler(commands=['mute'])
def mute_user(message):
    if not message.reply_to_message:
        bot.reply_to(message, "اعمل رد (Reply) على رسالة الشخص اللي عايز تكتمه! 🤫")
        return
    target = message.reply_to_message.from_user
    try:
        bot.restrict_chat_member(message.chat.id, target.id, until_date=int(time.time()) + 600, can_send_messages=False)
        bot.reply_to(message, f"تم كتم {target.first_name} لمدة 10 دقائق.. روح اشرب شاي وروّق أعصابك ☕🤐")
    except Exception:
        bot.reply_to(message, "ما قدرتش أكتمه، تأكد من صلاحيات البوت! 🤷‍♂️")

@bot.message_handler(commands=['kick'])
def kick_user(message):
    if not message.reply_to_message:
        bot.reply_to(message, "اعمل رد (Reply) على الشخص اللي عايز تطلعه برة الجروب! 🚪")
        return
    target = message.reply_to_message.from_user
    try:
        bot.unban_chat_member(message.chat.id, target.id)
        bot.reply_to(message, f"تم إخراج {target.first_name} لتهوية الجروب، وتقدر ترجع لما تهدأ 🚪✨")
    except Exception:
        bot.reply_to(message, "فشل الطرد، ابحث عن أدمن أقوى مني! 🤖")

@bot.message_handler(commands=['roast'])
def roast_user(message):
    funny_msg = random.choice(ROASTS)
    if message.reply_to_message:
        bot.reply_to(message.reply_to_message, funny_msg)
    else:
        bot.reply_to(message, funny_msg)

@bot.message_handler(func=lambda msg: True)
def auto_reply(message):
    text = message.text.lower() if message.text else ""
    if "مين الأدمن" in text or "من الأدمن" in text:
        bot.reply_to(message, "الأدمن مشغول بيكتب كود جديد للجروب دلوقتي 🫡👨‍💻")
    elif "السلام عليكم" in text:
        bot.reply_to(message, "وعليكم السلام ورحمة الله وبركاته! نورت الساحة 🌟")
    elif text.strip() == "بوت":
        bot.reply_to(message, "نعمين؟ البوت في الخدمة وجاهز للشغاوة 🤖🔥")

# تشغيل السيرفر الوهمي ثم البوت
keep_alive()
bot.infinity_polling()
