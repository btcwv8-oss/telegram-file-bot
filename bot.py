import os
import json
import logging
import asyncio
import qrcode
import hashlib
import threading
from io import BytesIO
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    ReplyKeyboardMarkup,
    KeyboardButton
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
SUPABASE_BUCKET_NAME = "public-files"
ADMIN_USERNAMES = ["btcwv", "LDvipa"]
DATA_FILE = "bot_data.json"
BJ_TZ = timezone(timedelta(hours=8))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== 数据持久化 ==========
def load_data():
    default = {
        'password': 'btcwv', 
        'verified_users': [], 
        'file_stats': {}, # {file_name: download_count}
        'folders': {}     # {file_name: folder_name}
    }
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 确保所有键都存在
                for k, v in default.items():
                    if k not in data: data[k] = v
                return data
    except Exception: pass
    return default

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# 内存缓存
user_data = {} 
callback_map = {} 

def get_short_id(full_text):
    short_id = hashlib.md5(full_text.encode()).hexdigest()[:10]
    callback_map[short_id] = full_text
    return short_id

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
            else:
                raise Exception()
        except Exception:
            new_msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
            user_data.setdefault(uid, {})['mid'] = new_msg.message_id

# ========== 权限系统 ==========
def is_verified(uid, user):
    if user.username and user.username.lower().replace('@','') in [a.lower() for a in ADMIN_USERNAMES]:
        return True
    return uid in load_data().get('verified_users', [])

def verify_user(uid):
    data = load_data()
    if uid not in data['verified_users']:
        data['verified_users'].append(uid)
        save_data(data)

# ========== 辅助函数 ==========
def get_file_icon(name):
    ext = name.split('.')[-1].lower() if '.' in name else ''
    if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']: return "🖼️"
    if ext in ['mp4', 'mov', 'avi', 'mkv']: return "🎬"
    if ext in ['mp3', 'wav', 'flac']: return "🎵"
    if ext in ['pdf', 'doc', 'docx', 'txt']: return "📄"
    if ext in ['zip', 'rar', '7z']: return "📦"
    if ext in ['apk', 'exe']: return "⚙️"
    return "📁"

def format_size(size_bytes):
    if not size_bytes: return "0 B"
    if size_bytes < 1024: return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
    else: return f"{size_bytes / (1024 * 1024):.1f} MB"

def generate_qr(url):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    qr_img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# ========== 业务逻辑 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try: await update.message.delete()
    except Exception: pass

    if not is_verified(uid, update.effective_user):
        user_data.setdefault(uid, {})['waiting_pwd'] = True
        await update_view(update, context, "🔐 *访问受限*\n\n请输入访问密码：")
        return

    text = "👋 *你好！我是文件助手*\n\n请选择操作或直接发送文件上传 👇"
    kb = ReplyKeyboardMarkup([
        [KeyboardButton("📂 文件列表"), KeyboardButton("📤 上传文件")],
        [KeyboardButton("🔍 搜索文件"), KeyboardButton("ℹ️ 帮助")]
    ], resize_keyboard=True)
    new_msg = await update.message.reply_text(text, reply_markup=kb, parse_mode='Markdown')
    user_data.setdefault(uid, {})['mid'] = new_msg.message_id

async def set_pwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    if not (user.username and user.username.lower().replace('@','') in [a.lower() for a in ADMIN_USERNAMES]):
        await update.message.reply_text("❌ 只有管理员可以修改密码")
        return
    
    if not context.args:
        await update.message.reply_text("📝 使用方法：`/setpwd 新密码`")
        return
    
    new_pwd = context.args[0]
    data = load_data()
    data['password'] = new_pwd
    save_data(data)
    await update.message.reply_text(f"✅ 密码已修改为：`{new_pwd}`")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    try: await update.message.delete()
    except Exception: pass

    data = load_data()
    if user_data.get(uid, {}).get('waiting_pwd'):
        if text == data.get('password'):
            verify_user(uid)
            user_data[uid]['waiting_pwd'] = False
            await start(update, context)
        else:
            await update_view(update, context, "❌ *密码错误*\n\n请重新输入：")
        return

    if user_data.get(uid, {}).get('waiting_rename'):
        await do_rename(update, context, text)
        return
    


    if not is_verified(uid, update.effective_user): return

    if text == "📂 文件列表": await send_file_list(update, context)
    elif text == "📤 上传文件": await update_view(update, context, "📤 *请直接发送文件/图片/视频给我*")
    elif text == "🔍 搜索文件": await update_view(update, context, "🔍 *请输入关键词搜索*\n例如：直接发送文件名关键词")
    elif text == "ℹ️ 帮助": 
        help_text = (
            "📖 *使用说明*\n\n"
            "1️⃣ *上传*：直接发送任何文件、图片或视频。\n"
            "2️⃣ *管理*：点击“文件列表”查看、重命名或删除文件。\n"
            "3️⃣ *分享*：点击文件可获取下载链接和二维码。\n"
            "4️⃣ *统计*：列表顶部实时显示存储占用情况。\n"
            "5️⃣ *安全*：支持为单个文件设置提取码。\n\n"
            "👤 *管理员指令*：\n"
            "`/setpwd [新密码]` - 修改访问密码"
        )
        await update_view(update, context, help_text)
    else:
        # 默认作为搜索处理
        await send_file_list(update, context, search_query=text)

async def send_file_list(update, context, page=0, search_query=None):
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        real_files = [f for f in files if f.get('name') != '.emptyFolderPlaceholder']
        
        if search_query:
            real_files = [f for f in real_files if search_query.lower() in f['name'].lower()]

        total_size = sum(f.get('metadata', {}).get('size', 0) for f in real_files)
        storage_info = f"📊 *存储统计*：{format_size(total_size)} / 1 GB"

        if not real_files:
            await update_view(update, context, f"{storage_info}\n\n📭 *暂无匹配文件*")
            return

        page_size = 6
        total_pages = (len(real_files) + page_size - 1) // page_size
        page = max(0, min(page, total_pages - 1))
        
        text = f"{storage_info}\n\n📂 *文件列表* ({len(real_files)}个)\n━━━━━━━━━━━━━━━"
        kb = []
        for f in real_files[page*page_size : (page+1)*page_size]:
            name = f['name']
            icon = get_file_icon(name)
            kb.append([InlineKeyboardButton(f"{icon} {name[:25]}", callback_data=f"lk:{get_short_id(name)}")])

        nav = []
        if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"pg:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1: nav.append(InlineKeyboardButton("➡️", callback_data=f"pg:{page+1}"))
        kb.append(nav)
        kb.append([InlineKeyboardButton("🔄 刷新", callback_data=f"pg:{page}"), InlineKeyboardButton("🧹 批量删除", callback_data="batch_del")])

        await update_view(update, context, text, reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logging.error(f"List error: {e}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == 'list_files': await send_file_list(update, context)
    elif data.startswith('pg:'): await send_file_list(update, context, page=int(data[3:]))
    elif data.startswith('lk:'): await show_file_detail(update, context, data[3:])
    elif data.startswith('cd:'): await confirm_delete(update, context, data[3:])
    elif data.startswith('yd:'): await do_delete(update, context, data[3:])
    elif data.startswith('rn:'): await start_rename(update, context, data[3:])

    elif data.startswith('ts:'): await get_temp_link(update, context, data[3:])
    elif data == 'batch_del': await send_batch_del(update, context)
    elif data.startswith('bs:'): await do_batch_del_single(update, context, data[3:])

async def show_file_detail(update, context, short_id):
    name = callback_map.get(short_id)
    if not name:
        await update_view(update, context, "❌ 链接失效，请返回列表刷新")
        return
    
    data = load_data()
    # 增加下载统计
    data['file_stats'][name] = data['file_stats'].get(name, 0) + 1
    save_data(data)
    
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        file_info = next((f for f in files if f['name'] == name), {})
        size = format_size(file_info.get('metadata', {}).get('size', 0))
        created = file_info.get('created_at', '')
        if created:
            dt = datetime.fromisoformat(created.replace('Z', '+00:00')).astimezone(BJ_TZ)
            created_str = dt.strftime('%Y-%m-%d %H:%M')
        else: created_str = "未知"

        res = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(name)
        url = res if isinstance(res, str) else res.get('publicURL', res)
        qr = generate_qr(url)
        
        count = data['file_stats'].get(name, 0)
        
        text = (
            f"✅ *文件详情*\n\n"
            f"📄 *文件名*：`{name}`\n"
            f"⚖️ *大小*：`{size}`\n"
            f"📅 *上传时间*：`{created_str}`\n"
            f"📈 *下载次数*：`{count}` 次\n\n"
            f"🔗 [点击下载]({url})\n\n"
            f"链接：`{url}`"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏳ 临时链接(1h)", callback_data=f"ts:{short_id}"), InlineKeyboardButton("✏️ 重命名", callback_data=f"rn:{short_id}")],
            [InlineKeyboardButton("🗑️ 删除", callback_data=f"cd:{short_id}"), InlineKeyboardButton("🔙 返回列表", callback_data="list_files")]
        ])
        await update_view(update, context, text, reply_markup=kb, photo=qr)
    except Exception as e: await update_view(update, context, f"❌ 获取详情失败: {e}")

async def get_temp_link(update, context, short_id):
    name = callback_map.get(short_id)
    try:
        # 生成 1 小时有效的签名链接
        res = supabase.storage.from_(SUPABASE_BUCKET_NAME).create_signed_url(name, 3600)
        temp_url = res.get('signedURL', res) if isinstance(res, dict) else res
        await update.callback_query.answer("✅ 已生成 1 小时有效链接", show_alert=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⏳ *临时分享链接 (1小时有效)*：\n\n`{temp_url}`", parse_mode='Markdown')
    except Exception as e: await update.callback_query.answer(f"❌ 生成失败: {e}", show_alert=True)



async def start_rename(update, context, short_id):
    name = callback_map.get(short_id)
    uid = update.effective_user.id
    user_data[uid].update({'waiting_rename': True, 'old_name': name})
    await update_view(update, context, f"✏️ *重命名*：`{name}`\n\n请输入新名称（支持路径如 `文档/1.pdf`）：")

async def do_rename(update, context, new_name):
    uid = update.effective_user.id
    old_name = user_data[uid].get('old_name')
    user_data[uid]['waiting_rename'] = False
    if new_name.lower() == '/cancel':
        await send_file_list(update, context)
        return
    try:
        file_data = supabase.storage.from_(SUPABASE_BUCKET_NAME).download(old_name)
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=new_name, file=file_data, file_options={'upsert': 'true'})
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([old_name])
        # 同步更新本地数据
        data = load_data()
        if old_name in data['file_stats']:
            data['file_stats'][new_name] = data['file_stats'].pop(old_name)

        save_data(data)
        await send_file_list(update, context)
    except Exception: await update_view(update, context, "❌ 重命名失败")

async def confirm_delete(update, context, short_id):
    name = callback_map.get(short_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ 确认删除", callback_data=f"yd:{short_id}"), InlineKeyboardButton("❌ 取消", callback_data="list_files")]])
    await update_view(update, context, f"⚠️ *确认删除？*\n`{name}`", reply_markup=kb)

async def do_delete(update, context, short_id):
    name = callback_map.get(short_id)
    if name: 
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([name])
        data = load_data()
        if name in data['file_stats']: del data['file_stats'][name]

        save_data(data)
    await send_file_list(update, context)

async def send_batch_del(update, context):
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        real_files = [f for f in files if f.get('name') != '.emptyFolderPlaceholder']
        kb = []
        for f in real_files[:10]:
            kb.append([InlineKeyboardButton(f"🗑 {f['name'][:30]}", callback_data=f"bs:{get_short_id(f['name'])}")])
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="list_files")])
        await update_view(update, context, "🧹 *批量删除模式*\n点击下方按钮立即删除文件：", reply_markup=InlineKeyboardMarkup(kb))
    except Exception: pass

async def do_batch_del_single(update, context, short_id):
    name = callback_map.get(short_id)
    if name: supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([name])
    await send_batch_del(update, context)

async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_verified(uid, update.effective_user): return
    msg = update.message
    try: await msg.delete()
    except Exception: pass
    
    file_obj = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file_obj: return
    
    name = getattr(file_obj, 'file_name', None) or f"img_{datetime.now(BJ_TZ).strftime('%m%d_%H%M%S')}.jpg"
    await update_view(update, context, f"⏳ *正在上传*：`{name}`...")
    
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        path = await tg_file.download_to_drive()
        with open(path, 'rb') as f: content = f.read()
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=name, file=content, file_options={'upsert': 'true'})
        await show_file_detail(update, context, get_short_id(name))
        if os.path.exists(path): os.remove(path)
    except Exception as e: await update_view(update, context, f"❌ 失败: {e}")

# ========== 启动 ==========
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, format, *args): return

def main():
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), HealthCheckHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setpwd", set_pwd_command))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO, handle_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__': main()
