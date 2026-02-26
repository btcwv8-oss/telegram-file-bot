import os
import logging
import asyncio
import qrcode
import threading
import mimetypes
import urllib.parse
import json
import base64
from io import BytesIO
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from supabase import create_client, Client

# ========== 配置 ==========
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_BUCKET_NAME = "public-files"
RENDER_EXTERNAL_URL = "https://telegram-file-bot-free.onrender.com"
BJ_TZ = timezone(timedelta(hours=8))

logging.basicConfig(level=logging.INFO)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== 状态与持久化配置 ==========
user_states = {} # 存放临时 action
DEFAULT_PWD = "btcwv"
CONFIG_FILE = ".bot_config.json"
AUTH_FILE = ".auth_users.json"

def get_remote_data(filename, default_val):
    try:
        res = supabase.storage.from_(SUPABASE_BUCKET_NAME).download(filename)
        return json.loads(res)
    except:
        return default_val

def save_remote_data(filename, data):
    try:
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(
            path=filename,
            file=json.dumps(data).encode(),
            file_options={"upsert": "true", "content-type": "application/json"}
        )
    except Exception as e:
        logging.error(f"Save data error for {filename}: {e}")

# 初始加载
bot_config = get_remote_data(CONFIG_FILE, {"password": DEFAULT_PWD})
auth_users = get_remote_data(AUTH_FILE, []) # 存储已验证的 user_id 列表

# ========== 微信中转引导页 HTML ==========
SUPABASE_BASE_URL = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET_NAME}"

GUIDE_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>资源查看</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; text-align: center; padding-top: 50px; color: #333; background: #f5f5f5; }}
        .weixin-tip {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); color: #fff; z-index: 999; }}
        .weixin-tip img {{ width: 100%; max-width: 250px; position: absolute; right: 20px; top: 10px; }}
        .card {{ background: #fff; margin: 20px; padding: 30px; border-radius: 15px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .btn {{ display: inline-block; padding: 15px 30px; background: #0088cc; color: #fff; text-decoration: none; border-radius: 10px; font-weight: bold; margin-top: 20px; }}
        .footer {{ font-size: 12px; color: #999; margin-top: 50px; }}
    </style>
</head>
<body>
    <div id="weixinTip" class="weixin-tip">
        <p style="margin-top: 120px; font-size: 20px; font-weight: bold; line-height: 1.6;">资源受限<br>请点击右上角 <span style="font-size: 24px;">•••</span><br>选择“在浏览器中打开”</p>
        <img src="https://img.alicdn.com/tfs/TB19S_4QXXXXXbSXXXXXXXXXXXX-1125-1125.png" alt="引导图">
    </div>
    <div class="card" id="normalView">
        <h2 style="margin-bottom: 10px;">资源已就绪</h2>
        <p id="fileName" style="word-break: break-all; color: #666;"></p>
        <a id="downloadBtn" class="btn" href="#" download>立即获取资源</a>
        <p style="font-size: 13px; color: #ff4d4f; margin-top: 15px;">若未自动弹出，请点击上方按钮</p>
    </div>
    <div class="footer">Powered by Resource Assistant</div>
    <script>
        var baseUrl = "{base_url}";
        function getParam(name) {{
            return new URLSearchParams(window.location.search).get(name);
        }}
        try {{
            var encodedName = getParam('s');
            if (encodedName) {{
                var name = atob(encodedName);
                var url = baseUrl + "/" + encodeURIComponent(name);
                var btn = document.getElementById('downloadBtn');
                btn.href = url;
                btn.setAttribute('download', name);
                document.getElementById('fileName').innerText = name;
                var ua = navigator.userAgent.toLowerCase();
                if (ua.match(/MicroMessenger/i) == "micromessenger") {{
                    document.getElementById('weixinTip').style.display = 'block';
                }} else {{
                    setTimeout(function(){{ window.location.href = url; }}, 800);
                }}
            }}
        }} catch(e) {{ console.error("Parse error"); }}
    </script>
</body>
</html>
"""

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/v/s"):
            self.send_response(200); self.send_header("Content-type", "text/html"); self.end_headers()
            html = GUIDE_HTML_TEMPLATE.replace("{base_url}", SUPABASE_BASE_URL)
            self.wfile.write(html.encode())
        else:
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

# ========== 工具 ==========
async def safe_delete(message):
    try: await message.delete()
    except: pass

async def send_or_edit(update: Update, text, reply_markup=None, photo=None):
    query = update.callback_query
    if update.message: await safe_delete(update.message)
    if query:
        if photo:
            await safe_delete(query.message)
            return await update.effective_chat.send_photo(photo=photo, caption=text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            if query.message.photo:
                await safe_delete(query.message)
                return await update.effective_chat.send_message(text=text, reply_markup=reply_markup, parse_mode='Markdown', disable_web_page_preview=True)
            else:
                return await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='Markdown', disable_web_page_preview=True)
    else:
        if photo: return await update.effective_chat.send_photo(photo=photo, caption=text, reply_markup=reply_markup, parse_mode='Markdown')
        else: return await update.effective_chat.send_message(text=text, reply_markup=reply_markup, parse_mode='Markdown', disable_web_page_preview=True)

def find_full_name(prefix):
    try:
        items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        for i in items:
            if i['name'].startswith(prefix): return i['name']
    except: pass
    return None

# ========== 身份验证装饰器 ==========
def check_auth(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        global auth_users
        if user_id not in auth_users:
            # 实时从云端拉取一次，防止多实例同步问题
            auth_users = get_remote_data(AUTH_FILE, [])
            if user_id not in auth_users:
                await send_or_edit(update, "*请发送访问密码以继续*")
                return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ========== 界面 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    global auth_users
    if user_id not in auth_users:
        auth_users = get_remote_data(AUTH_FILE, [])
        if user_id not in auth_users:
            await send_or_edit(update, "*请发送访问密码以继续*")
            return

    user_states[user_id] = {} # 清空 action
    kb = [
        [InlineKeyboardButton("文件列表", callback_data="p:0:normal")],
        [InlineKeyboardButton("批量删除", callback_data="p:0:batch_delete")],
        [InlineKeyboardButton("设置", callback_data="admin_menu")]
    ]
    await send_or_edit(update, "*文件助手*", reply_markup=InlineKeyboardMarkup(kb))

@check_auth
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0, mode="normal"):
    try:
        user_id = update.effective_user.id
        items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        files = [i for i in items if i['name'] not in ['.emptyFolderPlaceholder', CONFIG_FILE, AUTH_FILE]]
        files.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        if "selected" not in user_states[user_id]: user_states[user_id]["selected"] = set()
        selected = user_states[user_id]["selected"]

        title = "*批量删除*" if mode == "batch_delete" else "*文件列表*"
        if mode == "batch_delete": title += f" ({len(selected)})"
            
        kb = []
        for f in files[page*8 : (page+1)*8]:
            name = f['name']; prefix = name[:40]
            if mode == "batch_delete":
                mark = "✅ " if name in selected else "⬜️ "
                kb.append([InlineKeyboardButton(f"{mark}{name}", callback_data=f"sel:{prefix}:{page}")])
            else:
                kb.append([InlineKeyboardButton(name, callback_data=f"v:{prefix}")])
        
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"p:{page-1}:{mode}"))
        if (page+1)*8 < len(files): nav.append(InlineKeyboardButton("➡️", callback_data=f"p:{page+1}:{mode}"))
        if nav: kb.append(nav)
        
        if mode == "batch_delete":
            kb.append([InlineKeyboardButton("确认删除", callback_data="confirm_batch"), InlineKeyboardButton("返回", callback_data="back_home")])
        else:
            kb.append([InlineKeyboardButton("返回首页", callback_data="back_home")])
        
        await send_or_edit(update, title, reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logging.error(e)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data; user_id = update.effective_user.id
    if data == "back_home": await start(update, context); return
    
    global auth_users
    if user_id not in auth_users:
        auth_users = get_remote_data(AUTH_FILE, [])
        if user_id not in auth_users:
            await send_or_edit(update, "*会话已过期，请重新输入密码*")
            return

    if data.startswith("p:"):
        parts = data.split(":"); await list_files(update, context, page=int(parts[1]), mode=parts[2])
    elif data.startswith("v:"):
        name = find_full_name(data[2:]); 
        if name: await show_detail(update, context, name)
    elif data.startswith("d:"):
        name = find_full_name(data[2:]);
        if name:
            supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([name])
            await list_files(update, context)
    elif data.startswith("rn:"):
        name = find_full_name(data[3:]);
        if name:
            user_states[user_id]["action"] = "rename"; user_states[user_id]["old_name"] = name
            await send_or_edit(update, f"新名称 (原后缀 {os.path.splitext(name)[1]}):")
    elif data.startswith("sel:"):
        parts = data.split(":"); name = find_full_name(parts[1])
        if name:
            if "selected" not in user_states[user_id]: user_states[user_id]["selected"] = set()
            s = user_states[user_id]["selected"]
            if name in s: s.remove(name)
            else: s.add(name)
            await list_files(update, context, page=int(parts[2]), mode="batch_delete")
    elif data == "confirm_batch":
        s = list(user_states.get(user_id, {}).get("selected", []))
        if s: supabase.storage.from_(SUPABASE_BUCKET_NAME).remove(s)
        user_states[user_id].pop("selected", None); await start(update, context)
    elif data == "admin_menu":
        kb = [[InlineKeyboardButton("修改密码", callback_data="change_pwd")], [InlineKeyboardButton("退出登录", callback_data="logout")], [InlineKeyboardButton("返回", callback_data="back_home")]]
        await send_or_edit(update, "*设置*", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "change_pwd":
        user_states[user_id]["action"] = "pwd"; await send_or_edit(update, "输入新密码:")
    elif data == "logout":
        if user_id in auth_users:
            auth_users.remove(user_id)
            save_remote_data(AUTH_FILE, auth_users)
        await send_or_edit(update, "*已退出登录*")

async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    try:
        encoded_name = base64.b64encode(name.encode()).decode()
        dl_url = f"{RENDER_EXTERNAL_URL}/v/s?s={encoded_name}"
        qr = qrcode.make(dl_url); buf = BytesIO(); qr.save(buf, format='PNG'); buf.seek(0)
        text = f"`{name}`\n\n🔗 [点击下载]({dl_url})\n\n`{dl_url}`"
        prefix = name[:40]
        kb = [
            [InlineKeyboardButton("重命名", callback_data=f"rn:{prefix}"), InlineKeyboardButton("删除", callback_data=f"d:{prefix}")],
            [InlineKeyboardButton("返回列表", callback_data="p:0:normal")]
        ]
        await send_or_edit(update, text, reply_markup=InlineKeyboardMarkup(kb), photo=buf)
    except Exception as e: logging.error(e)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; msg = update.message
    global auth_users
    
    # 1. 验证逻辑
    if user_id not in auth_users:
        config = get_remote_data(CONFIG_FILE, {"password": DEFAULT_PWD})
        if msg.text and msg.text.strip() == config.get("password", DEFAULT_PWD):
            auth_users.append(user_id)
            save_remote_data(AUTH_FILE, auth_users)
            await start(update, context)
        else: await send_or_edit(update, "*密码错误，请重新输入*")
        return

    # 2. 处理 action
    state = user_states.get(user_id, {})
    if "action" in state:
        if state["action"] == "rename":
            new = msg.text.strip() + os.path.splitext(state["old_name"])[1]
            try: supabase.storage.from_(SUPABASE_BUCKET_NAME).move(state["old_name"], new); await show_detail(update, context, new)
            except: pass
        elif state["action"] == "pwd":
            new_pwd = msg.text.strip()
            save_remote_data(CONFIG_FILE, {"password": new_pwd})
            await start(update, context)
        state.pop("action", None); await safe_delete(msg); return
    
    # 3. 上传
    file = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file: await safe_delete(msg); return
        
    name = f"photo_{datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')}.jpg" if msg.photo else getattr(file, 'file_name', 'file')
    try:
        tg_file = await context.bot.get_file(file.file_id); f_path = await tg_file.download_to_drive()
        mtype, _ = mimetypes.guess_type(name)
        if name.lower().endswith('.apk'): mtype = 'application/vnd.android.package-archive'
        with open(f_path, 'rb') as f:
            supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=name, file=f.read(), file_options={'upsert':'true', 'content-type': mtype or 'application/octet-stream'})
        await safe_delete(msg); await show_detail(update, context, name)
        if os.path.exists(f_path): os.remove(f_path)
    except: pass

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CallbackQueryHandler(handle_callback)); app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling()

if __name__ == '__main__': main()
