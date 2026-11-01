import os
import logging
import io
import time
import fitz  # PyMuPDF
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, CallbackQueryHandler
)
from pypdf import PdfReader, PdfWriter

# تنظیم لاگ‌ها
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", "8443"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

MAX_FILE_SIZE_MB = 10

# تابع کمکی برای ارسال فایل
async def send_pdf_file(chat_id, context, writer, filename):
    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    output.name = filename
    await context.bot.send_document(chat_id=chat_id, document=output)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # پاک کردن حافظه کاربر با استارت جدید
    context.user_data['files'] = []
    context.user_data['state'] = None
    
    await update.message.reply_text(
        "سلام! به دستیار جامع PDF خوش آمدید. 📄\n"
        f"⚠️ حداکثر حجم مجاز فایل: {MAX_FILE_SIZE_MB} مگابایت.\n"
        "لطفاً یک یا چند فایل PDF برای من بفرستید:"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    
    # بررسی حجم
    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ حجم فایل بیشتر از {MAX_FILE_SIZE_MB} مگابایت است.")
        return

    # آماده‌سازی حافظه
    if 'files' not in context.user_data:
        context.user_data['files'] = []
        
    # دانلود و ذخیره
    pdf_file = await doc.get_file()
    file_bytes = await pdf_file.download_as_bytearray()
    context.user_data['files'].append(file_bytes)
    context.user_data['state'] = None # ریست کردن وضعیت

    # ساخت منو
    keyboard = [
        [InlineKeyboardButton("📝 استخراج متن", callback_data='extract_text'), InlineKeyboardButton("📄 خروجی Word", callback_data='to_word')],
        [InlineKeyboardButton("🖼️ تبدیل به عکس", callback_data='to_image'), InlineKeyboardButton("🔄 چرخش ۹۰ درجه", callback_data='rotate_90')],
        [InlineKeyboardButton("🔒 رمزگذاری", callback_data='encrypt'), InlineKeyboardButton("🗑️ حذف صفحات", callback_data='delete_pages')],
        [InlineKeyboardButton("✂️ استخراج صفحات", callback_data='split_pages'), InlineKeyboardButton("🔗 ادغام فایل‌ها", callback_data='merge')],
        [InlineKeyboardButton("💧 واترمارک متنی", callback_data='watermark')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    count = len(context.user_data['files'])
    msg = "فایل دریافت شد! چه کاری می‌خواهی انجام دهم؟" if count == 1 else f"فایل {count} دریافت شد. فایل بعدی را بفرست یا یک عملیات انتخاب کن."
    
    await update.message.reply_text(msg, reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if 'files' not in context.user_data or not context.user_data['files']:
        await query.edit_message_text("❌ فایل شما منقضی شده است. لطفاً دوباره PDF را بفرستید.")
        return

    action = query.data
    reader = PdfReader(io.BytesIO(context.user_data['files'][0]))

    if action == 'merge':
        if len(context.user_data['files']) < 2:
            await query.edit_message_text("⚠️ برای ادغام باید حداقل دو فایل فرستاده باشی. فایل بعدی را بفرست.")
            return
        await query.edit_message_text("در حال ادغام... ⏳")
        writer = PdfWriter()
        for f_bytes in context.user_data['files']:
            r = PdfReader(io.BytesIO(f_bytes))
            for page in r.pages:
                writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "merged.pdf")
        await query.message.delete()

    elif action == 'extract_text':
        await query.edit_message_text("در حال استخراج متن... ⏳")
        text = "".join([f"--- صفحه {i+1} ---\n{p.extract_text() or ''}\n\n" for i, p in enumerate(reader.pages)])
        if not text.strip():
            await query.edit_message_text("❌ متنی پیدا نشد (PDF تصویری است).")
            return
        output = io.BytesIO(text.encode('utf-8'))
        output.name = "extracted_text.txt"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()

    elif action == 'to_word':
        await query.edit_message_text("در حال تبدیل به Word... ⏳")
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

    elif action == 'to_image':
        await query.edit_message_text("در حال تبدیل به تصویر... ⏳ (فقط ۳ صفحه اول)")
        try:
            pdf_doc = fitz.open(stream=context.user_data['files'][0], filetype="pdf")
            for i in range(min(3, len(pdf_doc))):
                page = pdf_doc[i]
                pix = page.get_pixmap(dpi=100)
                img_bytes = pix.tobytes("png")
                await context.bot.send_photo(chat_id=user_id, photo=img_bytes, caption=f"صفحه {i+1}")
            await query.message.delete()
        except Exception as e:
            await query.edit_message_text(f"❌ خطا: {str(e)}")

    elif action == 'rotate_90':
        await query.edit_message_text("در حال چرخش... ⏳")
        writer = PdfWriter()
        for page in reader.pages:
            page.rotate(90)
            writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "rotated.pdf")
        await query.message.delete()

    elif action == 'encrypt':
        await query.edit_message_text("🔑 رمز عبور دلخواه را بفرستید:")
        context.user_data['state'] = 'waiting_password'

    elif action == 'delete_pages':
        await query.edit_message_text("🗑️ شماره صفحات برای حذف را بفرست (مثلاً: 1, 3):")
        context.user_data['state'] = 'waiting_delete'

    elif action == 'split_pages':
        await query.edit_message_text("✂️ بازه صفحات را بفرست (مثلاً: 2-5):")
        context.user_data['state'] = 'waiting_split'

    elif action == 'watermark':
        await query.edit_message_text("💧 متن واترمارک را بفرست:")
        context.user_data['state'] = 'waiting_watermark'

# این تابع پیام‌های متنی کاربر (مثل پسورد یا شماره صفحات) را می‌گیرد
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    if not state:
        return # اگر در حالت انتظار نیستیم، پیام را نادیده بگیر

    text = update.message.text
    user_id = update.effective_user.id
    file_bytes = context.user_data['files'][0]

    if state == 'waiting_password':
        await update.message.reply_text("در حال رمزگذاری... ⏳")
        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(file_bytes)).pages:
            writer.add_page(page)
        writer.encrypt(user_password=text, owner_password="admin_pass")
        await send_pdf_file(user_id, context, writer, "encrypted.pdf")
        context.user_data['state'] = None

    elif state == 'waiting_delete':
        try:
            pages = [int(p.strip()) for p in text.split(",")]
        except:
            await update.message.reply_text("❌ فرمت اشتباه است. مثل 1, 3 بفرست.")
            return
        await update.message.reply_text("در حال حذف... ⏳")
        writer = PdfWriter()
        for i, page in enumerate(PdfReader(io.BytesIO(file_bytes)).pages):
            if (i + 1) not in pages:
                writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "deleted.pdf")
        context.user_data['state'] = None

    elif state == 'waiting_split':
        try:
            start, end = map(int, text.split("-"))
        except:
            await update.message.reply_text("❌ فرمت اشتباه است. مثل 2-5 بفرست.")
            return
        await update.message.reply_text("در حال استخراج... ⏳")
        writer = PdfWriter()
        reader = PdfReader(io.BytesIO(file_bytes))
        for i in range(start-1, min(end, len(reader.pages))):
            writer.add_page(reader.pages[i])
        await send_pdf_file(user_id, context, writer, "splitted.pdf")
        context.user_data['state'] = None

    elif state == 'waiting_watermark':
        await update.message.reply_text("در حال اعمال واترمارک... ⏳ (فقط ۵ صفحه اول)")
        try:
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            for i in range(min(5, len(pdf_doc))):
                page = pdf_doc[i]
                page.insert_text((50, 50), text, fontsize=30, color=(1, 0, 0), rotate=45)
            output = io.BytesIO()
            pdf_doc.save(output)
            output.seek(0)
            output.name = "watermarked.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {str(e)}")
        context.user_data['state'] = None

def main():
    application = ApplicationBuilder().token(TOKEN).build()

    # اضافه کردن هندلرها به ترتیب
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=webhook_url
    )

if __name__ == '__main__':
    main()
