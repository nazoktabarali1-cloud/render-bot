import os
import logging
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, CallbackQueryHandler, ConversationHandler
)
from pypdf import PdfReader, PdfWriter

# تنظیم لاگ‌ها
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", "8443"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

# متغیرهای وضعیت برای مکالمه با کاربر
WAITING_FOR_PASSWORD, WAITING_FOR_PAGES = range(2)

# دیکشنری برای نگه داشتن فایل‌های کاربران و تنظیمات آن‌ها
# ساختار: user_files[user_id] = {"files": [bytes, bytes], "action": "action_name"}
user_files = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! من دستیار PDF شما هستم. 📄\n"
        "لطفاً یک یا چند فایل PDF برای من بفرست تا گزینه‌های پردازش را به تو نشان دهم.\n"
        "(برای ادغام، می‌توانید چند فایل را با هم فوروارد کنید)"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pdf_file = await update.message.document.get_file()
    file_bytes = await pdf_file.download_as_bytearray()
    
    # ذخیره فایل‌های ارسالی کاربر
    if user_id not in user_files:
        user_files[user_id] = {"files": [], "action": None}
    user_files[user_id]["files"].append(file_bytes)
    
    if len(user_files[user_id]["files"]) > 1:
        await update.message.reply_text(
            f"فایل {len(user_files[user_id]['files'])} دریافت شد. "
            "می‌توانید فایل بعدی را بفرستید یا یکی از عملیات زیر را انتخاب کنید."
        )
        return

    # اگر اولین فایل است، منو را نشان بده
    keyboard = [
        [InlineKeyboardButton("📝 استخراج متن", callback_data='extract_text')],
        [InlineKeyboardButton("🔄 چرخش ۹۰ درجه", callback_data='rotate_90')],
        [InlineKeyboardButton("🔒 رمزگذاری", callback_data='encrypt')],
        [InlineKeyboardButton("🗑️ حذف صفحات", callback_data='delete_pages')],
        [InlineKeyboardButton("🔗 ادغام فایل‌ها", callback_data='merge')],
        [InlineKeyboardButton("ℹ️ اطلاعات فایل", callback_data='info')]
    ]
    await update.message.reply_text("فایل دریافت شد! چه کاری می‌خواهی انجام دهم؟", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in user_files or not user_files[user_id]["files"]:
        await query.edit_message_text("❌ فایل شما منقضی شده است. لطفاً دوباره PDF را بفرستید.")
        return ConversationHandler.END

    # اگر کاربر قبلاً فایل‌ها را فرستاده و الان دکمه ادغام را زده
    if query.data == 'merge':
        if len(user_files[user_id]["files"]) < 2:
            await query.edit_message_text("⚠️ برای ادغام باید حداقل دو فایل فرستاده باشی. فایل بعدی را بفرست.")
            return
        await query.edit_message_text("در حال ادغام... ⏳")
        writer = PdfWriter()
        for f_bytes in user_files[user_id]["files"]:
            reader = PdfReader(io.BytesIO(f_bytes))
            for page in reader.pages:
                writer.add_page(page)
        await send_file(query, writer, "merged.pdf")
        return ConversationHandler.END

    # ذخیره اکشن فعلی و رفتن به مرحله بعد
    user_files[user_id]["action"] = query.data
    reader = PdfReader(io.BytesIO(user_files[user_id]["files"][0]))

    if query.data == 'info':
        info_text = f"📄 تعداد صفحات: {len(reader.pages)}\n📝 متادیتا: {reader.metadata}"
        await query.edit_message_text(info_text)
        return ConversationHandler.END

    elif query.data == 'extract_text':
        await query.edit_message_text("در حال استخراج متن... ⏳")
        text = "".join([f"--- صفحه {i+1} ---\n{p.extract_text() or ''}\n\n" for i, p in enumerate(reader.pages)])
        if not text.strip():
            await query.edit_message_text("❌ متنی پیدا نشد (ممکن است PDF تصویری باشد).")
            return ConversationHandler.END
        
        output = io.BytesIO(text.encode('utf-8'))
        output.name = "extracted_text.txt"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()
        return ConversationHandler.END

    elif query.data == 'rotate_90':
        await query.edit_message_text("در حال چرخش صفحات... ⏳")
        writer = PdfWriter()
        for page in reader.pages:
            page.rotate(90)
            writer.add_page(page)
        await send_file(query, writer, "rotated.pdf")
        return ConversationHandler.END

    elif query.data == 'encrypt':
        await query.edit_message_text("🔑 لطفاً رمز عبور دلخواه خود را ارسال کنید:")
        return WAITING_FOR_PASSWORD

    elif query.data == 'delete_pages':
        await query.edit_message_text("🗑️ لطفاً شماره صفحه‌هایی که می‌خواهی حذف شوند را با کاما بفرست (مثلاً: 1, 3, 5):")
        return WAITING_FOR_PAGES

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    password = update.message.text
    file_bytes = user_files[user_id]["files"][0]
    
    await update.message.reply_text("در حال رمزگذاری... ⏳")
    reader = PdfReader(io.BytesIO(file_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    
    writer.encrypt(user_password=password, owner_password="admin_owner_pass")
    await send_file(update, writer, "encrypted.pdf", is_message=True)
    del user_files[user_id] # پاک کردن حافظه
    return ConversationHandler.END

async def receive_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pages_str = update.message.text.replace(" ", "")
    try:
        pages_to_delete = [int(p) for p in pages_str.split(",")]
    except ValueError:
        await update.message.reply_text("❌ فرمت اشتباه است. لطفاً مثل 1, 3, 5 بفرست.")
        return WAITING_FOR_PAGES

    file_bytes = user_files[user_id]["files"][0]
    await update.message.reply_text("در حال حذف صفحات... ⏳")
    
    reader = PdfReader(io.BytesIO(file_bytes))
    writer = PdfWriter()
    
    for i, page in enumerate(reader.pages):
        # شماره صفحات در پایتون از 0 شروع می‌شود
        if (i + 1) not in pages_to_delete:
            writer.add_page(page)
            
    await send_file(update, writer, "deleted_pages.pdf", is_message=True)
    del user_files[user_id]
    return ConversationHandler.END

async def send_file(update, writer, filename, is_message=False):
    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    output.name = filename
    
    chat_id = update.from_user.id if is_message else update.from_user.id
    if is_message:
        await update.message.reply_document(chat_id=chat_id, document=output)
    else:
        await update.message.reply_document(chat_id=chat_id, document=output)
        await update.message.delete()

def main():
    application = ApplicationBuilder().token(TOKEN).build()

    # مدیریت مکالمه (State Machine)
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.PDF, handle_pdf),
            CallbackQueryHandler(button_handler)
        ],
        states={
            WAITING_FOR_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            WAITING_FOR_PAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pages)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)

    webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=webhook_url
    )

if __name__ == '__main__':
    main()
