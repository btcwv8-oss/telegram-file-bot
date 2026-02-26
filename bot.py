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

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
from supabase import create_client, Client

# ========== 配置信息 ==========
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL") # Render 提供的外部访问地址
SUPABASE_BUCKET_NAME = "public-files"
ADMIN_USERNAMES = ["btcwv", "LDvipa"]
DATA_FILE = "bot_data.json"
BJ_TZ = timezone(timedelta(hours=8))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== 数据持久化 ==========
def load_data():
    default = {'password': 'btcwv', 'verified_users': [], 'file_stats': {}}
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k, v in default.items():
                    if k not in data: data[k] = v
                return data
    except Exception: pass
    return default

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

user_data = {} 
callback_map = {} 

def get_short_id(full_text):
    short_id = hashlib.md5(full_text.encode()).hexdigest()[:10]
    callback_map[short_id] = full_text
    return short_id

# ========== 智能引导页 HTML ==========
GUIDE_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>文件下载指引</title>
    <style>
        body { font-family: -apple-system, sans-serif; margin: 0; padding: 0; background: #f4f4f7; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; }
        .container { text-align: center; padding: 20px; }
        .loading { border: 4px solid #f3f3f3; border-top: 4px solid #3498db; border-radius: 50%; width: 40px; height: 40px; animation: spin 2s linear infinite; margin: 0 auto 20px; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        #wechat-guide { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 999; color: white; text-align: right; }
        #wechat-guide img { width: 80%; margin-top: 10px; margin-right: 10px; }
        .tip-text { font-size: 18px; margin-top: 20px; color: #333; }
    </style>
</head>
<body>
    <div id="wechat-guide">
        <img src="https://img.alicdn.com/imgextra/i3/O1CN01S9fXfW1WfXfW1WfXf_!!6000000002824-2-tps-450-318.png" alt="点击右上角">
        <div style="padding: 20px; text-align: center; font-size: 20px; font-weight: bold;">请点击右上角<br>选择“在浏览器打开”下载</div>
    </div>
    <div class="container">
        <div class="loading"></div>
        <div class="tip-text">正在为您准备下载...</div>
    </div>
    <script>
        var downloadUrl = "{{DOWNLOAD_URL}}";
        var ua = navigator.userAgent.toLowerCase();
        var isWechat = ua.indexOf('micromessenger') != -1;
        
        if (isWechat) {
            document.getElementById('wechat-guide').style.display = 'block';
        } else {
            window.location.href = downloadUrl;
            setTimeout(function() {
                window.close();
            }, 5000);
        }
    </script>
</body>
</html>
"""

# ========== 智能 Web 服务器 ==========
class SmartGuideHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        
        if self.path.startswith('/d'):
            short_id = self.path.split('/')[-1].split('?')[0]
            full_path = callback_map.get(short_id)
            if full_path:
                long_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET_NAME}/{full_path}"
                html = GUIDE_PAGE_TEMPLATE.replace("{{DOWNLOAD_URL}}", long_url)
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode('utf-8'))
                return
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args): return

# ========== 核心交互工具 ==========
async def safe_delete(context, chat_id, message_id):
    if not message_id: return
    try: await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception: pass

async def update_view(update, context, text, reply_markup=None, photo=None):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    old_mid = user_data.get(uid, {}).get('mid')
    
    if photo:
        await safe_delete(context, chat_id, old_mid)
        new_msg = await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=reply_markup, parse_mode='Markdown')
        user_data.setdefault(uid, {})['mid'] = new_msg.message_id
    else:
        try:
            if old_mid:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=old_mid, text=text, reply_markup=reply_markup, parse_mode='Markdown')
            else: raise Exception()
        except Exception:
            new_msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
            user_data.setdefault(uid, {})['mid'] = new_msg.message_id

# ========== 辅助函数 ==========
def format_size(size_bytes):
    if not size_bytes: return "0 B"
    try:
        size_bytes = int(size_bytes)
        if size_bytes < 1024: return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
        else: return f"{size_bytes / (1024 * 1024):.1f} MB"
    except: return "未知"

def generate_qr(url):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    qr_img.save(buf, format='PNG')
    buf.seek(0)
    return buf

def get_all_files():
    all_files = []
    def _list_dir(path=""):
        try:
            items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list(path)
            for item in items:
                name = item.get('name')
                if not name or name == '.emptyFolderPlaceholder': continue
                full_path = f"{path}/{name}" if path else name
                if item.get('id') is None: _list_dir(full_path)
                else:
                    item['full_path'] = full_path
                    all_files.append(item)
        except Exception: pass
    _list_dir()
    return all_files

# ========== 业务逻辑 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try: await update.message.delete()
    except Exception: pass
    data = load_data()
    if not (update.effective_user.username and update.effective_user.username.lower().replace('@','') in [a.lower() for a in ADMIN_USERNAMES]) and uid not in data.get('verified_users', []):
        user_data.setdefault(uid, {})['waiting_pwd'] = True
        await update_view(update, context, "🔐 *访问受限*\n\n请输入访问密码：")
        return
    text = "👋 *你好！我是您的私人云端助手*\n\n发送 /list 查看文件列表\n直接发送文件或链接进行上传"
    new_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
    user_data.setdefault(uid, {})['mid'] = new_msg.message_id

async def send_file_list(update, context, page=0):
    real_files = get_all_files()
    total_size = sum(int(f.get('metadata', {}).get('size') or f.get('size', 0)) for f in real_files)
    percent = (total_size / (1024 * 1024 * 1024)) * 100
    storage_info = f"📊 *存储统计*：{format_size(total_size)} / 1 GB ({percent:.1f}%)"
    if not real_files:
        await update_view(update, context, f"{storage_info}\n\n📭 *暂无文件*")
        return
    page_size = 8
    total_pages = (len(real_files) + page_size - 1) // page_size
    page = max(0, min(page, total_pages - 1))
    text = f"{storage_info}\n\n📂 *文件列表*\n━━━━━━━━━━━━━━━"
    kb = []
    real_files.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    for f in real_files[page*page_size : (page+1)*page_size]:
        full_path = f['full_path']
        kb.append([InlineKeyboardButton(full_path[:35], callback_data=f"lk:{get_short_id(full_path)}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"pg:{page-1}"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"pg:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("🔄 刷新列表", callback_data=f"pg:{page}"), InlineKeyboardButton("🧹 批量删除", callback_data="batch_del")])
    await update_view(update, context, text, reply_markup=InlineKeyboardMarkup(kb))

async def show_file_detail(update, context, short_id):
    full_path = callback_map.get(short_id)
    if not full_path:
        await update_view(update, context, "❌ 链接失效，请返回列表刷新")
        return
    data = load_data()
    data['file_stats'][full_path] = data['file_stats'].get(full_path, 0) + 1
    save_data(data)
    try:
        path_parts = full_path.split('/')
        folder = "/".join(path_parts[:-1]) if len(path_parts) > 1 else ""
        filename = path_parts[-1]
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list(folder)
        file_info = next((f for f in files if f['name'] == filename), {})
        size = format_size(file_info.get('metadata', {}).get('size') or file_info.get('size', 0))
        created = file_info.get('created_at', '')
        created_str = datetime.fromisoformat(created.replace('Z', '+00:00')).astimezone(BJ_TZ).strftime('%Y-%m-%d %H:%M') if created else "未知"
        
        # 智能引导页链接
        base_url = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else f"http://localhost:{os.environ.get('PORT', 8080)}"
        guide_url = f"{base_url}/d/{short_id}"
        qr = generate_qr(guide_url)
        
        text = (
            f"✅ *文件详情*\n\n"
            f"📄 文件名：`{full_path}`\n"
            f"⚖️ 大小：`{size}`\n"
            f"📅 上传时间：`{created_str}`\n"
            f"📈 下载次数：`{data['file_stats'][full_path]}` 次\n\n"
            f"🔗 [点击下载]({guide_url})\n\n"
            f"链接：`{guide_url}`\n\n"
            f"💡 *微信用户提示*：\n扫码后请点击屏幕右上角的 **“三个点(...)”** 图标，选择 **“在浏览器打开”** 即可开始下载。"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ 删除", callback_data=f"cd:{short_id}"), InlineKeyboardButton("🔙 返回列表", callback_data="list_files")]
        ])
        await update_view(update, context, text, reply_markup=kb, photo=qr)
    except Exception as e: await update_view(update, context, f"❌ 获取详情失败: {e}")

async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_obj = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file_obj: return
    name = getattr(file_obj, 'file_name', None) or f"img_{datetime.now(BJ_TZ).strftime('%H%M%S')}.jpg"
    try: await msg.delete()
    except Exception: pass
    await update_view(update, context, f"⏳ *正在上传*：`{name}`...")
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        path = await tg_file.download_to_drive()
        mime_type, _ = mimetypes.guess_type(name)
        if name.endswith('.apk'): mime_type = 'application/vnd.android.package-archive'
        elif not mime_type: mime_type = 'application/octet-stream'
        with open(path, 'rb') as f: content = f.read()
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=name, file=content, file_options={'upsert': 'true', 'content-type': mime_type})
        await show_file_detail(update, context, get_short_id(name))
        if os.path.exists(path): os.remove(path)
    except Exception as e: await update_view(update, context, f"❌ 失败: {e}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == 'list_files': await send_file_list(update, context)
    elif data.startswith('pg:'): await send_file_list(update, context, page=int(data[3:]))
    elif data.startswith('lk:'): await show_file_detail(update, context, data[3:])
    elif data.startswith('cd:'):
        full_path = callback_map.get(data[3:])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ 确认删除", callback_data=f"yd:{data[3:]}"), InlineKeyboardButton("❌ 取消", callback_data="list_files")]])
        await update_view(update, context, f"⚠️ *确认删除？*\n`{full_path}`", reply_markup=kb)
    elif data.startswith('yd:'):
        full_path = callback_map.get(data[3:])
        if full_path: supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([full_path])
        await send_file_list(update, context)
    elif data == 'batch_del':
        real_files = get_all_files()
        real_files.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        kb = [[InlineKeyboardButton(f"🗑 {f['full_path'][:30]}", callback_data=f"bs:{get_short_id(f['full_path'])}")] for f in real_files[:10]]
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="list_files")])
        await update_view(update, context, "🧹 *批量删除模式*", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith('bs:'):
        full_path = callback_map.get(data[3:])
        if full_path: supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([full_path])
        await query.edit_message_text("✅ 已删除")
        await asyncio.sleep(1)
        await send_file_list(update, context)

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), SmartGuideHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", lambda u, c: send_file_list(u, c)))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO, handle_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: start(u, c))) # 简化处理
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__': main()
