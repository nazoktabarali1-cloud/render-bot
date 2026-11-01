import os
import logging
import io
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from pypdf import PdfReader

# تنظیم لاگ‌ها برای دیدن خطاها در Render
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# خواندن توکن از محیط Render
TOKEN = os.environ.get("BOT_TOKEN")
# خواندن پورت اختصاصی Render (بسیار مهم)
PORT = int(os.environ.get("PORT", "8443"))
# آدرس سرویس شما در Render (بعد از ساخت سرویس، این را در Environment تنظیم می‌کنیم)
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! من یک بات پردازش PDF هستم.\n"
        "لطفاً یک فایل PDF برای من بفرست تا تعداد صفحاتش را بشمارم."
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # دریافت فایل از کاربر
    pdf_file = await update.message.document.get_file()
    await update.message.reply_text("در حال دریافت و پردازش فایل... ⏳")

    # دانلود فایل در حافظه (بدون ذخیره روی دیسک برای امنیت و سرعت)
    file_bytes = await pdf_file.download_as_bytearray()
    
    try:
        # خواندن PDF با pypdf
        reader = PdfReader(io.BytesIO(file_bytes))
        num_pages = len(reader.pages)
        
        # اینجا می‌توانید کارهای دیگری مثل استخراج متن انجام دهید:
        # text = reader.pages[0].extract_text()
        
        await update.message.reply_text(
            f"✅ فایل با موفقیت پردازش شد!\n"
            f"📄 تعداد صفحات: {num_pages}\n\n"
            f"(برای کارهای بیشتر مثل ادغام یا استخراج متن، کد را تغییر دهید)"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در خواندن فایل: {str(e)}")

def main():
    # ساخت اپلیکیشن بات
    application = ApplicationBuilder().token(TOKEN).build()

    # اضافه کردن هندلرها (دستورات و فایل‌ها)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    # تنظیم Webhook برای اجرا روی Render
    # پورت 0.0.0.0 برای Render الزامی است
    webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
    
    # اجرای بات به صورت Webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=webhook_url
    )

if __name__ == '__main__':
    main()
