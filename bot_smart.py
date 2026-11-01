import os
import io
import asyncio
import logging
import zipfile
import datetime
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

# ==================== توابع پردازش (Sync) ====================

def p_extract_text(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    text = "".join([f"--- صفحه {i+1} ---\n{p.extract_text() or ''}\n\n" for i, p in enumerate(reader.pages)])
    out = io.BytesIO(text.encode('utf-8')); out.name = "extracted.txt"; return out

def p_extract_tables(file_bytes):
    tables_text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            for table in page.extract_tables():
                for row in table:
                    tables_text += " | ".join([str(c) if c else "" for c in row]) + "\n"
                tables_text += "\n"
    if not tables_text.strip(): return None, "هیچ جدولی پیدا نشد."
    out = io.BytesIO(tables_text.encode('utf-8')); out.name = "tables.txt"; return out, None

def p_extract_links(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    links = set()
    for page in pdf_doc:
        for link in page.get_links():
            if "uri" in link: links.add(link["uri"])
    if not links: return None, "هیچ لینکی پیدا نشد."
    out = io.BytesIO("\n".join(links).encode('utf-8')); out.name = "links.txt"; return out, None

def p_extract_images(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    zip_buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        for page in pdf_doc:
            for img in page.get_images(full=True):
                xref = img[0]
                base_image = pdf_doc.extract_image(xref)
                if base_image:
                    image_bytes = base_image["image"]
                    ext = base_image["ext"]
                    zf.writestr(f"image_{count}.{ext}", image_bytes)
                    count += 1
    if count == 0: return None, "هیچ عکسی پیدا نشد."
    zip_buffer.seek(0); zip_buffer.name = "images.zip"; return zip_buffer, None

def p_to_markdown(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    md_text = ""
    for i, page in enumerate(reader.pages):
        md_text += f"# صفحه {i+1}\n\n{page.extract_text() or ''}\n\n"
    out = io.BytesIO(md_text.encode('utf-8')); out.name = "text.md"; return out

def p_translate(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    full_text = "".join([p.extract_text() for p in reader.pages if p.extract_text()])
    if not full_text.strip(): return None, "متنی برای ترجمه نیست."
    
    chunks = [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]
    trans = GoogleTranslator(source='auto', target='en')
    res = [trans.translate(c) for c in chunks]
    out = io.BytesIO("\n".join(res).encode('utf-8')); out.name = "translated.txt"; return out, None

def p_to_word(file_bytes):
    doc = Document()
    for page in PdfReader(io.BytesIO(file_bytes)).pages:
        doc.add_paragraph(page.extract_text() or "")
        doc.add_page_break()
    out = io.BytesIO(); doc.save(out); out.seek(0); out.name = "converted.docx"; return out

def p_split_single(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    if len(reader.pages) > 20: return None, "برای جلوگیری از فشار به سرور، حداکثر ۲۰ صفحه مجاز است."
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        for i, page in enumerate(reader.pages):
            writer = PdfWriter(); writer.add_page(page)
            out = io.BytesIO(); writer.write(out)
            zf.writestr(f"page_{i+1}.pdf", out.getvalue())
    zip_buffer.seek(0); zip_buffer.name = "single_pages.zip"; return zip_buffer, None

def p_keep_first_page(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes)); writer = PdfWriter()
    if reader.pages: writer.add_page(reader.pages[0])
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "first_page.pdf"; return out

def p_merge(files_list):
    writer = PdfWriter()
    for f_bytes in files_list:
        for page in PdfReader(io.BytesIO(f_bytes)).pages: writer.add_page(page)
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "merged.pdf"; return out

def p_reverse(file_bytes):
    writer = PdfWriter()
    for page in reversed(PdfReader(io.BytesIO(file_bytes)).pages): writer.add_page(page)
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "reversed.pdf"; return out

def p_rotate(file_bytes, angle=90):
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(file_bytes)).pages:
        page.rotate(angle); writer.add_page(page)
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "rotated.pdf"; return out

def p_insert_blank(file_bytes, page_num):
    reader = PdfReader(io.BytesIO(file_bytes)); writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        writer.add_page(page)
        if i + 1 == page_num:
            writer.add_blank_page(width=page.mediabox.width, height=page.mediabox.height)
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "blank_inserted.pdf"; return out

def p_move_page_to_end(file_bytes, page_num):
    reader = PdfReader(io.BytesIO(file_bytes)); writer = PdfWriter()
    target = reader.pages[page_num - 1]
    for i, page in enumerate(reader.pages):
        if i != page_num - 1: writer.add_page(page)
    writer.add_page(target)
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "moved.pdf"; return out

def p_highlight(file_bytes, text):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    found = False
    for page in pdf_doc:
        rects = page.search_for(text)
        for rect in rects:
            page.add_highlight_annot(rect)
            found = True
    if not found: return None, "متن پیدا نشد."
    out = io.BytesIO(); pdf_doc.save(out); out.seek(0); out.name = "highlighted.pdf"; return out, None

def p_find_replace(file_bytes, old_text, new_text):
    reader = PdfReader(io.BytesIO(file_bytes))
    full_text = "".join([p.extract_text() or "" for p in reader.pages])
    new_full_text = full_text.replace(old_text, new_text)
    out = io.BytesIO(new_full_text.encode('utf-8')); out.name = "replaced.txt"; return out

def p_clear_metadata(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes)); writer = PdfWriter()
    for page in reader.pages: writer.add_page(page)
    writer.add_metadata({})
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "no_metadata.pdf"; return out

def p_restrict_print(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes)); writer = PdfWriter()
    for page in reader.pages: writer.add_page(page)
    # رمز owner برای ادمین است، کاربر عادی نمی‌تواند پرینت بگیرد
    writer.encrypt(user_password="", owner_password="AdminOwner123", permissions_flag=-3904)
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "restricted.pdf"; return out

def p_compress(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    out = io.BytesIO(); pdf_doc.save(out, garbage=4, deflate=True, clean=True)
    out.seek(0); out.name = "compressed.pdf"; return out

def p_resize_a4(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes)); writer = PdfWriter()
    for page in reader.pages:
        page.scale_to(width=595, height=842) # A4 در واحد point
        writer.add_page(page)
    out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "a4.pdf"; return out

def p_grayscale(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    for i in range(min(10, len(pdf_doc))): # محدودیت ۱۰ صفحه
        page = pdf_doc[i]
        pix = page.get_pixmap(colorspace=fitz.csGRAY)
        page.clean_contents()
        page.insert_image(page.rect, pixmap=pix)
    out = io.BytesIO(); pdf_doc.save(out); out.seek(0); out.name = "grayscale.pdf"; return out

def p_timestamp(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    for page in pdf_doc:
        page.insert_text((10, 30), f"Generated: {now}", fontsize=10, color=(0.5, 0.5, 0.5))
    out = io.BytesIO(); pdf_doc.save(out); out.seek(0); out.name = "timestamped.pdf"; return out

def p_add_numbers(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    for i, page in enumerate(pdf_doc):
        page.insert_text((page.rect.width / 2, page.rect.height - 20), f"{i+1}", fontsize=12)
    out = io.BytesIO(); pdf_doc.save(out); out.seek(0); out.name = "numbered.pdf"; return out

def p_watermark(file_bytes, text):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    for i in range(min(10, len(pdf_doc))):
        pdf_doc[i].insert_text((50, 50), text, fontsize=30, color=(1, 0, 0), rotate=45)
    out = io.BytesIO(); pdf_doc.save(out); out.seek(0); out.name = "watermarked.pdf"; return out

def p_add_cover(file_bytes, text):
    orig = fitz.open(stream=file_bytes, filetype="pdf"); new = fitz.open()
    cover = new.new_page(width=595, height=842)
    cover.insert_text((100, 400), text, fontsize=24, color=(0, 0, 0))
    new.insert_pdf(orig)
    out = io.BytesIO(); new.save(out); out.seek(0); out.name = "with_cover.pdf"; return out

def p_add_logo(file_bytes, img_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    page = pdf_doc[0]
    rect = fitz.Rect(page.rect.width - 120, 20, page.rect.width - 20, 120)
    page.insert_image(rect, stream=img_bytes)
    out = io.BytesIO(); pdf_doc.save(out); out.seek(0); out.name = "with_logo.pdf"; return out

# ==================== توابع کمکی ربات ====================

async def run_bg(query, func, *args, action_name="پردازش"):
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    for i in range(1, 11):
        if task.done(): break
        try:
            await query.edit_message_text(f"{action_name}...\n\n📊 [{('█'*i).ljust(10, '░')}] {i*10}%")
        except: pass
        await asyncio.sleep(0.5)
    return await task

def get_cancel_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]])

# ==================== هندلرها ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['files'] = []
    context.user_data['state'] = None
    await update.message.reply_text(
        "سلام! به دستیار جامع PDF خوش آمدید. 📄\n"
        f"⚠️ حداکثر حجم: {MAX_FILE_SIZE_MB} مگابایت.\n"
        "لطفاً فایل PDF خود را بفرستید:"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ حجم فایل بیشتر از {MAX_FILE_SIZE_MB} مگابایت است.")
        return

    if 'files' not in context.user_data: context.user_data['files'] = []
    pdf_file = await doc.get_file()
    file_bytes = await pdf_file.download_as_bytearray()
    context.user_data['files'].append(file_bytes)
    context.user_data['state'] = None

    keyboard = [
        [InlineKeyboardButton("📝 استخراج متن", callback_data='extract_text'), InlineKeyboardButton("📊 استخراج جدول", callback_data='extract_tables')],
        [InlineKeyboardButton("🔗 استخراج لینک", callback_data='extract_links'), InlineKeyboardButton("🖼️ استخراج عکس‌ها (ZIP)", callback_data='extract_images')],
        [InlineKeyboardButton("🌐 ترجمه", callback_data='translate'), InlineKeyboardButton("📄 Word", callback_data='to_word')],
        [InlineKeyboardButton("📑 Markdown", callback_data='to_markdown'), InlineKeyboardButton("🔇 فشرده‌سازی", callback_data='compress')],
        [InlineKeyboardButton("🔗 ادغام", callback_data='merge'), InlineKeyboardButton("✂️ تفکیک به تک‌صفحه", callback_data='split_single')],
        [InlineKeyboardButton("📄 نگه‌داشتن صفحه اول", callback_data='keep_first'), InlineKeyboardButton("🔢 شماره‌گذاری", callback_data='add_numbers')],
        [InlineKeyboardButton("🗑️ حذف صفحات", callback_data='delete_pages'), InlineKeyboardButton("➕ درج صفحه خالی", callback_data='insert_blank')],
        [InlineKeyboardButton("📌 جابجایی صفحه", callback_data='move_page'), InlineKeyboardButton("🔄 معکوس", callback_data='reverse')],
        [InlineKeyboardButton("🔄 چرخش ۹۰", callback_data='rotate'), InlineKeyboardButton("🔍 جستجو و جایگزینی", callback_data='find_replace')],
        [InlineKeyboardButton("🖍️ هایلایت متن", callback_data='highlight'), InlineKeyboardButton("💧 واترمارک", callback_data='watermark')],
        [InlineKeyboardButton("📏 اندازه A4", callback_data='resize_a4'), InlineKeyboardButton("⚫ سیاه و سفید", callback_data='grayscale')],
        [InlineKeyboardButton("⏱️ درج تاریخ", callback_data='timestamp'), InlineKeyboardButton("🧹 پاکسازی متادیتا", callback_data='clear_metadata')],
        [InlineKeyboardButton("🔒 رمزگذاری", callback_data='encrypt'), InlineKeyboardButton("🔓 باز کردن قفل", callback_data='decrypt')],
        [InlineKeyboardButton("🚫 جلوگیری از پرینت", callback_data='restrict_print'), InlineKeyboardButton("🖼️ افزودن کاور", callback_data='add_cover')],
        [InlineKeyboardButton("🏷️ افزودن لوگو", callback_data='add_logo')]
    ]
    count = len(context.user_data['files'])
    msg = "فایل دریافت شد! یک عملیات انتخاب کنید:" if count == 1 else f"فایل {count} دریافت شد. فایل بعدی یا عملیات:"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == 'cancel':
        context.user_data['state'] = None
        await query.edit_message_text("✅ عملیات لغو شد.")
        return

    if 'files' not in context.user_data or not context.user_data['files']:
        await query.edit_message_text("❌ فایل منقضی شده است.")
        return

    action = query.data
    file_bytes = context.user_data['files'][0]
    out = None
    err = None

    try:
        # عملیات‌های بدون نیاز به ورودی
        if action == 'extract_text': out = await run_bg(query, p_extract_text, file_bytes, action_name="استخراج متن")
        elif action == 'extract_tables': out, err = await run_bg(query, p_extract_tables, file_bytes, action_name="استخراج جدول")
        elif action == 'extract_links': out, err = await run_bg(query, p_extract_links, file_bytes, action_name="استخراج لینک")
        elif action == 'extract_images': out, err = await run_bg(query, p_extract_images, file_bytes, action_name="استخراج عکس‌ها")
        elif action == 'to_markdown': out = await run_bg(query, p_to_markdown, file_bytes, action_name="تبدیل به Markdown")
        elif action == 'translate': out, err = await run_bg(query, p_translate, file_bytes, action_name="ترجمه")
        elif action == 'to_word': out = await run_bg(query, p_to_word, file_bytes, action_name="تبدیل به Word")
        elif action == 'split_single': out, err = await run_bg(query, p_split_single, file_bytes, action_name="تفکیک صفحات")
        elif action == 'keep_first': out = await run_bg(query, p_keep_first_page, file_bytes, action_name="نگه‌داشتن صفحه اول")
        elif action == 'reverse': out = await run_bg(query, p_reverse, file_bytes, action_name="معکوس کردن")
        elif action == 'rotate': out = await run_bg(query, p_rotate, file_bytes, action_name="چرخش")
        elif action == 'clear_metadata': out = await run_bg(query, p_clear_metadata, file_bytes, action_name="پاکسازی متادیتا")
        elif action == 'restrict_print': out = await run_bg(query, p_restrict_print, file_bytes, action_name="محدودسازی پرینت")
        elif action == 'compress': out = await run_bg(query, p_compress, file_bytes, action_name="فشرده‌سازی")
        elif action == 'resize_a4': out = await run_bg(query, p_resize_a4, file_bytes, action_name="تغییر اندازه به A4")
        elif action == 'grayscale': out = await run_bg(query, p_grayscale, file_bytes, action_name="سیاه و سفید")
        elif action == 'timestamp': out = await run_bg(query, p_timestamp, file_bytes, action_name="درج تاریخ")
        elif action == 'add_numbers': out = await run_bg(query, p_add_numbers, file_bytes, action_name="شماره‌گذاری")
        elif action == 'merge': 
            if len(context.user_data['files']) < 2:
                await query.edit_message_text("⚠️ برای ادغام باید حداقل دو فایل فرستاده باشی.")
                return
            out = await run_bg(query, p_merge, context.user_data['files'], action_name="ادغام")

        # عملیات‌های نیازمند ورودی متنی
        elif action == 'encrypt':
            await query.edit_message_text("🔑 رمز عبور دلخواه را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_password'; return
        elif action == 'decrypt':
            await query.edit_message_text("🔓 رمز عبور فعلی فایل را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_decrypt'; return
        elif action == 'delete_pages':
            await query.edit_message_text("🗑️ شماره صفحات برای حذف (مثلاً 1, 3):", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_delete'; return
        elif action == 'insert_blank':
            await query.edit_message_text("➕ بعد از کدام صفحه، صفحه خالی درج شود؟ (مثلاً 2):", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_insert_blank'; return
        elif action == 'move_page':
            await query.edit_message_text("📌 کدام صفحه به آخر منتقل شود؟ (مثلاً 2):", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_move_page'; return
        elif action == 'find_replace':
            await query.edit_message_text("🔍 متن را به این شکل بفرستید: قدیمی | جدید", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_find_replace'; return
        elif action == 'highlight':
            await query.edit_message_text("🖍️ چه کلمه‌ای هایلایت شود؟", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_highlight'; return
        elif action == 'watermark':
            await query.edit_message_text("💧 متن واترمارک را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_watermark'; return
        elif action == 'add_cover':
            await query.edit_message_text("🖼️ عنوان کاور را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_cover'; return
        elif action == 'add_logo':
            await query.edit_message_text("🏷️ **عکس لوگو** را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_logo'; return

        if err:
            await query.edit_message_text(f"❌ {err}")
        elif out:
            await context.bot.send_document(chat_id=user_id, document=out)
            await query.message.delete()

    except Exception as e:
        await query.edit_message_text(f"❌ خطا: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    if not state: return
    text = update.message.text
    user_id = update.effective_user.id
    file_bytes = context.user_data['files'][0]
    msg = await update.message.reply_text("در حال پردازش... ⏳")
    out, err = None, None

    try:
        if state == 'waiting_password':
            writer = PdfWriter()
            for page in PdfReader(io.BytesIO(file_bytes)).pages: writer.add_page(page)
            writer.encrypt(user_password=text, owner_password="admin_pass")
            out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "encrypted.pdf"
        elif state == 'waiting_decrypt':
            reader = PdfReader(io.BytesIO(file_bytes))
            if reader.is_encrypted and reader.decrypt(text):
                writer = PdfWriter()
                for page in reader.pages: writer.add_page(page)
                out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "decrypted.pdf"
            else: err = "رمز عبور اشتباه است!"
        elif state == 'waiting_delete':
            pages = [int(p.strip()) for p in text.split(",")]
            reader = PdfReader(io.BytesIO(file_bytes)); writer = PdfWriter()
            for i, page in enumerate(reader.pages):
                if (i + 1) not in pages: writer.add_page(page)
            out = io.BytesIO(); writer.write(out); out.seek(0); out.name = "deleted.pdf"
        elif state == 'waiting_insert_blank':
            out = await asyncio.to_thread(p_insert_blank, file_bytes, int(text))
        elif state == 'waiting_move_page':
            out = await asyncio.to_thread(p_move_page_to_end, file_bytes, int(text))
        elif state == 'waiting_find_replace':
            old, new = text.split("|")
            out = await asyncio.to_thread(p_find_replace, file_bytes, old.strip(), new.strip())
        elif state == 'waiting_highlight':
            out, err = await asyncio.to_thread(p_highlight, file_bytes, text)
        elif state == 'waiting_watermark':
            out = await asyncio.to_thread(p_watermark, file_bytes, text)
        elif state == 'waiting_cover':
            out = await asyncio.to_thread(p_add_cover, file_bytes, text)

        if err: await msg.edit_text(f"❌ {err}")
        elif out:
            await context.bot.send_document(chat_id=user_id, document=out)
            await msg.delete()

    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)}")
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
        out = await asyncio.to_thread(p_add_logo, file_bytes, img_bytes)
        await context.bot.send_document(chat_id=user_id, document=out)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)}")
    finally:
        context.user_data['state'] = None

def main():
    application = ApplicationBuilder().token(TOKEN).connect_timeout(60.0).read_timeout(60.0).write_timeout(60.0).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
    application.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN, webhook_url=webhook_url)

if __name__ == '__main__':
    main()
