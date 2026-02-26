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
# 模拟密码存储（实际应用中建议存入数据库或环境变量）
# 初始密码设为 123456
bot_config = {"password": "admin"}

# ========== 极简 Web 服务器 (仅用于 Render 保活) ==========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

# ========== 辅助函数 ==========
def get_file_ext(name):
    return os.path.splitext(name)[1]

# ========== 机器人逻辑 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！直接发送文件即可上传，发送 /list 查看列表。", reply_markup=ReplyKeyboardRemove())

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
            kb.append([InlineKeyboardButton("取消批量模式", callback_data="p:0:normal")])
        else:
            kb.append([InlineKeyboardButton("批量删除", callback_data="p:0:batch_delete")])
            kb.append([InlineKeyboardButton("管理设置", callback_data="admin_menu")])
            kb.append([InlineKeyboardButton("刷新列表", callback_data="p:0:normal")])
        
        msg_text = text + ("暂无文件" if not files else "")
        if update.callback_query:
            if update.callback_query.message.photo:
                await update.effective_chat.send_message(msg_text, reply_markup=InlineKeyboardMarkup(kb))
                await update.callback_query.message.delete()
            else:
                await update.callback_query.edit_message_text(msg_text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logging.error(e)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("p:"):
        parts = data.split(":")
        page = int(parts[1])
        mode = parts[2] if len(parts) > 2 else "normal"
        if mode == "normal" and user_id in user_states:
            user_states.pop(user_id)
        await list_files(update, context, page=page, mode=mode)
        
    elif data.startswith("v:"):
        await show_detail(update, context, data[2:])
        
    elif data.startswith("d:"):
        await delete_file(update, context, data[2:])
        
    elif data.startswith("rn:"):
        await request_rename(update, context, data[3:])
        
    elif data.startswith("sel:"):
        parts = data.split(":")
        name_part = parts[1]
        page = int(parts[2])
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
        if not selected:
            await query.answer("未选择任何文件", show_alert=True)
            return
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove(selected)
        user_states.pop(user_id)
        await query.edit_message_text(f"已批量删除 {len(selected)} 个文件")
        await asyncio.sleep(1)
        await list_files(update, context)

    elif data == "admin_menu":
        kb = [
            [InlineKeyboardButton("修改管理员密码", callback_data="change_pwd")],
            [InlineKeyboardButton("返回文件列表", callback_data="p:0:normal")]
        ]
        await query.edit_message_text("管理员设置中心", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "change_pwd":
        user_states[user_id] = {"action": "change_password"}
        await query.message.delete()
        await update.effective_chat.send_message("请输入新的管理员密码：")

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
    
    text = (
        f"文件详情\n\n"
        f"文件名：{full_name}\n"
        f"大小：{size}\n"
        f"上传时间：{time_str}\n\n"
        f"点击下载：[点击此处]({long_url})\n"
        f"{long_url}"
    )
    kb = [
        [InlineKeyboardButton("重命名", callback_data=f"rn:{full_name[:50]}")],
        [InlineKeyboardButton("删除文件", callback_data=f"d:{full_name[:50]}")],
        [InlineKeyboardButton("返回列表", callback_data="p:0:normal")]
    ]
    
    await update.effective_chat.send_photo(photo=buf, caption=text, reply_markup=InlineKeyboardMarkup(kb))
    if update.callback_query:
        await update.callback_query.message.delete()

async def request_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
    f = next((i for i in items if i['name'].startswith(name)), None)
    if not f: return
    
    user_id = update.effective_user.id
    full_name = f['name']
    user_states[user_id] = {"action": "rename", "old_name": full_name}
    
    msg = f"请输入文件的新名称（无需输入后缀，当前后缀：{get_file_ext(full_name)}）："
    if update.callback_query:
        await update.callback_query.message.delete()
    await update.effective_chat.send_message(msg)

async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE, name):
    items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
    f = next((i for i in items if i['name'].startswith(name)), None)
    if f:
        full_name = f['name']
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([full_name])
        if update.callback_query.message.photo:
            await update.callback_query.message.delete()
            await update.effective_chat.send_message(f"已删除文件：{full_name}")
        else:
            await update.callback_query.edit_message_text(f"已删除文件：{full_name}")
        
        await asyncio.sleep(1)
        await list_files(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id)
    
    if state:
        action = state.get("action")
        # 处理重命名逻辑
        if action == "rename":
            old_name = state["old_name"]
            new_base_name = update.message.text.strip()
            ext = get_file_ext(old_name)
            new_name = new_base_name + ext
            try:
                supabase.storage.from_(SUPABASE_BUCKET_NAME).move(old_name, new_name)
                user_states.pop(user_id)
                await update.message.reply_text(f"重命名成功：{new_name}")
                await show_detail(update, context, new_name)
            except Exception as e:
                await update.message.reply_text(f"重命名失败：{e}")
                user_states.pop(user_id)
            return
        
        # 处理修改密码逻辑
        elif action == "change_password":
            new_pwd = update.message.text.strip()
            bot_config["password"] = new_pwd
            user_states.pop(user_id)
            await update.message.reply_text(f"管理员密码已成功修改为：{new_pwd}")
            await list_files(update, context)
            return

    # 处理文件上传
    msg = update.message
    file = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file: return
    
    if msg.photo:
        name = f"photo_{datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')}.jpg"
    else:
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
    except Exception as e: 
        logging.error(f"Upload error: {e}")
        await status_msg.edit_text(f"上传失败：{e}")

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
