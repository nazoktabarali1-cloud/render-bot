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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", "8443"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
MAX_FILE_SIZE_MB = 10
MAX_PAGES_HEAVY = 300   # محدودیت برای عملیات سنگین (grayscale, watermark, split)


# ==================== توابع پردازش (Sync) ====================

def _open_reader(file_bytes):
    """باز کردن PDF با پیام خطای واضح اگر رمزدار باشد"""
    reader = PdfReader(io.BytesIO(file_bytes))
    if reader.is_encrypted:
        # سعی می‌کنیم بدون رمز باز کنیم (بعضی فایل‌ها فقط owner password دارند)
        try:
            result = reader.decrypt("")
            if result == 0:
                raise ValueError("این فایل رمزدار است. ابتدا با دکمه «باز کردن قفل» رمز را وارد کنید.")
        except Exception:
            raise ValueError("این فایل رمزدار است. ابتدا با دکمه «باز کردن قفل» رمز را وارد کنید.")
    return reader


def p_extract_text(file_bytes):
    reader = _open_reader(file_bytes)
    text = "".join(
        [f"--- صفحه {i+1} ---\n{p.extract_text() or ''}\n\n" for i, p in enumerate(reader.pages)]
    )
    out = io.BytesIO(text.encode('utf-8'))
    out.name = "extracted.txt"
    return out


def p_extract_tables(file_bytes):
    tables_text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    tables_text += " | ".join([str(c) if c else "" for c in row]) + "\n"
                tables_text += "\n"
    if not tables_text.strip():
        return None, "هیچ جدولی پیدا نشد."
    out = io.BytesIO(tables_text.encode('utf-8'))
    out.name = "tables.txt"
    return out, None


def p_extract_links(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    links = set()
    for page in pdf_doc:
        for link in page.get_links():
            if "uri" in link:
                links.add(link["uri"])
    pdf_doc.close()
    if not links:
        return None, "هیچ لینکی پیدا نشد."
    out = io.BytesIO("\n".join(sorted(links)).encode('utf-8'))
    out.name = "links.txt"
    return out, None


def p_extract_images(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    zip_buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for page_num, page in enumerate(pdf_doc):
            for img_index, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                base_image = pdf_doc.extract_image(xref)
                if base_image:
                    ext = base_image["ext"]
                    zf.writestr(f"page{page_num+1}_img{img_index+1}.{ext}", base_image["image"])
                    count += 1
    pdf_doc.close()
    if count == 0:
        return None, "هیچ عکسی پیدا نشد."
    zip_buffer.seek(0)
    zip_buffer.name = "images.zip"
    return zip_buffer, None


def p_to_markdown(file_bytes):
    reader = _open_reader(file_bytes)
    md_text = ""
    for i, page in enumerate(reader.pages):
        md_text += f"# صفحه {i+1}\n\n{page.extract_text() or ''}\n\n"
    out = io.BytesIO(md_text.encode('utf-8'))
    out.name = "text.md"
    return out


def p_translate(file_bytes):
    reader = _open_reader(file_bytes)
    full_text = "".join([p.extract_text() or "" for p in reader.pages])
    if not full_text.strip():
        return None, "متنی برای ترجمه نیست."

    # بدون محدودیت طول - تکه‌تکه ترجمه می‌شود
    chunks = [full_text[i:i+3000] for i in range(0, len(full_text), 3000)]
    trans = GoogleTranslator(source='auto', target='en')
    results = []
    for i, c in enumerate(chunks):
        try:
            results.append(trans.translate(c))
        except Exception:
            results.append(f"[خطا در ترجمه بخش {i+1}]")
    out = io.BytesIO("\n".join(results).encode('utf-8'))
    out.name = "translated.txt"
    return out, None


def p_to_word(file_bytes):
    reader = _open_reader(file_bytes)
    doc = Document()
    for page in reader.pages:
        doc.add_paragraph(page.extract_text() or "")
        doc.add_page_break()
    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    out.name = "converted.docx"
    return out


def p_split_single(file_bytes):
    reader = _open_reader(file_bytes)
    if len(reader.pages) > MAX_PAGES_HEAVY:
        return None, f"برای جلوگیری از فشار به سرور، حداکثر {MAX_PAGES_HEAVY} صفحه مجاز است."
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, page in enumerate(reader.pages):
            writer = PdfWriter()
            writer.add_page(page)
            page_buf = io.BytesIO()
            writer.write(page_buf)
            zf.writestr(f"page_{i+1}.pdf", page_buf.getvalue())
    zip_buffer.seek(0)
    zip_buffer.name = "single_pages.zip"
    return zip_buffer, None


def p_keep_first_page(file_bytes):
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    if reader.pages:
        writer.add_page(reader.pages[0])
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "first_page.pdf"
    return out


def p_merge(files_list):
    writer = PdfWriter()
    for f_bytes in files_list:
        try:
            reader = PdfReader(io.BytesIO(f_bytes))
            for page in reader.pages:
                writer.add_page(page)
        except Exception:
            continue
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "merged.pdf"
    return out


def p_reverse(file_bytes):
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    for page in reversed(reader.pages):
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "reversed.pdf"
    return out


def p_rotate(file_bytes, angle=90):
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    for page in reader.pages:
        page.rotate(angle)
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "rotated.pdf"
    return out


def p_insert_blank(file_bytes, page_num):
    """بعد از صفحه page_num یک صفحه خالی درج می‌کند. page_num=0 یعنی اول فایل."""
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    total = len(reader.pages)
    if page_num < 0 or page_num > total:
        raise ValueError(f"شماره صفحه باید بین ۰ تا {total} باشد. (۰ = اول فایل)")
    # اگر page_num == 0 باشد، اول صفحه خالی می‌گذاریم
    if page_num == 0:
        if total > 0:
            first = reader.pages[0]
            writer.add_blank_page(width=first.mediabox.width, height=first.mediabox.height)
        else:
            writer.add_blank_page()
    for i, page in enumerate(reader.pages):
        writer.add_page(page)
        if i + 1 == page_num:
            writer.add_blank_page(width=page.mediabox.width, height=page.mediabox.height)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "blank_inserted.pdf"
    return out


def p_move_page_to_end(file_bytes, page_num):
    reader = _open_reader(file_bytes)
    total = len(reader.pages)
    if page_num < 1 or page_num > total:
        raise ValueError(f"شماره صفحه باید بین ۱ تا {total} باشد.")
    writer = PdfWriter()
    target = reader.pages[page_num - 1]
    for i, page in enumerate(reader.pages):
        if i != page_num - 1:
            writer.add_page(page)
    writer.add_page(target)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "moved.pdf"
    return out


def p_highlight(file_bytes, text):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    found = False
    for page in pdf_doc:
        rects = page.search_for(text)
        for rect in rects:
            page.add_highlight_annot(rect)
            found = True
    if not found:
        pdf_doc.close()
        return None, "متن پیدا نشد."
    out = io.BytesIO()
    pdf_doc.save(out)
    pdf_doc.close()
    out.seek(0)
    out.name = "highlighted.pdf"
    return out, None


def p_find_replace(file_bytes, old_text, new_text):
    """جایگزینی متن و برگرداندن فایل متنی (چون جایگزینی واقعی در PDF پیچیده و غیرقابل‌اعتماد است)"""
    reader = _open_reader(file_bytes)
    full_text = "".join([p.extract_text() or "" for p in reader.pages])
    new_full_text = full_text.replace(old_text, new_text)
    out = io.BytesIO(new_full_text.encode('utf-8'))
    out.name = "replaced.txt"
    return out


def p_clear_metadata(file_bytes):
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata({})
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "no_metadata.pdf"
    return out


def p_restrict_print(file_bytes):
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    # permissions_flag=-3904 ≈ جلوگیری از پرینت و کپی
    writer.encrypt(user_password="", owner_password="AdminOwner123", permissions_flag=-3904)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "restricted.pdf"
    return out


def p_compress(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    out = io.BytesIO()
    pdf_doc.save(out, garbage=4, deflate=True, clean=True)
    pdf_doc.close()
    out.seek(0)
    out.name = "compressed.pdf"
    return out


def p_resize_a4(file_bytes):
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    for page in reader.pages:
        # scale_to نسبت تصویر را حفظ نمی‌کند؛ برای سازگاری ساده نگه داشته شده
        page.scale_to(width=595, height=842)  # A4
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "a4.pdf"
    return out


def p_grayscale(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    if len(pdf_doc) > MAX_PAGES_HEAVY:
        pdf_doc.close()
        return None, f"حداکثر {MAX_PAGES_HEAVY} صفحه برای سیاه‌وسفید مجاز است."
    for i in range(len(pdf_doc)):
        page = pdf_doc[i]
        pix = page.get_pixmap(colorspace=fitz.csGRAY, alpha=False)
        page.clean_contents()
        page.insert_image(page.rect, pixmap=pix)
    out = io.BytesIO()
    pdf_doc.save(out, garbage=3, deflate=True)
    pdf_doc.close()
    out.seek(0)
    out.name = "grayscale.pdf"
    return out, None


def p_timestamp(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    for page in pdf_doc:
        page.insert_text((10, 30), f"Generated: {now}", fontsize=10, color=(0.5, 0.5, 0.5))
    out = io.BytesIO()
    pdf_doc.save(out)
    pdf_doc.close()
    out.seek(0)
    out.name = "timestamped.pdf"
    return out


def p_add_numbers(file_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    for i, page in enumerate(pdf_doc):
        page.insert_text(
            (page.rect.width / 2, page.rect.height - 20),
            f"{i+1}",
            fontsize=12
        )
    out = io.BytesIO()
    pdf_doc.save(out)
    pdf_doc.close()
    out.seek(0)
    out.name = "numbered.pdf"
    return out


def p_watermark(file_bytes, text):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    if len(pdf_doc) > MAX_PAGES_HEAVY:
        pdf_doc.close()
        return None, f"حداکثر {MAX_PAGES_HEAVY} صفحه برای واترمارک مجاز است."
    for page in pdf_doc:
        # واترمارک در مرکز صفحه با چرخش (سازگار با PyMuPDF قدیمی)
        page.insert_text(
            (page.rect.width / 2 - 80, page.rect.height / 2),
            text,
            fontsize=32,
            color=(0.85, 0.1, 0.1),   # قرمز کم‌رنگ
            rotate=45
        )
    out = io.BytesIO()
    pdf_doc.save(out)
    pdf_doc.close()
    out.seek(0)
    out.name = "watermarked.pdf"
    return out, None


def p_add_cover(file_bytes, text):
    orig = fitz.open(stream=file_bytes, filetype="pdf")
    new = fitz.open()
    cover = new.new_page(width=595, height=842)
    # عنوان در مرکز
    cover.insert_text((80, 400), text, fontsize=28, color=(0, 0, 0))
    new.insert_pdf(orig)
    out = io.BytesIO()
    new.save(out)
    new.close()
    orig.close()
    out.seek(0)
    out.name = "with_cover.pdf"
    return out


def p_add_logo(file_bytes, img_bytes):
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    page = pdf_doc[0]
    rect = fitz.Rect(page.rect.width - 120, 20, page.rect.width - 20, 120)
    page.insert_image(rect, stream=img_bytes)
    out = io.BytesIO()
    pdf_doc.save(out)
    pdf_doc.close()
    out.seek(0)
    out.name = "with_logo.pdf"
    return out


def p_encrypt(file_bytes, password):
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=password, owner_password=password + "_owner")
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "encrypted.pdf"
    return out


def p_decrypt(file_bytes, password):
    reader = PdfReader(io.BytesIO(file_bytes))
    if reader.is_encrypted:
        result = reader.decrypt(password)
        if result == 0:
            return None, "رمز عبور اشتباه است!"
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "decrypted.pdf"
    return out, None


def p_delete_pages(file_bytes, page_numbers):
    reader = _open_reader(file_bytes)
    writer = PdfWriter()
    total = len(reader.pages)
    pages_to_delete = set()
    for p in page_numbers:
        if 1 <= p <= total:
            pages_to_delete.add(p)
    if not pages_to_delete:
        raise ValueError("هیچ صفحه معتبری برای حذف وارد نشده.")
    for i, page in enumerate(reader.pages):
        if (i + 1) not in pages_to_delete:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = "deleted_pages.pdf"
    return out


def p_extract_range(file_bytes, start_page, end_page):
    """استخراج محدوده صفحات (مثلاً از صفحه ۵ تا ۱۲)"""
    reader = _open_reader(file_bytes)
    total = len(reader.pages)
    if start_page < 1 or end_page > total or start_page > end_page:
        raise ValueError(f"محدوده نامعتبر است. فایل {total} صفحه دارد.")
    writer = PdfWriter()
    for i in range(start_page - 1, end_page):
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    out.name = f"pages_{start_page}_to_{end_page}.pdf"
    return out


def p_to_images(file_bytes):
    """تبدیل هر صفحه PDF به تصویر JPEG و قرار دادن در ZIP"""
    pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
    if len(pdf_doc) > MAX_PAGES_HEAVY:
        pdf_doc.close()
        return None, f"حداکثر {MAX_PAGES_HEAVY} صفحه برای تبدیل به تصویر مجاز است."
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, page in enumerate(pdf_doc):
            # رزولوشن مناسب برای کیفیت خوب
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            img_bytes = pix.tobytes("jpeg")
            zf.writestr(f"page_{i+1:03d}.jpg", img_bytes)
    pdf_doc.close()
    zip_buffer.seek(0)
    zip_buffer.name = "pages_as_images.zip"
    return zip_buffer, None


# ==================== توابع کمکی ربات ====================

async def run_bg(query, func, *args, action_name="پردازش"):
    """اجرای تابع سنگین در thread جدا + نمایش progress ساده"""
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    for i in range(1, 12):
        if task.done():
            break
        try:
            bar = "█" * i + "░" * (10 - i)
            await query.edit_message_text(f"{action_name}...\n\n📊 [{bar}] {min(i*10, 100)}%")
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return await task


def get_cancel_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='cancel')]])


def get_main_keyboard():
    return [
        [InlineKeyboardButton("📝 استخراج متن", callback_data='extract_text'),
         InlineKeyboardButton("📊 استخراج جدول", callback_data='extract_tables')],
        [InlineKeyboardButton("🔗 استخراج لینک", callback_data='extract_links'),
         InlineKeyboardButton("🖼️ استخراج عکس‌ها", callback_data='extract_images')],
        [InlineKeyboardButton("🌐 ترجمه به انگلیسی", callback_data='translate'),
         InlineKeyboardButton("📄 تبدیل به Word", callback_data='to_word')],
        [InlineKeyboardButton("📑 Markdown", callback_data='to_markdown'),
         InlineKeyboardButton("🔇 فشرده‌سازی", callback_data='compress')],
        [InlineKeyboardButton("🔗 ادغام فایل‌ها", callback_data='merge'),
         InlineKeyboardButton("✂️ تفکیک به تک‌صفحه", callback_data='split_single')],
        [InlineKeyboardButton("📄 فقط صفحه اول", callback_data='keep_first'),
         InlineKeyboardButton("🔢 شماره‌گذاری", callback_data='add_numbers')],
        [InlineKeyboardButton("🗑️ حذف صفحات", callback_data='delete_pages'),
         InlineKeyboardButton("➕ درج صفحه خالی", callback_data='insert_blank')],
        [InlineKeyboardButton("📌 جابجایی صفحه", callback_data='move_page'),
         InlineKeyboardButton("🔄 معکوس کردن", callback_data='reverse')],
        [InlineKeyboardButton("🔄 چرخش ۹۰ درجه", callback_data='rotate'),
         InlineKeyboardButton("🔍 جایگزینی متن", callback_data='find_replace')],
        [InlineKeyboardButton("🖍️ هایلایت", callback_data='highlight'),
         InlineKeyboardButton("💧 واترمارک", callback_data='watermark')],
        [InlineKeyboardButton("📏 تغییر به A4", callback_data='resize_a4'),
         InlineKeyboardButton("⚫ سیاه و سفید", callback_data='grayscale')],
        [InlineKeyboardButton("⏱️ درج تاریخ", callback_data='timestamp'),
         InlineKeyboardButton("🧹 پاکسازی متادیتا", callback_data='clear_metadata')],
        [InlineKeyboardButton("🔒 رمزگذاری", callback_data='encrypt'),
         InlineKeyboardButton("🔓 باز کردن قفل", callback_data='decrypt')],
        [InlineKeyboardButton("🚫 جلوگیری از پرینت", callback_data='restrict_print'),
         InlineKeyboardButton("🖼️ افزودن کاور", callback_data='add_cover')],
        [InlineKeyboardButton("🏷️ افزودن لوگو", callback_data='add_logo'),
         InlineKeyboardButton("📑 استخراج محدوده", callback_data='extract_range')],
        [InlineKeyboardButton("🖼️ PDF به عکس (ZIP)", callback_data='to_images'),
         InlineKeyboardButton("♻️ پاک کردن فایل‌ها", callback_data='clear_files')]
    ]


# ==================== هندلرها ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data['files'] = []
    context.user_data['state'] = None
    await update.message.reply_text(
        "سلام! به دستیار جامع PDF خوش آمدید. 📄\n\n"
        f"⚠️ حداکثر حجم هر فایل: {MAX_FILE_SIZE_MB} مگابایت\n"
        f"⚠️ حداکثر صفحات برای عملیات سنگین: {MAX_PAGES_HEAVY}\n\n"
        "لطفاً فایل PDF خود را بفرستید:"
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith('.pdf'):
        await update.message.reply_text("❌ فقط فایل PDF پذیرفته می‌شود.")
        return

    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ حجم فایل بیشتر از {MAX_FILE_SIZE_MB} مگابایت است.")
        return

    if 'files' not in context.user_data:
        context.user_data['files'] = []

    # جلوگیری از انباشته شدن بیش از حد فایل
    if len(context.user_data['files']) >= 8:
        await update.message.reply_text("⚠️ حداکثر ۸ فایل می‌توانید نگه دارید. ابتدا از دکمه «پاک کردن فایل‌ها» استفاده کنید.")
        return

    try:
        pdf_file = await doc.get_file()
        file_bytes = await pdf_file.download_as_bytearray()
        context.user_data['files'].append(bytes(file_bytes))
        context.user_data['state'] = None
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در دریافت فایل: {str(e)}")
        return

    count = len(context.user_data['files'])
    msg = "فایل دریافت شد! یک عملیات انتخاب کنید:" if count == 1 else f"✅ فایل {count} دریافت شد.\nفایل بعدی بفرستید یا عملیات را انتخاب کنید:"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(get_main_keyboard()))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == 'cancel':
        context.user_data['state'] = None
        await query.edit_message_text("✅ عملیات لغو شد.")
        return

    if query.data == 'clear_files':
        context.user_data['files'] = []
        context.user_data['state'] = None
        await query.edit_message_text("✅ همه فایل‌ها پاک شدند.\nلطفاً فایل جدید بفرستید.")
        return

    if 'files' not in context.user_data or not context.user_data['files']:
        await query.edit_message_text("❌ فایلی موجود نیست. لطفاً ابتدا فایل PDF بفرستید.")
        return

    action = query.data
    file_bytes = context.user_data['files'][0]
    out = None
    err = None

    try:
        # عملیات‌های بدون نیاز به ورودی
        if action == 'extract_text':
            out = await run_bg(query, p_extract_text, file_bytes, action_name="استخراج متن")
        elif action == 'extract_tables':
            out, err = await run_bg(query, p_extract_tables, file_bytes, action_name="استخراج جدول")
        elif action == 'extract_links':
            out, err = await run_bg(query, p_extract_links, file_bytes, action_name="استخراج لینک")
        elif action == 'extract_images':
            out, err = await run_bg(query, p_extract_images, file_bytes, action_name="استخراج عکس‌ها")
        elif action == 'to_markdown':
            out = await run_bg(query, p_to_markdown, file_bytes, action_name="تبدیل به Markdown")
        elif action == 'translate':
            out, err = await run_bg(query, p_translate, file_bytes, action_name="ترجمه")
        elif action == 'to_word':
            out = await run_bg(query, p_to_word, file_bytes, action_name="تبدیل به Word")
        elif action == 'split_single':
            out, err = await run_bg(query, p_split_single, file_bytes, action_name="تفکیک صفحات")
        elif action == 'keep_first':
            out = await run_bg(query, p_keep_first_page, file_bytes, action_name="نگه‌داشتن صفحه اول")
        elif action == 'reverse':
            out = await run_bg(query, p_reverse, file_bytes, action_name="معکوس کردن")
        elif action == 'rotate':
            out = await run_bg(query, p_rotate, file_bytes, action_name="چرخش")
        elif action == 'clear_metadata':
            out = await run_bg(query, p_clear_metadata, file_bytes, action_name="پاکسازی متادیتا")
        elif action == 'restrict_print':
            out = await run_bg(query, p_restrict_print, file_bytes, action_name="محدودسازی پرینت")
        elif action == 'compress':
            out = await run_bg(query, p_compress, file_bytes, action_name="فشرده‌سازی")
        elif action == 'resize_a4':
            out = await run_bg(query, p_resize_a4, file_bytes, action_name="تغییر اندازه به A4")
        elif action == 'grayscale':
            result = await run_bg(query, p_grayscale, file_bytes, action_name="سیاه و سفید")
            if isinstance(result, tuple):
                out, err = result
            else:
                out = result
        elif action == 'timestamp':
            out = await run_bg(query, p_timestamp, file_bytes, action_name="درج تاریخ")
        elif action == 'add_numbers':
            out = await run_bg(query, p_add_numbers, file_bytes, action_name="شماره‌گذاری")
        elif action == 'merge':
            if len(context.user_data['files']) < 2:
                await query.edit_message_text("⚠️ برای ادغام باید حداقل دو فایل فرستاده باشید.")
                return
            out = await run_bg(query, p_merge, context.user_data['files'], action_name="ادغام")
        elif action == 'to_images':
            result = await run_bg(query, p_to_images, file_bytes, action_name="تبدیل به عکس")
            if isinstance(result, tuple):
                out, err = result
            else:
                out = result

        # عملیات‌های نیازمند ورودی
        elif action == 'extract_range':
            await query.edit_message_text(
                "📑 محدوده صفحات را به این شکل بفرستید:\nمثال: 5-12\nیا فقط یک صفحه: 7",
                reply_markup=get_cancel_btn()
            )
            context.user_data['state'] = 'waiting_extract_range'
            return
        elif action == 'encrypt':
            await query.edit_message_text("🔑 رمز عبور دلخواه را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_password'
            return
        elif action == 'decrypt':
            await query.edit_message_text("🔓 رمز عبور فعلی فایل را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_decrypt'
            return
        elif action == 'delete_pages':
            await query.edit_message_text(
                "🗑️ شماره صفحات برای حذف را وارد کنید (مثلاً: 1,3,5):",
                reply_markup=get_cancel_btn()
            )
            context.user_data['state'] = 'waiting_delete'
            return
        elif action == 'insert_blank':
            await query.edit_message_text(
                "➕ بعد از کدام صفحه، صفحه خالی درج شود؟ (مثلاً 2):",
                reply_markup=get_cancel_btn()
            )
            context.user_data['state'] = 'waiting_insert_blank'
            return
        elif action == 'move_page':
            await query.edit_message_text(
                "📌 کدام صفحه به آخر منتقل شود؟ (مثلاً 2):",
                reply_markup=get_cancel_btn()
            )
            context.user_data['state'] = 'waiting_move_page'
            return
        elif action == 'find_replace':
            await query.edit_message_text(
                "🔍 متن را به این شکل بفرستید:\nقدیمی | جدید",
                reply_markup=get_cancel_btn()
            )
            context.user_data['state'] = 'waiting_find_replace'
            return
        elif action == 'highlight':
            await query.edit_message_text("🖍️ چه کلمه‌ای هایلایت شود؟", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_highlight'
            return
        elif action == 'watermark':
            await query.edit_message_text("💧 متن واترمارک را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_watermark'
            return
        elif action == 'add_cover':
            await query.edit_message_text("🖼️ عنوان کاور را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_cover'
            return
        elif action == 'add_logo':
            await query.edit_message_text("🏷️ عکس لوگو را بفرستید:", reply_markup=get_cancel_btn())
            context.user_data['state'] = 'waiting_logo'
            return

        # ارسال نتیجه
        if err:
            await query.edit_message_text(f"❌ {err}")
        elif out:
            await context.bot.send_document(chat_id=user_id, document=out)
            try:
                await query.message.delete()
            except Exception:
                pass
        else:
            await query.edit_message_text("❌ نتیجه‌ای تولید نشد.")

    except Exception as e:
        logging.exception("Error in button_handler")
        await query.edit_message_text(f"❌ خطا: {str(e)[:200]}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    if not state:
        return

    if 'files' not in context.user_data or not context.user_data['files']:
        await update.message.reply_text("❌ فایلی موجود نیست.")
        context.user_data['state'] = None
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    file_bytes = context.user_data['files'][0]
    msg = await update.message.reply_text("در حال پردازش... ⏳")
    out, err = None, None

    try:
        if state == 'waiting_password':
            if len(text) < 3:
                err = "رمز عبور باید حداقل ۳ کاراکتر باشد."
            else:
                out = await asyncio.to_thread(p_encrypt, file_bytes, text)

        elif state == 'waiting_decrypt':
            out, err = await asyncio.to_thread(p_decrypt, file_bytes, text)

        elif state == 'waiting_delete':
            try:
                pages = [int(p.strip()) for p in text.split(",") if p.strip()]
                out = await asyncio.to_thread(p_delete_pages, file_bytes, pages)
            except ValueError as ve:
                err = str(ve)
            except Exception:
                err = "فرمت ورودی اشتباه است. مثال: 1,3,5"

        elif state == 'waiting_insert_blank':
            try:
                page_num = int(text)
                out = await asyncio.to_thread(p_insert_blank, file_bytes, page_num)
            except ValueError as ve:
                err = str(ve)
            except Exception:
                err = "لطفاً فقط یک عدد وارد کنید."

        elif state == 'waiting_move_page':
            try:
                page_num = int(text)
                out = await asyncio.to_thread(p_move_page_to_end, file_bytes, page_num)
            except ValueError as ve:
                err = str(ve)
            except Exception:
                err = "لطفاً فقط یک عدد وارد کنید."

        elif state == 'waiting_find_replace':
            if "|" not in text:
                err = "فرمت اشتباه است. باید به شکل: قدیمی | جدید باشد."
            else:
                parts = text.split("|", 1)
                old, new = parts[0].strip(), parts[1].strip()
                out = await asyncio.to_thread(p_find_replace, file_bytes, old, new)

        elif state == 'waiting_highlight':
            out, err = await asyncio.to_thread(p_highlight, file_bytes, text)

        elif state == 'waiting_watermark':
            result = await asyncio.to_thread(p_watermark, file_bytes, text)
            if isinstance(result, tuple):
                out, err = result
            else:
                out = result

        elif state == 'waiting_cover':
            out = await asyncio.to_thread(p_add_cover, file_bytes, text)

        elif state == 'waiting_extract_range':
            try:
                text = text.replace(" ", "")
                if "-" in text:
                    parts = text.split("-")
                    start_page = int(parts[0])
                    end_page = int(parts[1])
                else:
                    start_page = end_page = int(text)
                out = await asyncio.to_thread(p_extract_range, file_bytes, start_page, end_page)
            except ValueError as ve:
                err = str(ve)
            except Exception:
                err = "فرمت اشتباه است. مثال صحیح: 5-12 یا فقط 7"

        if err:
            await msg.edit_text(f"❌ {err}")
        elif out:
            await context.bot.send_document(chat_id=user_id, document=out)
            await msg.delete()
        else:
            await msg.edit_text("❌ نتیجه‌ای تولید نشد.")

    except Exception as e:
        logging.exception("Error in handle_text")
        await msg.edit_text(f"❌ خطا: {str(e)[:200]}")
    finally:
        context.user_data['state'] = None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('state') != 'waiting_logo':
        return

    if 'files' not in context.user_data or not context.user_data['files']:
        await update.message.reply_text("❌ فایلی موجود نیست.")
        context.user_data['state'] = None
        return

    user_id = update.effective_user.id
    file_bytes = context.user_data['files'][0]
    msg = await update.message.reply_text("در حال افزودن لوگو... ⏳")

    try:
        photo_file = await update.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        out = await asyncio.to_thread(p_add_logo, file_bytes, bytes(img_bytes))
        await context.bot.send_document(chat_id=user_id, document=out)
        await msg.delete()
    except Exception as e:
        logging.exception("Error in handle_photo")
        await msg.edit_text(f"❌ خطا: {str(e)[:200]}")
    finally:
        context.user_data['state'] = None


def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

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

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=webhook_url
        )
    else:
        # برای تست محلی
        application.run_polling()


if __name__ == '__main__':
    main()
