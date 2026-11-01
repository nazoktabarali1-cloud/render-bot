import os
import logging
import io
import asyncio
import fitz  # PyMuPDF
import pdfplumber
from docx import Document
from deep_translator import GoogleTranslator
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

# ---------------- توابع پردازش سنگین (Sync) ----------------

def process_extract_text(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    text = "".join([f"--- صفحه {i+1} ---\n{p.extract_text() or ''}\n\n" for i, p in enumerate(reader.pages)])
    output = io.BytesIO(text.encode('utf-8'))
    output.name = "extracted_text.txt"
    return output

def process_extract_tables(file_bytes):
    # استخراج جدول با pdfplumber
    tables_text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            if tables:
                tables_text += f"--- جدول‌های صفحه {i+1} ---\n"
                for table in tables:
                    for row in table:
                        tables_text += " | ".join([str(cell) if cell else "" for cell in row]) + "\n"
                    tables_text += "\n"
    if not tables_text.strip():
        return None, "❌ هیچ جدولی در این PDF پیدا نشد."
    output = io.BytesIO(tables_text.encode('utf-8'))
    output.name = "tables.txt"
    return output, None

def process_translate(file_bytes, target_lang='en'):
    reader = PdfReader(io.BytesIO(file_bytes))
    full_text = ""
    for page in reader.pages:
        text = page.extract_text()
        if text:
            full_text += text + "\n"
    
    if not full_text.strip():
        return None, "❌ متنی برای ترجمه پیدا نشد (احتمالاً PDF تصویری است)."
    
    max_chunk = 4000
    chunks = [full_text[i:i+max_chunk] for i in range(0, len(full_text), max_chunk)]
    translated_chunks = []
    translator = GoogleTranslator(source='auto', target=target_lang)
    
    for chunk in chunks:
        try:
            translated_chunks.append(translator.translate(chunk))
        except Exception as e:
            translated_chunks.append(f"[خطا در ترجمه بخشی از متن]")
    
    output = io.BytesIO("\n".join(translated_chunks).encode('utf-8'))
    output.name = f"translated_{target_lang}.txt"
    return output, None

def process_to_word(file_bytes):
    doc = Document()
    for page in PdfReader(io.BytesIO(file_bytes)).pages:
        doc.add_paragraph(page.extract_text() or "")
        doc.add_page_break()
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    output.name = "converted.docx"
    return output

def process_compress(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    output = io.BytesIO()
    pdf_doc.save(output, garbage=4, deflate=True, clean=True)
    output.seek(0)
    output.name = "compressed.pdf"
    return output

def process_add_numbers(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    for i, page in enumerate(pdf_doc):
        page.insert_text((page.rect.width / 2, page.rect.height - 20), f"{i+1}", fontsize=12)
    output = io.BytesIO()
    pdf_doc.save(output)
    output.seek(0)
    output.name = "numbered.pdf"
    return output

def process_watermark(file_bytes, text):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    for i in range(min(10, len(pdf_doc))):
        pdf_doc[i].insert_text((50, 50), text, fontsize=30, color=(1, 0, 0), rotate=45)
    output = io.BytesIO()
    pdf_doc.save(output)
    output.seek(0)
    output.name = "watermarked.pdf"
    return output

def process_add_cover(file_bytes, text):
    original_pdf = fitz.open(stream=file_bytes, filetype="pdf")
    new_pdf = fitz.open()
    cover = new_pdf.new_page(width=595, height=842)
    cover.insert_text((100, 400), text, fontsize=24, color=(0, 0, 0))
    new_pdf.insert_pdf(original_pdf)
    output = io.BytesIO()
    new_pdf.save(output)
    output.seek(0)
    output.name = "with_cover.pdf"
    return output

def process_add_logo(file_bytes, img_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    page = pdf_doc[0]
    rect = fitz.Rect(page.rect.width - 120, 20, page.rect.width - 20, 120)
    page.insert_image(rect, stream=img_bytes)
    output = io.BytesIO()
    pdf_doc.save(output)
    output.seek(0)
    output.name = "with_logo.pdf"
    return output

# ---------------- توابع نوار پیشرفت و اجرای پس‌زمینه ----------------

async def update_progress(query, current, total, action_name="پردازش"):
    percentage = int((current / total) * 100) if total > 0 else 0
    filled = int(percentage / 10)
    bar = "█" * filled + "░" * (10 - filled)
    try:
        await query.edit_message_text(f"{action_name}...\n\n📊 پیشرفت: [{bar}] {percentage}%")
    except Exception:
        pass

async def run_with_progress(query, coro, action_name="پردازش"):
    task = asyncio.create_task(coro)
    for i in range(1, 11):
        if task.done():
            break
        await update_progress(query, i, 10, action_name)
        await asyncio.sleep(0.5)
    return await task

# ---------------- هندلرها ----------------

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

    # منوی جدید با دکمه‌های ترجمه، جدول و OCR
    keyboard = [
        [InlineKeyboardButton("📝 استخراج متن", callback_data='extract_text'), InlineKeyboardButton("📊 استخراج جدول", callback_data='extract_tables')],
        [InlineKeyboardButton("🌐 ترجمه به انگلیسی", callback_data='translate_en'), InlineKeyboardButton("📄 خروجی Word", callback_data='to_word')],
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
        await query.edit_message_text("❌ فایل شما منقضی شده است. لطفاً دوباره PDF را بفرستید.")
        return

    action = query.data
    file_bytes = context.user_data['files'][0]

    try:
        # ---- عملیات‌های اضافه شده ----
        if action == 'extract_tables':
            output, error = await run_with_progress(query, asyncio.to_thread(process_extract_tables, file_bytes), "در حال استخراج جدول‌ها")
            if error:
                await query.edit_message_text(error)
            else:
                await context.bot.send_document(chat_id=user_id, document=output)
                await query.message.delete()

        elif action == 'translate_en':
            output, error = await run_with_progress(query, asyncio.to_thread(process_translate, file_bytes, 'en'), "در حال ترجمه به انگلیسی")
            if error:
                await query.edit_message_text(error)
            else:
                await context.bot.send_document(chat_id=user_id, document=output)
                await query.message.delete()

        # ---- عملیات‌های قبلی ----
        elif action == 'extract_text':
            output = await run_with_progress(query, asyncio.to_thread(process_extract_text, file_bytes), "در حال استخراج متن")
            await context.bot.send_document(chat_id=user_id, document=output)
            await query.message.delete()

        elif action == 'to_word':
            output = await run_with_progress(query, asyncio.to_thread(process_to_word, file_bytes), "در حال تبدیل به Word")
            await context.bot.send_document(chat_id=user_id, document=output)
            await query.message.delete()

        elif action == 'compress':
            output = await run_with_progress(query, asyncio.to_thread(process_compress, file_bytes), "در حال فشرده‌سازی")
            await context.bot.send_document(chat_id=user_id, document=output)
            await query.message.delete()

        elif action == 'add_numbers':
            output = await run_with_progress(query, asyncio.to_thread(process_add_numbers, file_bytes), "در حال شماره‌گذاری")
            await context.bot.send_document(chat_id=user_id, document=output)
            await query.message.delete()

        elif action == 'to_image':
            await query.edit_message_text("در حال تبدیل به عکس... ⏳")
            pdf_doc = await asyncio.to_thread(fitz.open, stream=file_bytes, filetype="pdf")
            total_pages = min(5, len(pdf_doc))
            for i in range(total_pages):
                await update_progress(query, i+1, total_pages, "در حال تبدیل به عکس")
                pix = pdf_doc[i].get_pixmap(dpi=100)
                await context.bot.send_photo(chat_id=user_id, photo=pix.tobytes("png"), caption=f"صفحه {i+1}")
                await asyncio.sleep(0.2)
            await query.message.delete()

        elif action == 'merge':
            if len(context.user_data['files']) < 2:
                await query.edit_message_text("⚠️ برای ادغام باید حداقل دو فایل فرستاده باشی.")
                return
            await query.edit_message_text("در حال ادغام... ⏳")
            writer = PdfWriter()
            for f_bytes in context.user_data['files']:
                for page in PdfReader(io.BytesIO(f_bytes)).pages:
                    writer.add_page(page)
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            output.name = "merged.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await query.message.delete()

        elif action == 'reverse':
            await query.edit_message_text("در حال معکوس کردن... ⏳")
            writer = PdfWriter()
            for page in reversed(PdfReader(io.BytesIO(file_bytes)).pages):
                writer.add_page(page)
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            output.name = "reversed.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await query.message.delete()

        elif action == 'rotate_90':
            await query.edit_message_text("در حال چرخش... ⏳")
            writer = PdfWriter()
            for page in PdfReader(io.BytesIO(file_bytes)).pages:
                page.rotate(90)
                writer.add_page(page)
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            output.name = "rotated.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await query.message.delete()

        # ---- عملیات‌های نیازمند ورودی ----
        elif action == 'watermark':
            await query.edit_message_text("💧 متن واترمارک را بفرستید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]]))
            context.user_data['state'] = 'waiting_watermark'

        elif action == 'delete_pages':
            await query.edit_message_text("🗑️ شماره صفحات برای حذف را بفرست (مثلاً: 1, 3):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]]))
            context.user_data['state'] = 'waiting_delete'

        elif action == 'split_pages':
            await query.edit_message_text("✂️ بازه صفحات را بفرست (مثلاً: 2-5):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]]))
            context.user_data['state'] = 'waiting_split'

        elif action == 'encrypt':
            await query.edit_message_text("🔑 رمز عبور دلخواه را بفرستید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]]))
            context.user_data['state'] = 'waiting_password'

        elif action == 'decrypt':
            await query.edit_message_text("🔓 رمز عبور فعلی فایل را بفرستید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]]))
            context.user_data['state'] = 'waiting_decrypt'

        elif action == 'metadata':
            await query.edit_message_text("📝 عنوان جدید فایل را بفرست:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]]))
            context.user_data['state'] = 'waiting_metadata'

        elif action == 'add_cover':
            await query.edit_message_text("🖼️ عنوانی که می‌خواهی روی کاور باشد را بفرست:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]]))
            context.user_data['state'] = 'waiting_cover'

        elif action == 'add_logo':
            await query.edit_message_text("🏷️ لطفاً **عکس لوگوی خود** را بفرستید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]]))
            context.user_data['state'] = 'waiting_logo'

    except Exception as e:
        await query.edit_message_text(f"❌ خطا: {str(e)}")

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
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            output.name = "encrypted.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await msg.delete()

        elif state == 'waiting_decrypt':
            reader = PdfReader(io.BytesIO(file_bytes))
            if reader.is_encrypted and reader.decrypt(text):
                writer = PdfWriter()
                for page in reader.pages: writer.add_page(page)
                output = io.BytesIO()
                writer.write(output)
                output.seek(0)
                output.name = "decrypted.pdf"
                await context.bot.send_document(chat_id=user_id, document=output)
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
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            output.name = "deleted.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await msg.delete()

        elif state == 'waiting_split':
            start, end = map(int, text.split("-"))
            writer = PdfWriter()
            reader = PdfReader(io.BytesIO(file_bytes))
            for i in range(start-1, min(end, len(reader.pages))):
                writer.add_page(reader.pages[i])
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            output.name = "splitted.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await msg.delete()

        elif state == 'waiting_watermark':
            output = await run_with_progress(msg, asyncio.to_thread(process_watermark, file_bytes, text), "در حال اعمال واترمارک")
            await context.bot.send_document(chat_id=user_id, document=output)
            await msg.delete()

        elif state == 'waiting_metadata':
            writer = PdfWriter()
            for page in PdfReader(io.BytesIO(file_bytes)).pages:
                writer.add_page(page)
            writer.add_metadata({"/Title": text, "/Author": "Telegram PDF Bot"})
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            output.name = "metadata.pdf"
            await context.bot.send_document(chat_id=user_id, document=output)
            await msg.delete()

        elif state == 'waiting_cover':
            output = await run_with_progress(msg, asyncio.to_thread(process_add_cover, file_bytes, text), "در حال ساخت کاور")
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
    msg = await update.message.reply_text("در حال افزودن لوگو... ⏳")
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        output = await run_with_progress(msg, asyncio.to_thread(process_add_logo, file_bytes, img_bytes), "در حال افزودن لوگو")
        await context.bot.send_document(chat_id=user_id, document=output)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطا در افزودن لوگو: {str(e)}")
    finally:
        context.user_data['state'] = None

def main():
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(60.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
    application.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN, webhook_url=webhook_url)

if __name__ == '__main__':
    main()
