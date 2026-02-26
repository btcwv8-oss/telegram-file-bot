import os
import json
import logging
import asyncio
import qrcode
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
ADMIN_USERNAMES = ["btcwv", "LDvipa"]  # 管理员用户名列表（不含@）
AUTH_FILE = "auth_data.json"  # 持久化存储

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 会话状态
WAITING_RENAME = 1

# 用户临时数据
user_data = {}

# ========== 密码系统 ==========

def load_auth():
    """加载认证数据"""
    default = {'password': 'btcwv', 'verified_users': []}
    try:
        if os.path.exists(AUTH_FILE):
            with open(AUTH_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    save_auth(default)
    return default

def save_auth(data):
    """保存认证数据"""
    try:
        with open(AUTH_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"保存认证数据失败: {e}")

def is_admin(user):
    """判断是否为管理员"""
    if not user or not user.username:
        return False
    username = user.username.lower().replace('@', '')
    return any(admin.lower() == username for admin in ADMIN_USERNAMES)

def is_verified(uid):
    """判断用户是否已验证"""
    auth = load_auth()
    return uid in auth.get('verified_users', [])

def verify_user(uid):
    """添加已验证用户"""
    auth = load_auth()
    if uid not in auth['verified_users']:
        auth['verified_users'].append(uid)
        save_auth(auth)

def check_password(pwd):
    """检查密码"""
    auth = load_auth()
    return pwd == auth.get('password', '')

def change_password(new_pwd):
    """修改密码"""
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
    if not size_bytes:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📂 文件列表"), KeyboardButton("📤 上传文件")],
        [KeyboardButton("🔍 搜索文件"), KeyboardButton("ℹ️ 帮助")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def auto_delete(msg, delay=5):
    """延迟自动删除消息"""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass

async def safe_edit_or_reply(query, text, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            # 如果是图片消息无法直接编辑文字，则删除旧的发送新的
            await query.message.delete()
            await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logging.error(f"回复失败: {e}")

# ========== 权限检查 ==========

async def require_auth(update: Update):
    """检查用户是否已验证，未验证则提示输入密码。返回 True 表示已验证"""
    user = update.effective_user
    uid = user.id
    if is_admin(user):
        verify_user(uid)
        return True
    if is_verified(uid):
        return True
    user_data[uid] = {'waiting_password': True}
    msg = await update.message.reply_text("🔐 请输入访问密码：")
    user_data[uid]['pwd_prompt'] = msg
    return False

# ========== 命令处理 ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    if is_admin(user):
        verify_user(uid)
    if not is_verified(uid):
        user_data[uid] = {'waiting_password': True}
        msg = await update.message.reply_text("👋 你好！欢迎使用文件助手\n\n🔐 请输入访问密码：")
        user_data[uid]['pwd_prompt'] = msg
        return
    text = (
        "👋 你好！我是文件助手\n\n"
        "📤 直接发送文件/图片/视频即可上传\n"
        "📂 上传后自动生成下载链接和二维码\n\n"
        "使用下方菜单栏操作 👇"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard())

async def cmd_setpwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not is_admin(update.effective_user):
        msg = await update.message.reply_text("❌ 只有管理员可以修改密码")
        asyncio.create_task(auto_delete(msg, 3))
        return
    if not context.args:
        msg = await update.message.reply_text("用法：/setpwd 新密码")
        asyncio.create_task(auto_delete(msg, 5))
        return
    new_pwd = ' '.join(context.args)
    change_password(new_pwd)
    msg = await update.message.reply_text(f"✅ 密码已修改")
    asyncio.create_task(auto_delete(msg, 3))

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not await require_auth(update):
        return
    await send_file_list(update.message)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not await require_auth(update):
        return
    text = (
        "ℹ️ 使用说明\n\n"
        "📤 发送文件/图片/视频 → 自动上传\n"
        "📂 /list → 查看已上传文件\n"
        "🔍 /search 关键词 → 搜索文件\n"
        "🗑️ /delete 文件名 → 删除文件\n"
        "🧹 /clear → 批量删除文件\n"
        "❓ /help → 查看帮助\n\n"
        "支持任意格式，同名文件自动覆盖。"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard())

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not await require_auth(update):
        return
    if not context.args:
        msg = await update.message.reply_text("🔍 请输入关键词\n例如：/search apk")
        asyncio.create_task(auto_delete(msg, 8))
        return
    keyword = ' '.join(context.args).lower()
    await search_files(update.message, keyword)

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not await require_auth(update):
        return
    if not context.args:
        msg = await update.message.reply_text("🗑️ 请输入文件名\n例如：/delete test.apk")
        asyncio.create_task(auto_delete(msg, 8))
        return
    file_name = ' '.join(context.args)
    try:
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([file_name])
        msg = await update.message.reply_text(f"✅ 已删除：{file_name}", reply_markup=get_main_keyboard())
        asyncio.create_task(auto_delete(msg, 5))
    except Exception as e:
        await update.message.reply_text(f"❌ 删除失败：{e}")

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(auto_delete(update.message, 1))
    if not await require_auth(update):
        return
    await send_batch_delete_list(update.message)

# ========== 文本处理 ==========

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
        user_data[uid] = {'waiting_password': True}
        msg = await update.message.reply_text("🔐 请先输入访问密码：")
        user_data[uid]['pwd_prompt'] = msg
        return
    if text == "📂 文件列表":
        await send_file_list(update.message)
    elif text == "📤 上传文件":
        msg = await update.message.reply_text("📤 直接发送文件/图片/视频给我即可上传")
        asyncio.create_task(auto_delete(msg, 5))
    elif text == "🔍 搜索文件":
        msg = await update.message.reply_text("🔍 请发送关键词搜索\n例如：/search apk")
        asyncio.create_task(auto_delete(msg, 8))
    elif text == "ℹ️ 帮助":
        await cmd_help(update, context)
    else:
        msg = await update.message.reply_text("💡 直接发送文件即可上传\n或使用下方菜单操作 👇")
        asyncio.create_task(auto_delete(msg, 5))

# ========== 文件列表（原地编辑版） ==========

async def send_file_list(message, page=0, query=None):
    page_size = 8
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        real_files = [f for f in files if f.get('name') != '.emptyFolderPlaceholder'] if files else []
        if not real_files:
            text = "📭 暂无文件\n\n直接发送文件给我即可上传 👇"
            if query: await query.edit_message_text(text)
            else: await message.reply_text(text)
            return

        total = len(real_files)
        total_pages = (total + page_size - 1) // page_size
        page = max(0, min(page, total_pages - 1))
        start_idx = page * page_size
        end_idx = min(start_idx + page_size, total)
        page_files = real_files[start_idx:end_idx]

        text = f"📂 文件列表（共 {total} 个）\n\n"
        keyboard = []
        for f in page_files:
            name = f['name']
            display = name if len(name) <= 35 else name[:32] + "..."
            keyboard.append([InlineKeyboardButton(f"📄 {display}", callback_data=f"lk:{name[:50]}")])

        nav_row = []
        if page > 0: nav_row.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"pg:{page - 1}"))
        nav_row.append(InlineKeyboardButton("🧹 批量删除", callback_data="batch_del"))
        if page < total_pages - 1: nav_row.append(InlineKeyboardButton("➡️ 下页", callback_data=f"pg:{page + 1}"))
        keyboard.append(nav_row)
        text += f"第 {page + 1}/{total_pages} 页"

        if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logging.error(f"获取列表失败: {e}")

# ========== 批量删除列表 ==========

async def send_batch_delete_list(message, query=None):
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        real_files = [f for f in files if f.get('name') != '.emptyFolderPlaceholder'] if files else []
        if not real_files:
            if query: await query.edit_message_text("📭 暂无文件")
            else: await message.reply_text("📭 暂无文件")
            return

        text = "🧹 批量删除\n\n点击选择要删除的文件：\n"
        keyboard = []
        for f in real_files[:15]:
            name = f['name']
            display = name if len(name) <= 30 else name[:27] + "..."
            keyboard.append([InlineKeyboardButton(f"☐ {display}", callback_data=f"bs:{name[:45]}")])
        keyboard.append([InlineKeyboardButton("🗑 删除全部", callback_data="bd_all"), InlineKeyboardButton("🔙 返回", callback_data="list_files")])

        if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logging.error(f"获取列表失败: {e}")

# ========== 搜索结果 ==========

async def search_files(message, keyword, query=None):
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        real_files = [f for f in files if f.get('name') != '.emptyFolderPlaceholder'] if files else []
        matched = [f for f in real_files if keyword in f['name'].lower()]
        if not matched:
            text = f"🔍 未找到「{keyword}」相关文件"
            if query: await query.edit_message_text(text)
            else: await message.reply_text(text)
            return

        text = f"🔍 搜索结果（{len(matched)} 个）\n\n"
        keyboard = []
        for f in matched[:10]:
            name = f['name']
            display = name if len(name) <= 35 else name[:32] + "..."
            keyboard.append([InlineKeyboardButton(f"📄 {display}", callback_data=f"lk:{name[:50]}")])
        keyboard.append([InlineKeyboardButton("🔙 返回列表", callback_data="list_files")])

        if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logging.error(f"搜索失败: {e}")

# ========== 回调处理 ==========

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'list_files': await send_file_list(None, query=query)
    elif data == 'batch_del': await send_batch_delete_list(None, query=query)
    elif data.startswith('lk:'): await show_file_link(query, data[3:])
    elif data.startswith('cd:'): await confirm_delete(query, data[3:])
    elif data.startswith('yd:'): await do_delete(query, data[3:])
    elif data.startswith('nd:'): await send_file_list(None, query=query)
    elif data.startswith('pg:'): await send_file_list(None, page=int(data[3:]), query=query)
    elif data.startswith('rn:'): await start_rename(query, data[3:])
    elif data.startswith('bs:'): await do_single_batch_delete(query, data[3:])
    elif data == 'bd_all': await confirm_delete_all(query)
    elif data == 'yd_all': await do_delete_all(query)

# ========== 文件详情 ==========

async def show_file_link(query, file_name):
    try:
        public_url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(file_name)
        qr_buf = generate_qr(public_url)
        caption = f"📄 {file_name}\n\n🔗 [点击下载]({public_url})\n\n链接：\n`{public_url}`"
        keyboard = [
            [InlineKeyboardButton("✏️ 改名", callback_data=f"rn:{file_name[:45]}"), InlineKeyboardButton("🗑️ 删除", callback_data=f"cd:{file_name[:50]}")],
            [InlineKeyboardButton("🔙 返回列表", callback_data='list_files')]
        ]
        # 详情页因为带图片，通常发送新消息更稳妥，但我们先尝试删除旧消息
        await query.message.delete()
        await query.message.reply_photo(photo=qr_buf, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception as e:
        await safe_edit_or_reply(query, f"❌ 获取链接失败：{e}")

async def confirm_delete(query, file_name):
    text = f"⚠️ 确认删除？\n\n📄 {file_name}"
    keyboard = [[InlineKeyboardButton("✅ 确认删除", callback_data=f"yd:{file_name[:50]}"), InlineKeyboardButton("❌ 取消", callback_data="list_files")]]
    await safe_edit_or_reply(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def do_delete(query, file_name):
    try:
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([file_name])
        await send_file_list(None, query=query)
    except Exception as e:
        await safe_edit_or_reply(query, f"❌ 删除失败：{e}")

async def do_single_batch_delete(query, file_name):
    try:
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([file_name])
        await send_batch_delete_list(None, query=query)
    except Exception as e:
        await safe_edit_or_reply(query, f"❌ 删除失败：{e}")

async def confirm_delete_all(query):
    text = "⚠️ 确认删除全部文件？\n\n此操作不可恢复！"
    keyboard = [[InlineKeyboardButton("✅ 确认全部删除", callback_data="yd_all"), InlineKeyboardButton("❌ 取消", callback_data="list_files")]]
    await safe_edit_or_reply(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def do_delete_all(query):
    try:
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list()
        real_files = [f['name'] for f in files if f.get('name') != '.emptyFolderPlaceholder'] if files else []
        if real_files: supabase.storage.from_(SUPABASE_BUCKET_NAME).remove(real_files)
        await send_file_list(None, query=query)
    except Exception as e:
        await safe_edit_or_reply(query, f"❌ 删除失败：{e}")

# ========== 重命名 ==========

async def start_rename(query, file_name):
    uid = query.from_user.id
    user_data[uid] = {'waiting_rename': True, 'old_name': file_name}
    ext = file_name[file_name.rfind('.'):] if '.' in file_name else ''
    user_data[uid]['ext'] = ext
    await query.message.delete()
    msg = await query.message.reply_text(f"✏️ 请输入新文件名（不需要后缀）\n\n当前：{file_name}\n后缀 {ext} 会自动保留\n\n发送 /cancel 取消")
    user_data[uid]['prompt_msg'] = msg

async def do_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = user_data.get(uid, {})
    old_name, ext = data.get('old_name', ''), data.get('ext', '')
    raw_input = update.message.text.strip()
    asyncio.create_task(auto_delete(update.message, 1))
    new_name = raw_input if '.' in raw_input else raw_input + ext
    prompt_msg = data.get('prompt_msg')
    user_data.pop(uid, None)
    if prompt_msg: asyncio.create_task(auto_delete(prompt_msg, 0))
    if raw_input.lower() == '/cancel': return
    try:
        file_data = supabase.storage.from_(SUPABASE_BUCKET_NAME).download(old_name)
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=new_name, file=file_data, file_options={'content-type': 'application/octet-stream', 'upsert': 'true'})
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([old_name])
        await send_file_list(update.message)
    except Exception as e:
        await update.message.reply_text(f"❌ 重命名失败：{e}")

# ========== 上传处理 ==========

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update): return
    doc = update.message.document
    file_name, file_size = doc.file_name, format_size(doc.file_size)
    status = await update.message.reply_text(f"⏳ 上传中：{file_name}...")
    path = None
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        path = await tg_file.download_to_drive()
        with open(path, 'rb') as f: content = f.read()
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=file_name, file=content, file_options={'content-type': doc.mime_type or 'application/octet-stream', 'upsert': 'true'})
        public_url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(file_name)
        qr_buf = generate_qr(public_url)
        await status.delete()
        caption = f"✅ 上传成功\n\n📄 {file_name}（{file_size}）\n🔗 [点击下载]({public_url})\n\n链接：\n`{public_url}`"
        await update.message.reply_photo(photo=qr_buf, caption=caption, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📂 文件列表", callback_data='list_files')]]))
    except Exception as e:
        await status.edit_text(f"❌ 上传失败：{e}")
    finally:
        if path and os.path.exists(str(path)): os.remove(str(path))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update): return
    photo = update.message.photo[-1]
    file_name = f"photo_{datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')}.jpg"
    status = await update.message.reply_text("⏳ 上传图片中...")
    path = None
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        path = await tg_file.download_to_drive()
        with open(path, 'rb') as f: content = f.read()
        file_size = format_size(len(content))
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=file_name, file=content, file_options={'content-type': 'image/jpeg', 'upsert': 'true'})
        public_url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(file_name)
        qr_buf = generate_qr(public_url)
        await status.delete()
        caption = f"✅ 图片上传成功\n\n📄 {file_name}（{file_size}）\n🔗 [点击下载]({public_url})\n\n链接：\n`{public_url}`"
        await update.message.reply_photo(photo=qr_buf, caption=caption, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📂 文件列表", callback_data='list_files')]]))
    except Exception as e:
        await status.edit_text(f"❌ 图片上传失败：{e}")
    finally:
        if path and os.path.exists(str(path)): os.remove(str(path))

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update): return
    video = update.message.video
    file_name = video.file_name or f"video_{datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')}.mp4"
    file_size = format_size(video.file_size)
    status = await update.message.reply_text(f"⏳ 上传视频中：{file_name}...")
    path = None
    try:
        tg_file = await context.bot.get_file(video.file_id)
        path = await tg_file.download_to_drive()
        with open(path, 'rb') as f: content = f.read()
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=file_name, file=content, file_options={'content-type': video.mime_type or 'video/mp4', 'upsert': 'true'})
        public_url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(file_name)
        qr_buf = generate_qr(public_url)
        await status.delete()
        caption = f"✅ 视频上传成功\n\n📄 {file_name}（{file_size}）\n🔗 [点击下载]({public_url})\n\n链接：\n`{public_url}`"
        await update.message.reply_photo(photo=qr_buf, caption=caption, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📂 文件列表", callback_data='list_files')]]))
    except Exception as e:
        await status.edit_text(f"❌ 视频上传失败：{e}")
    finally:
        if path and os.path.exists(str(path)): os.remove(str(path))

# ========== 启动 ==========

async def post_init(application):
    commands = [
        BotCommand("start", "开始使用"),
        BotCommand("list", "文件列表"),
        BotCommand("search", "搜索文件"),
        BotCommand("delete", "删除文件"),
        BotCommand("clear", "批量删除"),
        BotCommand("setpwd", "修改密码(管理员)"),
        BotCommand("help", "使用帮助"),
    ]
    await application.bot.set_my_commands(commands)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): return

def run_health_check():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logging.info(f"健康检查服务器运行在端口 {port}")
    server.serve_forever()

def main():
    threading.Thread(target=run_health_check, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("setpwd", cmd_setpwd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logging.info("机器人已启动...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
