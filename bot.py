import os
import json
import logging
import asyncio
import qrcode
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from io import BytesIO
from datetime import datetime, timezone, timedelta

BJ_TZ = timezone(timedelta(hours=8))

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
    ConversationHandler,
    filters
)
from supabase import create_client, Client

# 配置信息
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_BUCKET_NAME = "public-files"

# 密码保护
ADMIN_USERNAMES = ["btcwv", "LDvipa"]
AUTH_FILE = "auth_data.json"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 用户临时数据
user_data = {}
# 用于解决 Telegram callback_data 64字节限制的映射表
callback_map = {}

def get_short_id(full_text):
    """生成短ID以规避Telegram 64字节限制"""
    short_id = hashlib.md5(full_text.encode()).hexdigest()[:10]
    callback_map[short_id] = full_text
    return short_id

# ========== 密码系统 ==========

def load_auth():
    default = {'password': 'btcwv', 'verified_users': []}
    try:
        if os.path.exists(AUTH_FILE):
            with open(AUTH_FILE, 'r') as f:
                return json.load(f)
    except Exception: pass
    save_auth(default)
    return default

def save_auth(data):
    try:
        with open(AUTH_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"保存认证数据失败: {e}")

def is_admin(user):
    if not user or not user.username: return False
    username = user.username.lower().replace('@', '')
    return any(admin.lower() == username for admin in ADMIN_USERNAMES)

def is_verified(uid):
    auth = load_auth()
    return uid in auth.get('verified_users', [])

def verify_user(uid):
    auth = load_auth()
    if uid not in auth['verified_users']:
        auth['verified_users'].append(uid)
        save_auth(auth)

def check_password(pwd):
    auth = load_auth()
    return pwd == auth.get('password', '')

def change_password(new_pwd):
    auth = load_auth()
    auth['password'] = new_pwd
    save_auth(auth)

# ========== 工具函数 ==========

def generate_qr(url):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    qr_img.save(buf, format='PNG')
    buf.seek(0)
    return buf

def format_size(size_bytes):
    if not size_bytes: return "未知"
    if size_bytes < 1024: return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
    else: return f"{size_bytes / (1024 * 1024):.1f} MB"

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📂 文件列表"), KeyboardButton("📤 上传文件")],
        [KeyboardButton("🔍 搜索文件"), KeyboardButton("ℹ️ 帮助")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def auto_delete(msg, delay=5):
    await asyncio.sleep(delay)
    try: await msg.delete()
    except Exception: pass

async def safe_edit_or_reply(query, text, reply_markup=None, parse_mode='Markdown'):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await query.message.delete()
            await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logging.error(f"回复失败: {e}")

# ========== 权限检查 ==========

async def require_auth(update: Update):
    user = update.effective_user
    uid = user.id
    if is_admin(user):
        verify_user(uid)
        return True
    if is_verified(uid): return True
    user_data[uid] = {'waiting_password': True}
    msg = await update.message.reply_text("🔐 *访问受限*\n\n请输入访问密码以继续：", parse_mode='Markdown')
    user_data[uid]['pwd_prompt'] = msg
    return False

# ========== 命令处理 ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    if is_admin(user): verify_user(uid)
    if not is_verified(uid):
        user_data[uid] = {'waiting_password': True}
        msg = await update.message.reply_text("👋 *你好！欢迎使用文件助手*\n\n🔐 请输入访问密码：", parse_mode='Markdown')
        user_data[uid]['pwd_prompt'] = msg
        return
    text = (
        "👋 *你好！我是文件助手*\n\n"
        "📤 *上传*：直接发送文件/图片/视频\n"
        "📂 *管理*：点击下方按钮查看列表\n\n"
        "请选择操作 👇"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def cmd_setpwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not is_admin(update.effective_user):
        msg = await update.message.reply_text("❌ 只有管理员可以修改密码")
        asyncio.create_task(auto_delete(msg, 3))
        return
    if not context.args:
        msg = await update.message.reply_text("用法：`/setpwd 新密码`", parse_mode='Markdown')
        asyncio.create_task(auto_delete(msg, 5))
        return
    new_pwd = ' '.join(context.args)
    change_password(new_pwd)
    msg = await update.message.reply_text(f"✅ 密码已成功修改")
    asyncio.create_task(auto_delete(msg, 3))

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not await require_auth(update): return
    await send_file_list(update.message)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not await require_auth(update): return
    text = (
        "📖 *使用说明*\n\n"
        "1️⃣ *上传*：直接发送任何文件给机器人\n"
        "2️⃣ *列表*：发送 /list 或点击菜单按钮\n"
        "3️⃣ *搜索*：`/search 关键词` 查找文件\n"
        "4️⃣ *删除*：`/delete 文件名` 或在详情页操作\n\n"
        "💡 *提示*：同名文件上传会覆盖旧文件。"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(), parse_mode='Markdown')

# ========== 交互逻辑 ==========

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in user_data and user_data[uid].get('waiting_password'):
        pwd_input = update.message.text.strip()
        asyncio.create_task(auto_delete(update.message, 1))
        prompt_msg = user_data[uid].get('pwd_prompt')
        if check_password(pwd_input):
            verify_user(uid)
            user_data.pop(uid, None)
            if prompt_msg: asyncio.create_task(auto_delete(prompt_msg, 0))
            msg = await update.message.reply_text("✅ 验证成功！", reply_markup=get_main_keyboard())
            asyncio.create_task(auto_delete(msg, 3))
            await start(update, context)
        else:
            msg = await update.message.reply_text("❌ 密码错误，请重新输入：")
            user_data[uid]['pwd_prompt'] = msg
            if prompt_msg: asyncio.create_task(auto_delete(prompt_msg, 0))
        return

    if uid in user_data and user_data[uid].get('waiting_rename'):
        await do_rename(update, context)
        return

    text = update.message.text.strip()
    asyncio.create_task(auto_delete(update.message, 1))
    if not is_verified(uid) and not is_admin(update.effective_user):
        if not await require_auth(update): return

    if text == "📂 文件列表": await send_file_list(update.message)
    elif text == "📤 上传文件":
        msg = await update.message.reply_text("📤 *请直接发送文件/图片/视频给我*", parse_mode='Markdown')
        asyncio.create_task(auto_delete(msg, 5))
    elif text == "🔍 搜索文件":
        msg = await update.message.reply_text("🔍 *请输入关键词搜索*\n例如：`/search apk`", parse_mode='Markdown')
        asyncio.create_task(auto_delete(msg, 8))
    elif text == "ℹ️ 帮助": await cmd_help(update, context)
    else:
        msg = await update.message.reply_text("💡 *请发送文件上传，或使用下方菜单* 👇", parse_mode='Markdown')
        asyncio.create_task(auto_delete(msg, 5))

# ========== UI 优化版文件列表 ==========

async def send_file_list(message, page=0, query=None):
    page_size = 6
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        real_files = [f for f in files if f.get('name') != '.emptyFolderPlaceholder'] if files else []
        if not real_files:
            text = "📭 *暂无文件*\n\n直接发送文件给我即可上传 👇"
            if query: await query.edit_message_text(text, parse_mode='Markdown')
            else: await message.reply_text(text, parse_mode='Markdown')
            return

        total = len(real_files)
        total_pages = (total + page_size - 1) // page_size
        page = max(0, min(page, total_pages - 1))
        start_idx, end_idx = page * page_size, min((page + 1) * page_size, total)
        page_files = real_files[start_idx:end_idx]

        text = f"📂 *文件列表* (共 {total} 个)\n"
        text += f"━━━━━━━━━━━━━━━\n"
        keyboard = []
        for f in page_files:
            name = f['name']
            size = format_size(f.get('metadata', {}).get('size', 0))
            display = name if len(name) <= 25 else name[:22] + "..."
            keyboard.append([InlineKeyboardButton(f"📄 {display} ({size})", callback_data=f"lk:{get_short_id(name)}")])

        # 导航栏
        nav_row = []
        if page > 0: nav_row.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"pg:{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="noop"))
        if page < total_pages - 1: nav_row.append(InlineKeyboardButton("➡️ 下页", callback_data=f"pg:{page + 1}"))
        keyboard.append(nav_row)
        
        # 功能栏
        keyboard.append([
            InlineKeyboardButton("🧹 批量删除", callback_data="batch_del"),
            InlineKeyboardButton("🔄 刷新", callback_data=f"pg:{page}")
        ])

        if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception as e:
        logging.error(f"列表错误: {e}")

# ========== 详情页优化 ==========

async def show_file_link(query, short_id):
    file_name = callback_map.get(short_id)
    if not file_name:
        await query.answer("❌ 链接已失效，请刷新列表", show_alert=True)
        return
    try:
        public_url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(file_name)
        qr_buf = generate_qr(public_url)
        caption = (
            f"📄 *文件名*：`{file_name}`\n\n"
            f"🔗 *下载链接*：[点击下载]({public_url})\n\n"
            f"👇 *操作菜单*"
        )
        keyboard = [
            [InlineKeyboardButton("✏️ 重命名", callback_data=f"rn:{short_id}"),
             InlineKeyboardButton("🗑️ 删除文件", callback_data=f"cd:{short_id}")],
            [InlineKeyboardButton("🔙 返回列表", callback_data='list_files')]
        ]
        await query.message.delete()
        await query.message.reply_photo(photo=qr_buf, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception as e:
        await query.answer(f"错误: {e}", show_alert=True)

# ========== 回调分发器 ==========

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer() # 立即响应，消除按钮转圈

    if data == 'list_files': await send_file_list(None, query=query)
    elif data == 'batch_del': await send_batch_delete_list(query)
    elif data.startswith('lk:'): await show_file_link(query, data[3:])
    elif data.startswith('pg:'): await send_file_list(None, page=int(data[3:]), query=query)
    elif data.startswith('cd:'): await confirm_delete(query, data[3:])
    elif data.startswith('yd:'): await do_delete(query, data[3:])
    elif data.startswith('rn:'): await start_rename(query, data[3:])
    elif data.startswith('bs:'): await do_single_batch_delete(query, data[3:])
    elif data == 'bd_all': await confirm_delete_all(query)
    elif data == 'yd_all': await do_delete_all(query)

# ========== 其他交互函数 (简化版) ==========

async def confirm_delete(query, short_id):
    file_name = callback_map.get(short_id, "未知文件")
    text = f"⚠️ *确认删除该文件吗？*\n\n📄 `{file_name}`\n\n此操作不可撤销！"
    keyboard = [
        [InlineKeyboardButton("✅ 确认删除", callback_data=f"yd:{short_id}")],
        [InlineKeyboardButton("❌ 取消", callback_data="list_files")]
    ]
    await safe_edit_or_reply(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def do_delete(query, short_id):
    file_name = callback_map.get(short_id)
    if file_name:
        try:
            supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([file_name])
            await query.answer(f"✅ 已删除 {file_name}")
        except Exception: pass
    await send_file_list(None, query=query)

async def send_batch_delete_list(query):
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        real_files = [f for f in files if f.get('name') != '.emptyFolderPlaceholder'] if files else []
        text = "🧹 *批量删除模式*\n点击下方按钮立即删除文件："
        keyboard = []
        for f in real_files[:10]:
            name = f['name']
            keyboard.append([InlineKeyboardButton(f"🗑 {name[:30]}", callback_data=f"bs:{get_short_id(name)}")])
        keyboard.append([InlineKeyboardButton("💥 删除全部", callback_data="bd_all")])
        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="list_files")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception: pass

async def do_single_batch_delete(query, short_id):
    file_name = callback_map.get(short_id)
    if file_name:
        try: supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([file_name])
        except Exception: pass
    await send_batch_delete_list(query)

async def confirm_delete_all(query):
    text = "🚨 *警告：确认删除全部文件？*\n\n所有存储的文件都将被清空！"
    keyboard = [[InlineKeyboardButton("🔥 确认全部清空", callback_data="yd_all")], [InlineKeyboardButton("❌ 取消", callback_data="list_files")]]
    await safe_edit_or_reply(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def do_delete_all(query):
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        names = [f['name'] for f in files if f.get('name') != '.emptyFolderPlaceholder']
        if names: supabase.storage.from_(SUPABASE_BUCKET_NAME).remove(names)
    except Exception: pass
    await send_file_list(None, query=query)

async def start_rename(query, short_id):
    file_name = callback_map.get(short_id)
    if not file_name: return
    uid = query.from_user.id
    user_data[uid] = {'waiting_rename': True, 'old_name': file_name}
    ext = file_name[file_name.rfind('.'):] if '.' in file_name else ''
    user_data[uid]['ext'] = ext
    await query.message.delete()
    msg = await query.message.reply_text(f"✏️ *重命名*：`{file_name}`\n\n请输入新文件名（无需后缀，后缀 `{ext}` 会自动保留）：\n\n发送 `/cancel` 取消", parse_mode='Markdown')
    user_data[uid]['prompt_msg'] = msg

async def do_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = user_data.get(uid, {})
    old_name, ext = data.get('old_name', ''), data.get('ext', '')
    raw_input = update.message.text.strip()
    asyncio.create_task(auto_delete(update.message, 1))
    user_data.pop(uid, None)
    if raw_input.lower() == '/cancel': 
        await start(update, context)
        return
    new_name = raw_input if '.' in raw_input else raw_input + ext
    try:
        file_data = supabase.storage.from_(SUPABASE_BUCKET_NAME).download(old_name)
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=new_name, file=file_data, file_options={'upsert': 'true'})
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([old_name])
        await update.message.reply_text(f"✅ 已重命名为：`{new_name}`", parse_mode='Markdown')
        await send_file_list(update.message)
    except Exception as e:
        await update.message.reply_text(f"❌ 失败: {e}")

# ========== 上传处理 (保持原样但优化反馈) ==========

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update): return
    doc = update.message.document
    status = await update.message.reply_text(f"⏳ *正在上传*：`{doc.file_name}`...", parse_mode='Markdown')
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        path = await tg_file.download_to_drive()
        with open(path, 'rb') as f: content = f.read()
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=doc.file_name, file=content, file_options={'upsert': 'true'})
        url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(doc.file_name)
        await status.delete()
        await update.message.reply_photo(photo=generate_qr(url), caption=f"✅ *上传成功*\n\n📄 `{doc.file_name}`\n🔗 [点击下载]({url})", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📂 查看列表", callback_data='list_files')]]))
    except Exception as e: await status.edit_text(f"❌ 失败: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update): return
    photo = update.message.photo[-1]
    name = f"img_{datetime.now(BJ_TZ).strftime('%m%d_%H%M%S')}.jpg"
    status = await update.message.reply_text("⏳ *正在上传图片...*", parse_mode='Markdown')
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        path = await tg_file.download_to_drive()
        with open(path, 'rb') as f: content = f.read()
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=name, file=content, file_options={'upsert': 'true'})
        url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(name)
        await status.delete()
        await update.message.reply_photo(photo=generate_qr(url), caption=f"✅ *图片上传成功*\n\n🔗 [点击下载]({url})", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📂 查看列表", callback_data='list_files')]]))
    except Exception as e: await status.edit_text(f"❌ 失败: {e}")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update): return
    video = update.message.video
    name = video.file_name or f"vid_{datetime.now(BJ_TZ).strftime('%m%d_%H%M%S')}.mp4"
    status = await update.message.reply_text(f"⏳ *正在上传视频*：`{name}`...", parse_mode='Markdown')
    try:
        tg_file = await context.bot.get_file(video.file_id)
        path = await tg_file.download_to_drive()
        with open(path, 'rb') as f: content = f.read()
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=name, file=content, file_options={'upsert': 'true'})
        url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(name)
        await status.delete()
        await update.message.reply_photo(photo=generate_qr(url), caption=f"✅ *视频上传成功*\n\n🔗 [点击下载]({url})", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📂 查看列表", callback_data='list_files')]]))
    except Exception as e: await status.edit_text(f"❌ 失败: {e}")

# ========== 启动 ==========

async def post_init(application):
    await application.bot.set_my_commands([BotCommand("start", "开始"), BotCommand("list", "列表"), BotCommand("help", "帮助")])

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, format, *args): return

def main():
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), HealthCheckHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__': main()
