import os
import logging
import asyncio
import qrcode
import threading
import mimetypes
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
BJ_TZ = timezone(timedelta(hours=8))

logging.basicConfig(level=logging.INFO)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== 状态与配置 ==========
# user_states[user_id] = {"auth": bool, "action": str, "old_name": str, "selected": set()}
user_states = {}
bot_config = {"password": os.environ.get("BOT_PASSWORD", "admin")}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
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
    # 保持 auth 状态，只清空其他 action
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
    
    # 基础路由（无需 Auth 的只有返回首页，但首页本身会检查 Auth）
    if data == "back_home": await start(update, context); return
    
    # 其他所有 Callback 检查 Auth
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
        url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET_NAME}/{name}"
        qr = qrcode.make(url); buf = BytesIO(); qr.save(buf, format='PNG'); buf.seek(0)
        # 增加等宽代码块包裹的原始链接，方便一键点击复制
        text = f"`{name}`\n\n🔗 [点击下载]({url})\n\n`{url}`"
        prefix = name[:40]
        kb = [
            [InlineKeyboardButton("重命名", callback_data=f"rn:{prefix}"), InlineKeyboardButton("删除", callback_data=f"d:{prefix}")],
            [InlineKeyboardButton("返回列表", callback_data="p:0:normal")]
        ]
        await send_or_edit(update, text, reply_markup=InlineKeyboardMarkup(kb), photo=buf)
    except: pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; msg = update.message
    if user_id not in user_states: user_states[user_id] = {"auth": False}
    state = user_states[user_id]
    
    # 1. 优先处理验证逻辑
    if not state.get("auth"):
        if msg.text and msg.text.strip() == bot_config["password"]:
            state["auth"] = True
            await start(update, context)
        else:
            await send_or_edit(update, "*密码错误，请重新输入*")
        return

    # 2. 已验证，处理正在进行的动作
    if "action" in state:
        if state["action"] == "rename":
            new = msg.text.strip() + os.path.splitext(state["old_name"])[1]
            try: supabase.storage.from_(SUPABASE_BUCKET_NAME).move(state["old_name"], new); await show_detail(update, context, new)
            except: pass
        elif state["action"] == "pwd":
            bot_config["password"] = msg.text.strip()
            await start(update, context)
        state.pop("action", None); await safe_delete(msg); return
    
    # 3. 处理上传
    file = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file: 
        await safe_delete(msg); return
        
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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling()

if __name__ == '__main__': main()
