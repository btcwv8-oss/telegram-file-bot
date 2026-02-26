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

# ========== 状态管理 ==========
user_states = {}
bot_config = {"password": "admin"}

# ========== 极简 Web 服务器 (仅用于 Render 保活) ==========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

# ========== 辅助函数 ==========
def get_file_ext(name):
    return os.path.splitext(name)[1]

async def safe_delete(message):
    try:
        await message.delete()
    except Exception:
        pass

async def send_or_edit(update: Update, text, reply_markup=None, photo=None):
    query = update.callback_query
    if query:
        if photo:
            await safe_delete(query.message)
            return await update.effective_chat.send_photo(photo=photo, caption=text, reply_markup=reply_markup)
        else:
            if query.message.photo:
                await safe_delete(query.message)
                return await update.effective_chat.send_message(text=text, reply_markup=reply_markup)
            else:
                return await query.edit_message_text(text=text, reply_markup=reply_markup)
    else:
        if photo:
            return await update.effective_chat.send_photo(photo=photo, caption=text, reply_markup=reply_markup)
        else:
            return await update.effective_chat.send_message(text=text, reply_markup=reply_markup)

# ========== 机器人功能 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 首页：统一显示所有功能按键
    text = "你好！欢迎使用文件机器人。\n直接发送文件即可上传，或通过下方按键进行管理。"
    kb = [
        [InlineKeyboardButton("查看文件列表", callback_data="p:0:normal")],
        [InlineKeyboardButton("批量删除模式", callback_data="p:0:batch_delete")],
        [InlineKeyboardButton("管理员设置", callback_data="admin_menu")]
    ]
    await send_or_edit(update, text, reply_markup=InlineKeyboardMarkup(kb))

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0, mode="normal"):
    try:
        user_id = update.effective_user.id
        items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        files = [i for i in items if i['name'] != '.emptyFolderPlaceholder']
        files.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        total_size = sum(int(f.get('metadata', {}).get('size', 0)) for f in files)
        size_str = f"{total_size/(1024*1024):.1f} MB" if total_size > 1024*1024 else f"{total_size/1024:.1f} KB"
        
        header = f"存储统计：{size_str} / 1 GB\n\n"
        if mode == "batch_delete":
            selected = user_states.get(user_id, {}).get("selected_files", [])
            text = header + f"批量删除模式（已选 {len(selected)} 个）：\n"
        else:
            text = header + "文件列表：\n"
            
        kb = []
        for f in files[page*8 : (page+1)*8]:
            name = f['name']
            display_name = name
            if mode == "batch_delete":
                is_selected = name in user_states.get(user_id, {}).get("selected_files", [])
                display_name = ("✅ " if is_selected else "⬜️ ") + name
                kb.append([InlineKeyboardButton(display_name, callback_data=f"sel:{name[:50]}:{page}")])
            else:
                kb.append([InlineKeyboardButton(display_name, callback_data=f"v:{name[:50]}")])
        
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("上一页", callback_data=f"p:{page-1}:{mode}"))
        if (page+1)*8 < len(files): nav.append(InlineKeyboardButton("下一页", callback_data=f"p:{page+1}:{mode}"))
        if nav: kb.append(nav)
        
        if mode == "batch_delete":
            kb.append([InlineKeyboardButton("确认删除已选", callback_data="confirm_batch")])
            kb.append([InlineKeyboardButton("返回首页", callback_data="back_home")])
        else:
            kb.append([InlineKeyboardButton("刷新列表", callback_data=f"p:{page}:normal")])
            kb.append([InlineKeyboardButton("返回首页", callback_data="back_home")])
        
        msg_text = text + ("暂无文件" if not files else "")
        await send_or_edit(update, msg_text, reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logging.error(e)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "back_home":
        if user_id in user_states: user_states.pop(user_id)
        await start(update, context)
    elif data.startswith("p:"):
        parts = data.split(":")
        page = int(parts[1])
        mode = parts[2] if len(parts) > 2 else "normal"
        await list_files(update, context, page=page, mode=mode)
    elif data.startswith("v:"):
        await show_detail(update, context, data[2:])
    elif data.startswith("d:"):
        await delete_file(update, context, data[2:])
    elif data.startswith("rn:"):
        await request_rename(update, context, data[3:])
    elif data.startswith("sel:"):
        parts = data.split(":")
        name_part, page = parts[1], int(parts[2])
        if user_id not in user_states: user_states[user_id] = {"selected_files": []}
        items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        f = next((i for i in items if i['name'].startswith(name_part)), None)
        if f:
            full_name = f['name']
            selected = user_states[user_id]["selected_files"]
            if full_name in selected: selected.remove(full_name)
            else: selected.append(full_name)
            await list_files(update, context, page=page, mode="batch_delete")
    elif data == "confirm_batch":
        selected = user_states.get(user_id, {}).get("selected_files", [])
        if not selected: return await query.answer("未选择文件", show_alert=True)
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove(selected)
        user_states.pop(user_id)
        msg = await update.effective_chat.send_message(f"已批量删除 {len(selected)} 个文件")
        await asyncio.sleep(1)
        await msg.delete()
        await start(update, context)
    elif data == "admin_menu":
        kb = [[InlineKeyboardButton("修改管理员密码", callback_data="change_pwd")], [InlineKeyboardButton("返回首页", callback_data="back_home")]]
        await send_or_edit(update, "管理员设置中心", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "change_pwd":
        user_states[user_id] = {"action": "change_password"}
        await send_or_edit(update, "请输入新的管理员密码：")

async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
    f = next((i for i in items if i['name'].startswith(name)), None)
    if not f: return
    
    full_name = f['name']
    size_raw = int(f.get('metadata', {}).get('size', 0))
    size = f"{size_raw/(1024*1024):.1f} MB" if size_raw > 1024*1024 else f"{size_raw/1024:.1f} KB"
    time_str = datetime.fromisoformat(f['created_at'].replace('Z', '+00:00')).astimezone(BJ_TZ).strftime('%Y-%m-%d %H:%M') if f.get('created_at') else "未知"
    long_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET_NAME}/{full_name}"
    
    qr = qrcode.make(long_url)
    buf = BytesIO(); qr.save(buf, format='PNG'); buf.seek(0)
    
    text = f"文件详情\n\n文件名：{full_name}\n大小：{size}\n上传时间：{time_str}\n\n点击下载：[点击此处]({long_url})\n{long_url}"
    kb = [[InlineKeyboardButton("重命名", callback_data=f"rn:{full_name[:50]}")], [InlineKeyboardButton("删除文件", callback_data=f"d:{full_name[:50]}")], [InlineKeyboardButton("返回列表", callback_data="p:0:normal")], [InlineKeyboardButton("返回首页", callback_data="back_home")]]
    await send_or_edit(update, text, reply_markup=InlineKeyboardMarkup(kb), photo=buf)

async def request_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
    f = next((i for i in items if i['name'].startswith(name)), None)
    if not f: return
    user_id, full_name = update.effective_user.id, f['name']
    user_states[user_id] = {"action": "rename", "old_name": full_name}
    await send_or_edit(update, f"请输入新名称（无需后缀，当前：{get_file_ext(full_name)}）：")

async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
    f = next((i for i in items if i['name'].startswith(name)), None)
    if f:
        full_name = f['name']
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([full_name])
        msg = await update.effective_chat.send_message(f"已删除：{full_name}")
        await asyncio.sleep(1)
        await msg.delete()
        await start(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id)
    
    if state:
        action = state.get("action")
        if action == "rename":
            old_name, new_base = state["old_name"], update.message.text.strip()
            new_name = new_base + get_file_ext(old_name)
            try:
                supabase.storage.from_(SUPABASE_BUCKET_NAME).move(old_name, new_name)
                user_states.pop(user_id)
                await update.message.delete()
                await show_detail(update, context, new_name)
            except Exception as e:
                await update.message.reply_text(f"失败：{e}")
                user_states.pop(user_id)
            return
        elif action == "change_password":
            bot_config["password"] = update.message.text.strip()
            user_states.pop(user_id)
            await update.message.delete()
            await start(update, context)
            return

    msg = update.message
    file = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file: return
    name = f"photo_{datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')}.jpg" if msg.photo else getattr(file, 'file_name', f"file_{datetime.now(BJ_TZ).strftime('%H%M%S')}")
    status_msg = await msg.reply_text(f"上传中：{name}...")
    try:
        tg_file = await context.bot.get_file(file.file_id)
        f_path = await tg_file.download_to_drive()
        mtype, _ = mimetypes.guess_type(name)
        if name.endswith('.apk'): mtype = 'application/vnd.android.package-archive'
        with open(f_path, 'rb') as f:
            supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=name, file=f.read(), file_options={'upsert':'true', 'content-type': mtype or 'application/octet-stream'})
        await status_msg.delete()
        await update.message.delete()
        await show_detail(update, context, name)
        if os.path.exists(f_path): os.remove(f_path)
    except Exception as e: await status_msg.edit_text(f"失败：{e}")

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling()

if __name__ == '__main__': main()

# Trigger redeploy: 2026-02-27 16:05
