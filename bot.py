import os
import json
import logging
import asyncio
import qrcode
import hashlib
import threading
import requests
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
        'file_stats': {}
    }
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
    data = load_data()
    return uid in data.get('verified_users', [])

def verify_user(uid):
    data = load_data()
    if uid not in data['verified_users']:
        data['verified_users'].append(uid)
        save_data(data)

# ========== 辅助函数 ==========
def format_size(size_bytes):
    if not size_bytes: return "0 B"
    size_bytes = int(size_bytes)
    if size_bytes < 1024: return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
    else: return f"{size_bytes / (1024 * 1024):.1f} MB"

def get_short_url(long_url):
    try:
        api_url = f"http://tinyurl.com/api-create.php?url={requests.utils.quote(long_url, safe=':/')}"
        res = requests.get(api_url, timeout=5)
        if res.status_code == 200:
            return res.text
    except Exception: pass
    return long_url

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
    """递归获取所有文件，包括子文件夹"""
    all_files = []
    def _list_dir(path=""):
        try:
            items = supabase.storage.from_(SUPABASE_BUCKET_NAME).list(path)
            for item in items:
                name = item.get('name')
                if not name or name == '.emptyFolderPlaceholder': continue
                full_path = f"{path}/{name}" if path else name
                if item.get('id') is None: # 这是一个文件夹
                    _list_dir(full_path)
                else:
                    item['full_path'] = full_path
                    all_files.append(item)
        except Exception as e:
            logging.error(f"List dir error at {path}: {e}")
    _list_dir()
    return all_files

# ========== 业务逻辑 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try: await update.message.delete()
    except Exception: pass

    if not is_verified(uid, update.effective_user):
        user_data.setdefault(uid, {})['waiting_pwd'] = True
        await update_view(update, context, "🔐 *访问受限*\n\n请输入访问密码：")
        return

    text = "👋 *你好！我是您的私人云端助手*\n\n发送 /list 查看文件列表\n直接发送文件或链接进行上传"
    new_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
    user_data.setdefault(uid, {})['mid'] = new_msg.message_id

async def set_pwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if text.startswith("http"):
        await handle_url_upload(update, context, text)
    else:
        await send_file_list(update, context, search_query=text)

async def handle_url_upload(update, context, url):
    await update_view(update, context, "⏳ *正在尝试远程转存...*")
    try:
        response = requests.get(url, stream=True, timeout=15)
        name = url.split('/')[-1].split('?')[0] or f"web_{datetime.now(BJ_TZ).strftime('%H%M%S')}.html"
        content = response.content
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(path=name, file=content, file_options={'upsert': 'true'})
        await show_file_detail(update, context, get_short_id(name))
    except Exception as e:
        await update_view(update, context, f"❌ 远程转存失败: {e}")

async def send_file_list(update, context, page=0, search_query=None):
    try:
        real_files = get_all_files()
        if search_query:
            real_files = [f for f in real_files if search_query.lower() in f['full_path'].lower()]

        total_size = sum(int(f.get('metadata', {}).get('size') or f.get('size', 0)) for f in real_files)
        percent = (total_size / (1024 * 1024 * 1024)) * 100
        storage_info = f"📊 *存储统计*：{format_size(total_size)} / 1 GB ({percent:.1f}%)"

        if not real_files:
            await update_view(update, context, f"{storage_info}\n\n📭 *暂无文件*")
            return

        page_size = 8
        total_pages = (len(real_files) + page_size - 1) // page_size
        page = max(0, min(page, total_pages - 1))
        
        text = f"{storage_info}\n\n📂 *文件列表* ({len(real_files)}个)\n━━━━━━━━━━━━━━━"
        kb = []
        # 按时间倒序排列
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
    full_path = callback_map.get(short_id)
    if not full_path:
        await update_view(update, context, "❌ 链接失效，请返回列表刷新")
        return
    data = load_data()
    data['file_stats'][full_path] = data['file_stats'].get(full_path, 0) + 1
    save_data(data)
    try:
        # 获取单个文件详情
        path_parts = full_path.split('/')
        folder = "/".join(path_parts[:-1]) if len(path_parts) > 1 else ""
        filename = path_parts[-1]
        files = supabase.storage.from_(SUPABASE_BUCKET_NAME).list(folder)
        file_info = next((f for f in files if f['name'] == filename), {})
        
        raw_size = file_info.get('metadata', {}).get('size') or file_info.get('size', 0)
        size = format_size(raw_size)
        created = file_info.get('created_at', '')
        if created:
            dt = datetime.fromisoformat(created.replace('Z', '+00:00')).astimezone(BJ_TZ)
            created_str = dt.strftime('%Y-%m-%d %H:%M')
        else: created_str = "未知"

        res = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(full_path)
        long_url = res if isinstance(res, str) else res.get('publicURL', res)
        
        short_url = get_short_url(long_url)
        qr = generate_qr(short_url)
        count = data['file_stats'].get(full_path, 0)
        
        text = (
            f"✅ *文件详情*\n\n"
            f"📄 文件名：`{full_path}`\n"
            f"⚖️ 大小：`{size}`\n"
            f"📅 上传时间：`{created_str}`\n"
            f"📈 下载次数：`{count}` 次\n\n"
            f"🔗 [点击下载]({short_url})\n\n"
            f"短链接：`{short_url}`\n"
            f"（微信扫码后请点击右上角在浏览器打开）"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏳ 临时链接", callback_data=f"ts:{short_id}"), InlineKeyboardButton("✏️ 重命名", callback_data=f"rn:{short_id}")],
            [InlineKeyboardButton("🗑️ 删除", callback_data=f"cd:{short_id}"), InlineKeyboardButton("🔙 返回列表", callback_data="list_files")]
        ])
        await update_view(update, context, text, reply_markup=kb, photo=qr)
    except Exception as e: await update_view(update, context, f"❌ 获取详情失败: {e}")

async def get_temp_link(update, context, short_id):
    full_path = callback_map.get(short_id)
    try:
        res = supabase.storage.from_(SUPABASE_BUCKET_NAME).create_signed_url(full_path, 3600)
        temp_url = res.get('signedURL', res) if isinstance(res, dict) else res
        short_temp_url = get_short_url(temp_url)
        await update.callback_query.answer("✅ 已生成 1 小时有效短链接", show_alert=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⏳ *临时分享链接 (1小时有效)*：\n\n`{short_temp_url}`", parse_mode='Markdown')
    except Exception as e: await update.callback_query.answer(f"❌ 生成失败: {e}", show_alert=True)

async def start_rename(update, context, short_id):
    full_path = callback_map.get(short_id)
    uid = update.effective_user.id
    user_data[uid].update({'waiting_rename': True, 'old_name': full_path})
    await update_view(update, context, f"✏️ *重命名*：`{full_path}`\n\n请输入新路径/名称：")

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
        data = load_data()
        if old_name in data['file_stats']: data['file_stats'][new_name] = data['file_stats'].pop(old_name)
        save_data(data)
        await send_file_list(update, context)
    except Exception: await update_view(update, context, "❌ 重命名失败")

async def confirm_delete(update, context, short_id):
    full_path = callback_map.get(short_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ 确认删除", callback_data=f"yd:{short_id}"), InlineKeyboardButton("❌ 取消", callback_data="list_files")]])
    await update_view(update, context, f"⚠️ *确认删除？*\n`{full_path}`", reply_markup=kb)

async def do_delete(update, context, short_id):
    full_path = callback_map.get(short_id)
    if full_path: 
        supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([full_path])
        data = load_data()
        if full_path in data['file_stats']: del data['file_stats'][full_path]
        save_data(data)
    await send_file_list(update, context)

async def send_batch_del(update, context):
    try:
        real_files = get_all_files()
        real_files.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        kb = []
        for f in real_files[:10]:
            full_path = f['full_path']
            kb.append([InlineKeyboardButton(f"🗑 {full_path[:30]}", callback_data=f"bs:{get_short_id(full_path)}")])
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="list_files")])
        await update_view(update, context, "🧹 *批量删除模式*\n点击下方按钮立即删除文件：", reply_markup=InlineKeyboardMarkup(kb))
    except Exception: pass

async def do_batch_del_single(update, context, short_id):
    full_path = callback_map.get(short_id)
    if full_path: supabase.storage.from_(SUPABASE_BUCKET_NAME).remove([full_path])
    await send_batch_del(update, context)

async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_verified(uid, update.effective_user): return
    msg = update.message
    try: await msg.delete()
    except Exception: pass
    file_obj = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not file_obj: return
    name = getattr(file_obj, 'file_name', None) or f"img_{datetime.now(BJ_TZ).strftime('%H%M%S')}.jpg"
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
    app.add_handler(CommandHandler("list", lambda u, c: send_file_list(u, c)))
    app.add_handler(CommandHandler("setpwd", set_pwd_command))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO, handle_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__': main()
