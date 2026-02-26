import os
import logging
import asyncio
import qrcode
import threading
import mimetypes
from io import BytesIO
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from supabase import create_client, Client

# ========== 核心配置 ==========
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_BUCKET_NAME = "public-files"
BJ_TZ = timezone(timedelta(hours=8))

logging.basicConfig(level=logging.INFO)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== 极简 Web 服务器 (仅用于 Render 保活) ==========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

# ========== 机器人逻辑 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！直接发送文件即可上传，发送 /list 查看列表。", reply_markup=ReplyKeyboardRemove())

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    try:
        items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        files = [i for i in items if i['name'] != '.emptyFolderPlaceholder']
        files.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        total_size = sum(int(f.get('metadata', {}).get('size', 0)) for f in files)
        size_str = f"{total_size/(1024*1024):.1f} MB" if total_size > 1024*1024 else f"{total_size/1024:.1f} KB"
        
        text = f"存储统计：{size_str} / 1 GB\n\n文件列表：\n"
        kb = []
        for f in files[page*8 : (page+1)*8]:
            name = f['name']
            # 使用文件名作为 callback_data，如果太长则截断（Telegram 限制 64 字节）
            kb.append([InlineKeyboardButton(name, callback_data=f"v:{name[:50]}")])
        
        if len(files) > 8:
            nav = []
            if page > 0: nav.append(InlineKeyboardButton("上一页", callback_data=f"p:{page-1}"))
            if (page+1)*8 < len(files): nav.append(InlineKeyboardButton("下一页", callback_data=f"p:{page+1}"))
            if nav: kb.append(nav)
        
        kb.append([InlineKeyboardButton("刷新列表", callback_data="p:0")])
        
        msg_text = text + ("暂无文件" if not files else "")
        if update.callback_query:
            await update.callback_query.edit_message_text(msg_text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logging.error(e)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("p:"): await list_files(update, context, page=int(data[2:]))
    elif data.startswith("v:"): await show_detail(update, context, data[2:])
    elif data.startswith("d:"): await delete_file(update, context, data[2:])

async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
    # 模糊匹配文件名（处理 callback_data 截断的情况）
    f = next((i for i in items if i['name'].startswith(name)), None)
    if not f: return
    
    full_name = f['name']
    size_raw = int(f.get('metadata', {}).get('size', 0))
    size = f"{size_raw/(1024*1024):.1f} MB" if size_raw > 1024*1024 else f"{size_raw/1024:.1f} KB"
    time_str = datetime.fromisoformat(f['created_at'].replace('Z', '+00:00')).astimezone(BJ_TZ).strftime('%Y-%m-%d %H:%M') if f.get('created_at') else "未知"

    # 严格使用原始直连链接
    long_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET_NAME}/{full_name}"
    
    # 生成二维码
    qr = qrcode.make(long_url)
    buf = BytesIO(); qr.save(buf, format='PNG'); buf.seek(0)
    
    text = (
        f"文件详情\n\n"
        f"文件名：{full_name}\n"
        f"大小：{size}\n"
        f"上传时间：{time_str}\n\n"
        f"点击下载：[点击此处]({long_url})\n\n"
        f"链接：{long_url}"
    )
    kb = [[InlineKeyboardButton("删除文件", callback_data=f"d:{full_name[:50]}")], [InlineKeyboardButton("返回列表", callback_data="p:0")]]
    await update.effective_chat.send_photo(photo=buf, caption=text, reply_markup=InlineKeyboardMarkup(kb))

async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
    f = next((i for i in items if i['name'].startswith(name)), None)
    if f:
        full_name = f['name']
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([full_name])
        await update.callback_query.edit_message_text(f"已删除文件：{full_name}")
        await asyncio.sleep(1)
        await list_files(update, context)

async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file: return
    name = getattr(file, 'file_name', f"file_{datetime.now(BJ_TZ).strftime('%H%M%S')}")
    status_msg = await msg.reply_text(f"正在上传：{name}...")
    try:
        tg_file = await context.bot.get_file(file.file_id)
        f_path = await tg_file.download_to_drive()
        mtype, _ = mimetypes.guess_type(name)
        if name.endswith('.apk'): mtype = 'application/vnd.android.package-archive'
        with open(f_path, 'rb') as f:
            supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=name, file=f.read(), file_options={'upsert':'true', 'content-type': mtype or 'application/octet-stream'})
        await status_msg.delete()
        await show_detail(update, context, name)
        if os.path.exists(f_path): os.remove(f_path)
    except Exception as e: await status_msg.edit_text(f"上传失败：{e}")

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO, handle_upload))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()

if __name__ == '__main__': main()
