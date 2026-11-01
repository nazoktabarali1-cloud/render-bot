import os
import logging
import io
import fitz  # PyMuPDF
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, CallbackQueryHandler
)
from pypdf import PdfReader, PdfWriter

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
    context.user_data['files'] = []
    context.user_data['state'] = None
    await update.message.reply_text(
        "سلام! به دستیار جامع PDF خوش آمدید. 📄\n"
        f"⚠️ حداکثر حجم مجاز فایل: {MAX_FILE_SIZE_MB} مگابایت.\n"
        "لطفاً یک یا چند فایل PDF برای من بفرستید:"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ حجم فایل بیشتر از {MAX_FILE_SIZE_MB} مگابایت است.")
        return

    if 'files' not in context.user_data:
        context.user_data['files'] = []
        
    pdf_file = await doc.get_file()
    file_bytes = await pdf_file.download_as_bytearray()
    context.user_data['files'].append(file_bytes)
    context.user_data['state'] = None

    keyboard = [
        [InlineKeyboardButton("📝 استخراج متن", callback_data='extract_text'), InlineKeyboardButton("📄 خروجی Word", callback_data='to_word')],
        [InlineKeyboardButton("🖼️ تبدیل به عکس", callback_data='to_image'), InlineKeyboardButton("🔇 فشرده‌سازی", callback_data='compress')],
        [InlineKeyboardButton("🔒 رمزگذاری", callback_data='encrypt'), InlineKeyboardButton("🔓 باز کردن قفل", callback_data='decrypt')],
        [InlineKeyboardButton("🗑️ حذف صفحات", callback_data='delete_pages'), InlineKeyboardButton("✂️ استخراج صفحات", callback_data='split_pages')],
        [InlineKeyboardButton("🔗 ادغام فایل‌ها", callback_data='merge'), InlineKeyboardButton("🔄 چرخش ۹۰ درجه", callback_data='rotate_90')],
        [InlineKeyboardButton("🔢 شماره‌گذاری صفحات", callback_data='add_numbers'), InlineKeyboardButton("🔄 معکوس کردن", callback_data='reverse')],
        [InlineKeyboardButton("💧 واترمارک", callback_data='watermark'), InlineKeyboardButton("📝 ویرایش متادیتا", callback_data='metadata')],
        [InlineKeyboardButton("🖼️ افزودن کاور", callback_data='add_cover'), InlineKeyboardButton("🏷️ افزودن لوگو", callback_data='add_logo')]
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
    file_bytes = context.user_data['files'][0]

    # --- عملیات‌های فوری (بدون نیاز به ورودی متنی) ---
    if action == 'merge':
        if len(context.user_data['files']) < 2:
            await query.edit_message_text("⚠️ برای ادغام باید حداقل دو فایل فرستاده باشی.")
            return
        await query.edit_message_text("در حال ادغام... ⏳")
        writer = PdfWriter()
        for f_bytes in context.user_data['files']:
            for page in PdfReader(io.BytesIO(f_bytes)).pages:
                writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "merged.pdf")
        await query.message.delete()

    elif action == 'reverse':
        await query.edit_message_text("در حال معکوس کردن... ⏳")
        reader = PdfReader(io.BytesIO(file_bytes))
        writer = PdfWriter()
        for page in reversed(reader.pages):
            writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "reversed.pdf")
        await query.message.delete()

    elif action == 'compress':
        await query.edit_message_text("در حال فشرده‌سازی... ⏳")
        try:
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            output = io.BytesIO()
            pdf_doc.save(output, garbage=4, deflate=True, clean=True)
            output.seek(0)
            output.name = "compressed.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await query.message.delete()
        except Exception as e:
            await query.edit_message_text(f"❌ خطا در فشرده‌سازی: {str(e)}")

    elif action == 'add_numbers':
        await query.edit_message_text("در حال شماره‌گذاری... ⏳")
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        for i, page in enumerate(pdf_doc):
            text = f"{i+1}"
            page.insert_text((page.rect.width / 2, page.rect.height - 20), text, fontsize=12)
        output = io.BytesIO()
        pdf_doc.save(output)
        output.seek(0)
        output.name = "numbered.pdf"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()

    # --- عملیات‌های قبلی (استخراج، ورد، عکس، چرخش) ---
    elif action == 'extract_text':
        await query.edit_message_text("در حال استخراج متن... ⏳")
        reader = PdfReader(io.BytesIO(file_bytes))
        text = "".join([f"--- صفحه {i+1} ---\n{p.extract_text() or ''}\n\n" for i, p in enumerate(reader.pages)])
        if not text.strip():
            await query.edit_message_text("❌ متنی پیدا نشد.")
            return
        output = io.BytesIO(text.encode('utf-8'))
        output.name = "extracted_text.txt"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()

    elif action == 'to_word':
        await query.edit_message_text("در حال تبدیل به Word... ⏳")
        reader = PdfReader(io.BytesIO(file_bytes))
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
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        for i in range(min(3, len(pdf_doc))):
            pix = pdf_doc[i].get_pixmap(dpi=100)
            await context.bot.send_photo(chat_id=user_id, photo=pix.tobytes("png"), caption=f"صفحه {i+1}")
        await query.message.delete()

    elif action == 'rotate_90':
        await query.edit_message_text("در حال چرخش... ⏳")
        reader = PdfReader(io.BytesIO(file_bytes))
        writer = PdfWriter()
        for page in reader.pages:
            page.rotate(90)
            writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "rotated.pdf")
        await query.message.delete()

    # --- عملیات‌های نیازمند دریافت متن از کاربر ---
    elif action == 'encrypt':
        await query.edit_message_text("🔑 رمز عبور دلخواه را بفرستید:")
        context.user_data['state'] = 'waiting_password'
    elif action == 'decrypt':
        await query.edit_message_text("🔓 رمز عبور فعلی فایل را بفرستید:")
        context.user_data['state'] = 'waiting_decrypt'
    elif action == 'delete_pages':
        await query.edit_message_text("🗑️ شماره صفحات برای حذف را بفرست (مثلاً: 1, 3):")
        context.user_data['state'] = 'waiting_delete'
    elif action == 'split_pages':
        await query.edit_message_text("✂️ بازه صفحات را بفرست (مثلاً: 2-5):")
        context.user_data['state'] = 'waiting_split'
    elif action == 'watermark':
        await query.edit_message_text("💧 متن واترمارک را بفرست:")
        context.user_data['state'] = 'waiting_watermark'
    elif action == 'metadata':
        await query.edit_message_text("📝 عنوان جدید فایل را بفرست (مثلاً: گزارش مالی ۱۴۰۳):")
        context.user_data['state'] = 'waiting_metadata'
    elif action == 'add_cover':
        await query.edit_message_text("🖼️ عنوانی که می‌خواهی روی کاور باشد را بفرست:")
        context.user_data['state'] = 'waiting_cover'
    elif action == 'add_logo':
        await query.edit_message_text("🏷️ لطفاً **عکس لوگوی خود** را به عنوان یک عکس (Photo) بفرستید:")
        context.user_data['state'] = 'waiting_logo'

# --- پردازش پیام‌های متنی ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    if not state: return

    text = update.message.text
    user_id = update.effective_user.id
    file_bytes = context.user_data['files'][0]

    try:
        if state == 'waiting_password':
            await update.message.reply_text("در حال رمزگذاری... ⏳")
            writer = PdfWriter()
            for page in PdfReader(io.BytesIO(file_bytes)).pages:
                writer.add_page(page)
            writer.encrypt(user_password=text, owner_password="admin_pass")
            await send_pdf_file(user_id, context, writer, "encrypted.pdf")

        elif state == 'waiting_decrypt':
            await update.message.reply_text("در حال تلاش برای باز کردن قفل... ⏳")
            reader = PdfReader(io.BytesIO(file_bytes))
            if reader.is_encrypted:
                if reader.decrypt(text):
                    writer = PdfWriter()
                    for page in reader.pages:
                        writer.add_page(page)
                    await send_pdf_file(user_id, context, writer, "decrypted.pdf")
                else:
                    await update.message.reply_text("❌ رمز عبور اشتباه است!")
            else:
                await update.message.reply_text("⚠️ این فایل اصلاً رمز ندارد!")

        elif state == 'waiting_delete':
            pages = [int(p.strip()) for p in text.split(",")]
            await update.message.reply_text("در حال حذف... ⏳")
            writer = PdfWriter()
            for i, page in enumerate(PdfReader(io.BytesIO(file_bytes)).pages):
                if (i + 1) not in pages:
                    writer.add_page(page)
            await send_pdf_file(user_id, context, writer, "deleted.pdf")

        elif state == 'waiting_split':
            start, end = map(int, text.split("-"))
            await update.message.reply_text("در حال استخراج... ⏳")
            writer = PdfWriter()
            reader = PdfReader(io.BytesIO(file_bytes))
            for i in range(start-1, min(end, len(reader.pages))):
                writer.add_page(reader.pages[i])
            await send_pdf_file(user_id, context, writer, "splitted.pdf")

        elif state == 'waiting_watermark':
            await update.message.reply_text("در حال اعمال واترمارک... ⏳")
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            for i in range(min(5, len(pdf_doc))): # محدودیت ۵ صفحه
                pdf_doc[i].insert_text((50, 50), text, fontsize=30, color=(1, 0, 0), rotate=45)
            output = io.BytesIO()
            pdf_doc.save(output)
            output.seek(0)
            output.name = "watermarked.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)

        elif state == 'waiting_metadata':
            await update.message.reply_text("در حال ویرایش... ⏳")
            writer = PdfWriter()
            for page in PdfReader(io.BytesIO(file_bytes)).pages:
                writer.add_page(page)
            writer.add_metadata({"/Title": text, "/Author": "Telegram PDF Bot"})
            await send_pdf_file(user_id, context, writer, "metadata.pdf")

        elif state == 'waiting_cover':
            await update.message.reply_text("در حال ساخت کاور... ⏳")
            original_pdf = fitz.open(stream=file_bytes, filetype="pdf")
            new_pdf = fitz.open()
            cover_page = new_pdf.new_page(width=595, height=842) # A4
            cover_page.insert_text((100, 400), text, fontsize=24, color=(0, 0, 0))
            new_pdf.insert_pdf(original_pdf)
            output = io.BytesIO()
            new_pdf.save(output)
            output.seek(0)
            output.name = "with_cover.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)

    except Exception as e:
        await update.message.reply_text(f"❌ خطا در پردازش: {str(e)}")
    finally:
        context.user_data['state'] = None

# --- پردازش دریافت عکس (برای لوگو) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('state') != 'waiting_logo': return
    
    user_id = update.effective_user.id
    file_bytes = context.user_data['files'][0]
    await update.message.reply_text("در حال افزودن لوگو به صفحه اول... ⏳")
    
    try:
        photo_file = await update.message.photo[-1].get_file() # بالاترین کیفیت
        img_bytes = await photo_file.download_as_bytearray()
        
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = pdf_doc[0] # فقط صفحه اول
        # جای‌گذاری لوگو در گوشه بالا سمت راست
        rect = fitz.Rect(page.rect.width - 120, 20, page.rect.width - 20, 120)
        page.insert_image(rect, stream=img_bytes)
        
        output = io.BytesIO()
        pdf_doc.save(output)
        output.seek(0)
        output.name = "with_logo.pdf"
        await context.bot.send_document(chat_id=user_id, document=output)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در افزودن لوگو: {str(e)}")
    finally:
        context.user_data['state'] = None

def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
    application.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN, webhook_url=webhook_url)

if __name__ == '__main__':
    main()
