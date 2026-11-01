import os
import logging
import io
import fitz  # PyMuPDF
import asyncio
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

async def send_pdf_file(chat_id, context, writer, filename):
    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    output.name = filename
    await context.bot.send_document(chat_id=chat_id, document=output)

async def update_progress(query, current, total, action_name="پردازش"):
    percentage = int((current / total) * 100) if total > 0 else 0
    filled = int(percentage / 10)
    bar = "█" * filled + "░" * (10 - filled)
    try:
        await query.edit_message_text(f"{action_name}...\n\n📊 پیشرفت: [{bar}] {percentage}%")
    except Exception:
        pass

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]])

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
    
    if query.data == 'cancel':
        context.user_data['state'] = None
        await query.edit_message_text("✅ عملیات لغو شد.")
        return

    if 'files' not in context.user_data or not context.user_data['files']:
        await query.edit_message_text("❌ فایل شما منقضی شده است.")
        return

    action = query.data
    file_bytes = context.user_data['files'][0]

    if action == 'extract_text':
        reader = PdfReader(io.BytesIO(file_bytes))
        total_pages = len(reader.pages)
        text = ""
        for i, page in enumerate(reader.pages):
            text += f"--- صفحه {i+1} ---\n{page.extract_text() or ''}\n\n"
            if (i + 1) % max(1, total_pages // 10) == 0 or (i+1) == total_pages:
                await update_progress(query, i+1, total_pages, "در حال استخراج متن")
                await asyncio.sleep(0.1)
        
        if not text.strip():
            await query.edit_message_text("❌ متنی پیدا نشد.", reply_markup=get_cancel_keyboard())
            return
        output = io.BytesIO(text.encode('utf-8'))
        output.name = "extracted_text.txt"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()

    elif action == 'to_image':
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        total_pages = min(5, len(pdf_doc))
        for i in range(total_pages):
            await update_progress(query, i+1, total_pages, "در حال تبدیل به عکس")
            pix = pdf_doc[i].get_pixmap(dpi=100)
            await context.bot.send_photo(chat_id=user_id, photo=pix.tobytes("png"), caption=f"صفحه {i+1}")
        await query.message.delete()

    elif action == 'watermark':
        await query.edit_message_text("💧 متن واترمارک را بفرستید:", reply_markup=get_cancel_keyboard())
        context.user_data['state'] = 'waiting_watermark'

    elif action == 'delete_pages':
        await query.edit_message_text("🗑️ شماره صفحات برای حذف را بفرست (مثلاً: 1, 3):", reply_markup=get_cancel_keyboard())
        context.user_data['state'] = 'waiting_delete'

    elif action == 'split_pages':
        await query.edit_message_text("✂️ بازه صفحات را بفرست (مثلاً: 2-5):", reply_markup=get_cancel_keyboard())
        context.user_data['state'] = 'waiting_split'

    elif action == 'encrypt':
        await query.edit_message_text("🔑 رمز عبور دلخواه را بفرستید:", reply_markup=get_cancel_keyboard())
        context.user_data['state'] = 'waiting_password'

    elif action == 'decrypt':
        await query.edit_message_text("🔓 رمز عبور فعلی فایل را بفرستید:", reply_markup=get_cancel_keyboard())
        context.user_data['state'] = 'waiting_decrypt'

    elif action == 'metadata':
        await query.edit_message_text("📝 عنوان جدید فایل را بفرست:", reply_markup=get_cancel_keyboard())
        context.user_data['state'] = 'waiting_metadata'

    elif action == 'add_cover':
        await query.edit_message_text("🖼️ عنوانی که می‌خواهی روی کاور باشد را بفرست:", reply_markup=get_cancel_keyboard())
        context.user_data['state'] = 'waiting_cover'

    elif action == 'add_logo':
        await query.edit_message_text("🏷️ لطفاً **عکس لوگوی خود** را بفرستید:", reply_markup=get_cancel_keyboard())
        context.user_data['state'] = 'waiting_logo'

    elif action == 'merge':
        if len(context.user_data['files']) < 2:
            await query.edit_message_text("⚠️ برای ادغام باید حداقل دو فایل فرستاده باشی.", reply_markup=get_cancel_keyboard())
            return
        await query.edit_message_text("در حال ادغام... ⏳")
        writer = PdfWriter()
        for f_bytes in context.user_data['files']:
            for page in PdfReader(io.BytesIO(f_bytes)).pages:
                writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "merged.pdf")
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
            await query.edit_message_text(f"❌ خطا: {str(e)}")

    elif action == 'reverse':
        await query.edit_message_text("در حال معکوس کردن... ⏳")
        writer = PdfWriter()
        for page in reversed(PdfReader(io.BytesIO(file_bytes)).pages):
            writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "reversed.pdf")
        await query.message.delete()

    elif action == 'add_numbers':
        await query.edit_message_text("در حال شماره‌گذاری... ⏳")
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        for i, page in enumerate(pdf_doc):
            page.insert_text((page.rect.width / 2, page.rect.height - 20), f"{i+1}", fontsize=12)
        output = io.BytesIO()
        pdf_doc.save(output)
        output.seek(0)
        output.name = "numbered.pdf"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()

    elif action == 'to_word':
        await query.edit_message_text("در حال تبدیل به Word... ⏳")
        doc = Document()
        for page in PdfReader(io.BytesIO(file_bytes)).pages:
            doc.add_paragraph(page.extract_text() or "")
            doc.add_page_break()
        output = io.BytesIO()
        doc.save(output)
        output.seek(0)
        output.name = "converted.docx"
        await context.bot.send_document(chat_id=user_id, document=output)
        await query.message.delete()

    elif action == 'rotate_90':
        await query.edit_message_text("در حال چرخش... ⏳")
        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(file_bytes)).pages:
            page.rotate(90)
            writer.add_page(page)
        await send_pdf_file(user_id, context, writer, "rotated.pdf")
        await query.message.delete()

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    if not state: return

    text = update.message.text
    user_id = update.effective_user.id
    file_bytes = context.user_data['files'][0]
    msg = await update.message.reply_text("در حال پردازش... ⏳")

    try:
        if state == 'waiting_password':
            writer = PdfWriter()
            for page in PdfReader(io.BytesIO(file_bytes)).pages:
                writer.add_page(page)
            writer.encrypt(user_password=text, owner_password="admin_pass")
            await send_pdf_file(user_id, context, writer, "encrypted.pdf")
            await msg.delete()

        elif state == 'waiting_decrypt':
            reader = PdfReader(io.BytesIO(file_bytes))
            if reader.is_encrypted and reader.decrypt(text):
                writer = PdfWriter()
                for page in reader.pages: writer.add_page(page)
                await send_pdf_file(user_id, context, writer, "decrypted.pdf")
                await msg.delete()
            else:
                await msg.edit_text("❌ رمز عبور اشتباه است!")

        elif state == 'waiting_delete':
            pages = [int(p.strip()) for p in text.split(",")]
            reader = PdfReader(io.BytesIO(file_bytes))
            writer = PdfWriter()
            for i, page in enumerate(reader.pages):
                if (i + 1) not in pages:
                    writer.add_page(page)
            await send_pdf_file(user_id, context, writer, "deleted.pdf")
            await msg.delete()

        elif state == 'waiting_split':
            start, end = map(int, text.split("-"))
            writer = PdfWriter()
            reader = PdfReader(io.BytesIO(file_bytes))
            for i in range(start-1, min(end, len(reader.pages))):
                writer.add_page(reader.pages[i])
            await send_pdf_file(user_id, context, writer, "splitted.pdf")
            await msg.delete()

        elif state == 'waiting_watermark':
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            total_pages = min(5, len(pdf_doc))
            for i in range(total_pages):
                await update_progress(msg, i+1, total_pages, "در حال اعمال واترمارک")
                pdf_doc[i].insert_text((50, 50), text, fontsize=30, color=(1, 0, 0), rotate=45)
            output = io.BytesIO()
            pdf_doc.save(output)
            output.seek(0)
            output.name = "watermarked.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await msg.delete()

        elif state == 'waiting_metadata':
            writer = PdfWriter()
            for page in PdfReader(io.BytesIO(file_bytes)).pages:
                writer.add_page(page)
            writer.add_metadata({"/Title": text, "/Author": "Telegram PDF Bot"})
            await send_pdf_file(user_id, context, writer, "metadata.pdf")
            await msg.delete()

        elif state == 'waiting_cover':
            original_pdf = fitz.open(stream=file_bytes, filetype="pdf")
            new_pdf = fitz.open()
            cover = new_pdf.new_page(width=595, height=842)
            cover.insert_text((100, 400), text, fontsize=24, color=(0, 0, 0))
            new_pdf.insert_pdf(original_pdf)
            output = io.BytesIO()
            new_pdf.save(output)
            output.seek(0)
            output.name = "with_cover.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await msg.delete()

    except Exception as e:
        await msg.edit_text(f"❌ خطا در پردازش: {str(e)}")
    finally:
        context.user_data['state'] = None

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('state') != 'waiting_logo': return
    
    user_id = update.effective_user.id
    file_bytes = context.user_data['files'][0]
    msg = await update.message.reply_text("در حال افزودن لوگو به صفحه اول... ⏳")
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = pdf_doc[0]
        rect = fitz.Rect(page.rect.width - 120, 20, page.rect.width - 20, 120)
        page.insert_image(rect, stream=img_bytes)
        
        output = io.BytesIO()
        pdf_doc.save(output)
        output.seek(0)
        output.name = "with_logo.pdf"
        await context.bot.send_document(chat_id=user_id, document=output)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطا در افزودن لوگو: {str(e)}")
    finally:
        context.user_data['state'] = None

def main():
    # این خط مهم‌ترین تغییر است! concurrent_updates(True) مشکل دکمه‌ها را حل می‌کند
    application = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
    application.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN, webhook_url=webhook_url)

if __name__ == '__main__':
    main()
