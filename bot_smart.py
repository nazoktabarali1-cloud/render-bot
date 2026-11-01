import os
import logging
import io
import time
import fitz  # این همان PyMuPDF است
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, CallbackQueryHandler, ConversationHandler
)
from pypdf import PdfReader, PdfWriter

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", "8443"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

# حالت‌های مکالمه
WAITING_FOR_PASSWORD, WAITING_FOR_PAGES, WAITING_FOR_WATERMARK, WAITING_FOR_SPLIT = range(4)

# دیکشنری برای نگه داری فایل‌ها
# ساختار: user_files[user_id] = {"files": [bytes], "action": str, "timestamp": float}
user_files = {}
MAX_FILE_SIZE_MB = 10
TIME_LIMIT = 300 # ۵ دقیقه

def check_size(size):
    return size < MAX_FILE_SIZE_MB * 1024 * 1024

def cleanup_old_files():
    now = time.time()
    for uid in list(user_files.keys()):
        if now - user_files[uid].get("timestamp", 0) > TIME_LIMIT:
            del user_files[uid]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_old_files()
    await update.message.reply_text(
        "سلام! به دستیار جامع PDF خوش آمدید. 📄\n"
        "⚠️ حداکثر حجم مجاز فایل: ۱۰ مگابایت.\n"
        "لطفاً یک یا چند فایل PDF برای من بفرستید:"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_old_files()
    user_id = update.effective_user.id
    doc = update.message.document
    
    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ حجم فایل بیشتر از {MAX_FILE_SIZE_MB} مگابایت است. برای جلوگیری از کرش سرور، فایل کوچکتری بفرستید.")
        return

    pdf_file = await doc.get_file()
    file_bytes = await pdf_file.download_as_bytearray()
    
    if user_id not in user_files:
        user_files[user_id] = {"files": [], "action": None}
        
    user_files[user_id]["files"].append(file_bytes)
    user_files[user_id]["timestamp"] = time.time()

    if len(user_files[user_id]["files"]) > 1:
        await update.message.reply_text(f"فایل {len(user_files[user_id]['files'])} دریافت شد. فایل بعدی را بفرستید یا یک عملیات را انتخاب کنید.")
        return

    keyboard = [
        [InlineKeyboardButton("📝 استخراج متن", callback_data='extract_text'), InlineKeyboardButton("📄 خروجی Word", callback_data='to_word')],
        [InlineKeyboardButton("🖼️ تبدیل به عکس", callback_data='to_image'), InlineKeyboardButton("🔄 چرخش ۹۰ درجه", callback_data='rotate_90')],
        [InlineKeyboardButton("🔒 رمزگذاری", callback_data='encrypt'), InlineKeyboardButton("🗑️ حذف صفحات", callback_data='delete_pages')],
        [InlineKeyboardButton("✂️ استخراج صفحات", callback_data='split_pages'), InlineKeyboardButton("🔗 ادغام فایل‌ها", callback_data='merge')],
        [InlineKeyboardButton("💧 واترمارک متنی", callback_data='watermark')]
    ]
    await update.message.reply_text("فایل دریافت شد! چه کاری می‌خواهی انجام دهم؟", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in user_files or not user_files[user_id]["files"]:
        await query.edit_message_text("❌ فایل شما منقضی شده است. لطفاً دوباره PDF را بفرستید.")
        return ConversationHandler.END

    user_files[user_id]["action"] = query.data
    user_files[user_id]["timestamp"] = time.time()
    reader = PdfReader(io.BytesIO(user_files[user_id]["files"][0]))

    if query.data == 'merge':
        if len(user_files[user_id]["files"]) < 2:
            await query.edit_message_text("⚠️ برای ادغام باید حداقل دو فایل فرستاده باشی. فایل بعدی را بفرست.")
            return
        await query.edit_message_text("در حال ادغام... ⏳")
        writer = PdfWriter()
        for f_bytes in user_files[user_id]["files"]:
            r = PdfReader(io.BytesIO(f_bytes))
            for page in r.pages:
                writer.add_page(page)
        await send_pdf(query, writer, "merged.pdf")
        return ConversationHandler.END

    elif query.data == 'extract_text':
        await query.edit_message_text("در حال استخراج متن... ⏳")
        text = "".join([f"--- صفحه {i+1} ---\n{p.extract_text() or ''}\n\n" for i, p in enumerate(reader.pages)])
        if not text.strip():
            await query.edit_message_text("❌ متنی پیدا نشد (PDF تصویری است).")
            return ConversationHandler.END
        output = io.BytesIO(text.encode('utf-8'))
        output.name = "extracted_text.txt"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()

    elif query.data == 'to_word':
        await query.edit_message_text("در حال تبدیل به Word... ⏳ (ممکن است کمی طول بکشد)")
        doc = Document()
        for page in reader.pages:
            doc.add_paragraph(page.extract_text() or "")
            doc.add_page_break()
        output = io.BytesIO()
        doc.save(output)
        output.seek(0)
        output.name = "converted.docx"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()

    elif query.data == 'to_image':
        await query.edit_message_text("در حال تبدیل به تصویر... ⏳ (فقط ۳ صفحه اول برای جلوگیری از فشار به سرور)")
        try:
            pdf_doc = fitz.open(stream=user_files[user_id]["files"][0], filetype="pdf")
            for i in range(min(3, len(pdf_doc))): # فقط ۳ صفحه اول
                page = pdf_doc[i]
                pix = page.get_pixmap(dpi=100) # کاهش کیفیت برای حجم کمتر
                img_bytes = pix.tobytes("png")
                await context.bot.send_photo(chat_id=user_id, photo=img_bytes, caption=f"صفحه {i+1}")
            await query.message.delete()
        except Exception as e:
            await query.edit_message_text(f"❌ خطا در تبدیل عکس: {str(e)}")

    elif query.data == 'rotate_90':
        await query.edit_message_text("در حال چرخش... ⏳")
        writer = PdfWriter()
        for page in reader.pages:
            page.rotate(90)
            writer.add_page(page)
        await send_pdf(query, writer, "rotated.pdf")

    elif query.data == 'encrypt':
        await query.edit_message_text("🔑 رمز عبور دلخواه را بفرستید:")
        return WAITING_FOR_PASSWORD

    elif query.data == 'delete_pages':
        await query.edit_message_text("🗑️ شماره صفحاتی که می‌خواهی حذف شوند را با کاما بفرست (مثلاً: 1, 3):")
        return WAITING_FOR_PAGES

    elif query.data == 'split_pages':
        await query.edit_message_text("✂️ بازه صفحاتی که می‌خواهی جدا شوند را بفرست (مثلاً: 2-5):")
        return WAITING_FOR_SPLIT

    elif query.data == 'watermark':
        await query.edit_message_text("💧 متن واترمارک را بفرست (مثلاً: محرمانه):")
        return WAITING_FOR_WATERMARK

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    file_bytes = user_files[user_id]["files"][0]
    await update.message.reply_text("در حال رمزگذاری... ⏳")
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(file_bytes)).pages:
        writer.add_page(page)
    writer.encrypt(user_password=update.message.text, owner_password="admin_owner_pass")
    await send_pdf(update, writer, "encrypted.pdf", is_msg=True)
    return ConversationHandler.END

async def receive_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        pages = [int(p.strip()) for p in update.message.text.split(",")]
    except:
        await update.message.reply_text("❌ فرمت اشتباه است. مثل 1, 3, 5 بفرست.")
        return WAITING_FOR_PAGES

    file_bytes = user_files[user_id]["files"][0]
    await update.message.reply_text("در حال حذف... ⏳")
    writer = PdfWriter()
    for i, page in enumerate(PdfReader(io.BytesIO(file_bytes)).pages):
        if (i + 1) not in pages:
            writer.add_page(page)
    await send_pdf(update, writer, "deleted.pdf", is_msg=True)
    return ConversationHandler.END

async def receive_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        start, end = map(int, update.message.text.split("-"))
    except:
        await update.message.reply_text("❌ فرمت اشتباه است. مثل 2-5 بفرست.")
        return WAITING_FOR_SPLIT

    file_bytes = user_files[user_id]["files"][0]
    await update.message.reply_text("در حال استخراج صفحات... ⏳")
    writer = PdfWriter()
    reader = PdfReader(io.BytesIO(file_bytes))
    for i in range(start-1, min(end, len(reader.pages))):
        writer.add_page(reader.pages[i])
    await send_pdf(update, writer, "splitted.pdf", is_msg=True)
    return ConversationHandler.END

async def receive_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    watermark_text = update.message.text
    file_bytes = user_files[user_id]["files"][0]
    
    await update.message.reply_text("در حال اعمال واترمارک... ⏳ (فقط روی ۵ صفحه اول برای بهینه‌سازی)")
    try:
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        for i in range(min(5, len(pdf_doc))):
            page = pdf_doc[i]
            # قرار دادن متن در گوشه صفحه
            page.insert_text((50, 50), watermark_text, fontsize=30, color=(1, 0, 0), rotate=45)
            
        output = io.BytesIO()
        pdf_doc.save(output)
        output.seek(0)
        output.name = "watermarked.pdf"
        await update.message.reply_document(chat_id=user_id, document=output)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {str(e)}")
    return ConversationHandler.END

async def send_pdf(update, writer, filename, is_msg=False):
    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    output.name = filename
    if is_msg:
        await update.message.reply_document(chat_id=update.from_user.id, document=output)
    else:
        await update.message.reply_document(chat_id=update.from_user.id, document=output)
        await update.message.delete()

def main():
    application = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.PDF, handle_pdf),
            CallbackQueryHandler(button_handler)
        ],
        states={
            WAITING_FOR_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            WAITING_FOR_PAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pages)],
            WAITING_FOR_SPLIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_split)],
            WAITING_FOR_WATERMARK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watermark)],
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
