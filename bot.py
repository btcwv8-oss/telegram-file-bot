import os
import json
import logging
import asyncio
import qrcode
import hashlib
import threading
import requests
import mimetypes
import urllib.parse
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
ADMIN_USERNAMES = ["btcwv", "LDvipa"]
BJ_TZ = timezone(timedelta(hours=8))

logging.basicConfig(level=logging.INFO)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== 状态管理 ==========
user_state = {} 
path_map = {} # 存储短ID到长路径的映射，确保按键响应

def get_id(path):
    sid = hashlib.md5(path.encode()).hexdigest()[:10]
    path_map[sid] = path
    return sid

# ========== 微信引导页 HTML ==========
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
    <title>下载指引</title>
    <style>
        body { font-family: sans-serif; margin: 0; background: #f4f4f7; display: flex; align-items: center; justify-content: center; height: 100vh; }
        #guide { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); color: white; text-align: right; }
        #guide img { width: 80%; margin: 10px; }
        .msg { text-align: center; font-size: 18px; color: #333; }
    </style>
</head>
<body>
    <div id="guide">
        <img src="https://img.alicdn.com/imgextra/i3/O1CN01S9fXfW1WfXfW1WfXf_!!6000000002824-2-tps-450-318.png">
        <div style="padding:20px;text-align:center;font-size:20px;">请点击右上角<br>选择“在浏览器打开”下载</div>
    </div>
    <div class="msg">正在准备下载...</div>
    <script>
        var url = "{{URL}}";
        if (navigator.userAgent.toLowerCase().indexOf('micromessenger') != -1) {
            document.getElementById('guide').style.display = 'block';
        } else {
            window.location.href = url;
        }
    </script>
</body>
</html>
"""

class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/d/'):
            sid = self.path.split('/')[-1]
            path = path_map.get(sid)
            if path:
                dl_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET_NAME}/{path}"
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(HTML_TEMPLATE.replace("{{URL}}", dl_url).encode())
                return
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

# ========== 机器人逻辑 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {'mid': None}
    await update.message.reply_text("你好！直接发送文件即可上传，发送 /list 查看列表。", reply_markup=ReplyKeyboardRemove())

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    uid = update.effective_user.id
    try:
        items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        files = [i for i in items if i['name'] != '.emptyFolderPlaceholder']
        files.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        total_size = sum(int(f.get('metadata', {}).get('size', 0)) for f in files)
        size_str = f"{total_size/(1024*1024):.1f} MB" if total_size > 1024*1024 else f"{total_size/1024:.1f} KB"
        
        text = f"存储统计：{size_str} / 1 GB\n\n文件列表：\n"
        kb = []
        for f in files[page*8 : (page+1)*page_size if 'page_size' in locals() else (page+1)*8]:
            name = f['name']
            kb.append([InlineKeyboardButton(name, callback_data=f"view:{get_id(name)}")])
        
        if len(files) > 8:
            nav = []
            if page > 0: nav.append(InlineKeyboardButton("上一页", callback_data=f"page:{page-1}"))
            if (page+1)*8 < len(files): nav.append(InlineKeyboardButton("下一页", callback_data=f"page:{page+1}"))
            if nav: kb.append(nav)
        
        kb.append([InlineKeyboardButton("刷新列表", callback_data="page:0")])
        
        msg_text = text + ("暂无文件" if not files else "")
        if update.callback_query:
            await update.callback_query.edit_message_text(msg_text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            msg = await update.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(kb))
            user_state[uid]['mid'] = msg.message_id
    except Exception as e: logging.error(e)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("page:"):
        await list_files(update, context, page=int(data[5:]))
    elif data.startswith("view:"):
        await show_detail(update, context, data[5:])
    elif data.startswith("del:"):
        await delete_file(update, context, data[4:])

async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, sid):
    path = path_map.get(sid)
    if not path: return
    
    # 获取文件信息
    items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
    f = next((i for i in items if i['name'] == path), None)
    if not f: return
    
    size_raw = int(f.get('metadata', {}).get('size', 0))
    size = f"{size_raw/(1024*1024):.1f} MB" if size_raw > 1024*1024 else f"{size_raw/1024:.1f} KB"
    time_str = "未知"
    if f.get('created_at'):
        time_str = datetime.fromisoformat(f['created_at'].replace('Z', '+00:00')).astimezone(BJ_TZ).strftime('%Y-%m-%d %H:%M')

    # 引导页链接
    host = os.environ.get("RENDER_EXTERNAL_URL") or f"http://localhost:{os.environ.get('PORT', 8080)}"
    guide_url = f"{host.rstrip('/')}/d/{sid}"
    
    # 生成二维码
    qr = qrcode.make(guide_url)
    buf = BytesIO(); qr.save(buf, format='PNG'); buf.seek(0)
    
    text = (
        f"文件详情\n\n"
        f"文件名：{path}\n"
        f"大小：{size}\n"
        f"上传时间：{time_str}\n\n"
        f"下载链接：{guide_url}\n\n"
        f"微信用户提示：扫码后点击右上角“...”选择“在浏览器打开”即可下载。"
    )
    kb = [[InlineKeyboardButton("删除文件", callback_data=f"del:{sid}")], [InlineKeyboardButton("返回列表", callback_data="page:0")]]
    
    await update.effective_chat.send_photo(photo=buf, caption=text, reply_markup=InlineKeyboardMarkup(kb))

async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE, sid):
    path = path_map.get(sid)
    if path:
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([path])
        await update.callback_query.edit_message_text(f"已删除文件：{path}")
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
        await show_detail(update, context, get_id(name))
        if os.path.exists(f_path): os.remove(f_path)
    except Exception as e: await status_msg.edit_text(f"上传失败：{e}")

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), WebHandler).serve_forever(), daemon=True).start()
    
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO, handle_upload))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()

if __name__ == '__main__': main()
