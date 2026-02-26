import os
import logging
import asyncio
import qrcode
import threading
import mimetypes
import urllib.parse
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
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "") # 在 Render 环境变量中设置，例如 https://your-app.onrender.com
BJ_TZ = timezone(timedelta(hours=8))

logging.basicConfig(level=logging.INFO)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== 状态与配置 ==========
user_states = {}
bot_config = {"password": os.environ.get("BOT_PASSWORD", "admin")}

# ========== 微信中转引导页 HTML ==========
GUIDE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>文件下载</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; text-align: center; padding-top: 50px; color: #333; }}
        .weixin-tip {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); color: #fff; z-index: 999; }}
        .weixin-tip img {{ width: 100%; max-width: 300px; position: absolute; right: 20px; top: 10px; }}
        .btn {{ display: inline-block; padding: 12px 24px; background: #0088cc; color: #fff; text-decoration: none; border-radius: 8px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div id="weixinTip" class="weixin-tip">
        <p style="margin-top: 100px; font-size: 18px;">微信内无法直接下载<br>请点击右上角 •••<br>选择“在浏览器中打开”</p>
        <img src="https://img.alicdn.com/tfs/TB19S_4QXXXXXbSXXXXXXXXXXXX-1125-1125.png" alt="引导图">
    </div>
    <div id="normalView">
        <h3>正在准备下载...</h3>
        <p id="fileName"></p>
        <a id="downloadBtn" class="btn" href="#">手动点击下载</a>
    </div>
    <script>
        var url = new URLSearchParams(window.location.search).get('url');
        var name = new URLSearchParams(window.location.search).get('name');
        if (url) {{
            document.getElementById('downloadBtn').href = url;
            document.getElementById('fileName').innerText = name || '文件准备就绪';
            var ua = navigator.userAgent.toLowerCase();
            if (ua.match(/MicroMessenger/i) == "micromessenger") {{
                document.getElementById('weixinTip').style.display = 'block';
            }} else {{
                window.location.href = url; // 浏览器环境下自动跳转下载
            }}
        }}
    </script>
</body>
</html>
"""

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/dl"):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(GUIDE_HTML.encode())
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
        if not user_states.get(user_id, {}).get("auth"):
            await send_or_edit(update, "*请发送访问密码以继续*")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ========== 界面 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_states:
        auth_status = user_states[user_id].get("auth", False)
        user_states[user_id] = {"auth": auth_status}
    else:
        user_states[user_id] = {"auth": False}
        
    if not user_states[user_id]["auth"]:
        await send_or_edit(update, "*请发送访问密码以继续*")
        return

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
        files = [i for i in items if i['name'] != '.emptyFolderPlaceholder']
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
    if not user_states.get(user_id, {}).get("auth"):
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
        kb = [[InlineKeyboardButton("修改密码", callback_data="change_pwd")], [InlineKeyboardButton("返回", callback_data="back_home")]]
        await send_or_edit(update, "*设置*", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "change_pwd":
        user_states[user_id]["action"] = "pwd"; await send_or_edit(update, "输入新密码:")

async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    try:
        raw_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET_NAME}/{name}"
        # 构建中转页链接
        if RENDER_EXTERNAL_URL:
            dl_url = f"{RENDER_EXTERNAL_URL}/dl?name={urllib.parse.quote(name)}&url={urllib.parse.quote(raw_url)}"
        else:
            dl_url = raw_url
            
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
    if user_id not in user_states: user_states[user_id] = {"auth": False}
    state = user_states[user_id]
    
    if not state.get("auth"):
        if msg.text and msg.text.strip() == bot_config["password"]:
            state["auth"] = True; await start(update, context)
        else: await send_or_edit(update, "*密码错误，请重新输入*")
        return

    if "action" in state:
        if state["action"] == "rename":
            new = msg.text.strip() + os.path.splitext(state["old_name"])[1]
            try: supabase.storage.from_(SUPABASE_BUCKET_NAME).move(state["old_name"], new); await show_detail(update, context, new)
            except: pass
        elif state["action"] == "pwd":
            bot_config["password"] = msg.text.strip(); await start(update, context)
        state.pop("action", None); await safe_delete(msg); return
    
    file = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file: await safe_delete(msg); return
        
    name = f"photo_{datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')}.jpg" if msg.photo else getattr(file, 'file_name', 'file')
    try:
        tg_file = await context.bot.get_file(file.file_id); f_path = await tg_file.download_to_drive()
        mtype, _ = mimetypes.guess_type(name)
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
