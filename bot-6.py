import os
import logging
import random
import string
import json
import asyncio
import urllib.request
import urllib.parse
import urllib.error
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pymongo import MongoClient
from flask import Flask
from threading import Thread

# =========================
# LOAD CONFIG
# =========================
load_dotenv()
logging.basicConfig(level=logging.INFO)

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MONGO_URI = os.environ.get("MONGO_URI", "")
LOG_CHANNEL_ENV = os.environ.get("LOG_CHANNEL", "-1004410113938").strip()
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL", "").strip()
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip()]

if not API_ID or not API_HASH or not BOT_TOKEN or not MONGO_URI:
    raise RuntimeError("Required environment variables missing: API_ID/API_HASH/BOT_TOKEN/MONGO_URI")

# =========================
# FLASK / RENDER KEEP-ALIVE
# =========================
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# =========================
# MONGODB
# =========================
try:
    mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    mongo.admin.command("ping")
    db = mongo["file_link_bot"]
    files_collection = db["files"]
    settings_collection = db["settings"]
    batches_collection = db["batches"]
    logging.info("MongoDB Connected Successfully!")
except Exception as e:
    logging.exception("MongoDB connection failed")
    raise

# =========================
# PYROGRAM CLIENT
# =========================
app = Client(
    "FileLinkBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# =========================
# TELEGRAM BOT API HELPERS
# IMPORTANT: Channel forwarding/copying uses Bot API directly.
# This avoids Pyrogram 'Peer id invalid' after Render restarts.
# =========================
def bot_api(method, data):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API HTTP {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"Telegram API request failed: {e}")

    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    return payload.get("result")


def tg_chat_id(value):
    value = str(value).strip()
    if value.startswith("@"):
        return value
    try:
        return int(value)
    except ValueError:
        return value

# =========================
# HELPERS
# =========================
def generate_random_string(length=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def configured_log_channel():
    setting = settings_collection.find_one({"_id": "log_channel"})
    if setting and setting.get("chat_id"):
        return setting["chat_id"]
    if LOG_CHANNEL_ENV:
        return tg_chat_id(LOG_CHANNEL_ENV)
    return None


def configured_update_channel():
    # Render ENV takes priority so an old MongoDB value cannot override it.
    if UPDATE_CHANNEL:
        return tg_chat_id(UPDATE_CHANNEL)

    setting = settings_collection.find_one({"_id": "update_channel"})
    username = setting.get("username") if setting else None
    if username:
        return tg_chat_id(username)

    if setting and setting.get("chat_id"):
        return setting["chat_id"]
    return None


def update_join_url():
    # Prefer the Render ENV username so an old MongoDB value cannot
    # send users to the wrong channel.
    if UPDATE_CHANNEL and not UPDATE_CHANNEL.lstrip("@").startswith("-100"):
        return f"https://t.me/{UPDATE_CHANNEL.lstrip('@')}"

    setting = settings_collection.find_one({"_id": "update_channel"})
    username = setting.get("username") if setting else None
    if username:
        return f"https://t.me/{username.lstrip('@')}"

    invite = settings_collection.find_one({"_id": "update_invite"})
    if invite and invite.get("url"):
        return invite["url"]
    return "https://t.me/"


async def is_user_member(client: Client, user_id: int) -> bool:
    update_chat = configured_update_channel()
    if not update_chat:
        logging.error("UPDATE_CHANNEL is not configured")
        return False
    try:
        # Membership check through Bot API: reliable after restarts.
        result = bot_api("getChatMember", {"chat_id": update_chat, "user_id": user_id})
        status = result.get("status", "")
        logging.info(f"JOIN CHECK user={user_id} chat={update_chat} status={status}")
        if status in ("creator", "administrator", "member"):
            return True
        # A restricted user can still be a member if Telegram says so.
        if status == "restricted":
            return bool(result.get("is_member", False))
        return False
    except Exception:
        logging.exception(
            f"Membership API check failed for user={user_id}, chat={update_chat}"
        )
        return False


async def get_bot_mode() -> str:
    setting = settings_collection.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    settings_collection.update_one(
        {"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True
    )
    return "public"


async def get_bot_username(client: Client):
    me = await client.get_me()
    return me.username


def batch_keyboard(batch_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ ADD MORE", callback_data=f"batch_add_{batch_id}")],
            [InlineKeyboardButton("🔗 GET FREE LINK", callback_data=f"batch_done_{batch_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"batch_cancel_{batch_id}")],
        ]
    )


def find_content_record(content_id):
    batch = batches_collection.find_one({"_id": content_id})
    if batch and batch.get("file_message_ids"):
        return "batch", batch

    old_file = files_collection.find_one({"_id": content_id})
    if old_file:
        return "single", old_file

    return None, None


def copy_content_to_user(user_id, content_id):
    kind, record = find_content_record(content_id)
    if not record:
        raise RuntimeError("File not found!")

    log_channel = configured_log_channel()
    if not log_channel:
        raise RuntimeError("LOG channel configured nahi hai.")

    if kind == "batch":
        for message_id in record["file_message_ids"]:
            bot_api(
                "copyMessage",
                {
                    "chat_id": user_id,
                    "from_chat_id": log_channel,
                    "message_id": int(message_id),
                },
            )
    else:
        bot_api(
            "copyMessage",
            {
                "chat_id": user_id,
                "from_chat_id": log_channel,
                "message_id": int(record["message_id"]),
            },
        )

# =========================
# /START
# =========================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    if len(message.command) <= 1:
        await message.reply(
            "👋 **Welcome to File Store Bot!**\n\n"
            "📁 Mujhe koi file bhejo aur main uska shareable link bana dunga.\n\n"
            "⚡ Fast • Easy • Secure"
        )
        return

    file_id_str = message.command[1]

    if not await is_user_member(client, message.from_user.id):
        join_button = InlineKeyboardButton("🔗 Join Update Channel", url=update_join_url())
        joined_button = InlineKeyboardButton(
            "✅ I Have Joined", callback_data=f"check_join_{file_id_str}"
        )
        keyboard = InlineKeyboardMarkup([[join_button], [joined_button]])

        await message.reply(
            f"👋 **Hello, {message.from_user.first_name}!**\n\n"
            "🔐 File access karne ke liye pehle hamara **Update Channel** join karein.\n\n"
            "Join karne ke baad **I Have Joined** button dabayein.",
            reply_markup=keyboard,
        )
        return

    content_kind, content_record = find_content_record(file_id_str)
    if not content_record:
        await message.reply("🤔 **File not found!** Ho sakta hai link galat ya expire ho gaya ho.")
        return

    try:
        copy_content_to_user(message.from_user.id, file_id_str)
    except Exception as e:
        logging.error(f"File delivery error: {e}")
        await message.reply(f"❌ File bhejte waqt error aa gaya.\n`{e}`")

# =========================
# FILE UPLOAD -> ONLY LOG/DB CHANNEL
# =========================
@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
async def file_handler(client: Client, message: Message):
    if message.from_user and message.from_user.is_bot:
        return

    bot_mode = await get_bot_mode()
    if bot_mode == "private" and message.from_user.id not in ADMINS:
        await message.reply("😔 **Sorry!** Abhi sirf Admins hi files upload kar sakte hain.")
        return

    status_msg = None

    try:
        log_channel = configured_log_channel()
        if not log_channel:
            raise RuntimeError("LOG channel configured nahi hai.")

        # Reuse an active batch for this user. If none exists, start a new one.
        batch = batches_collection.find_one(
            {"user_id": message.from_user.id, "active": True}
        )
        if not batch:
            batch_id = generate_random_string(12)
            batches_collection.insert_one(
                {
                    "_id": batch_id,
                    "user_id": message.from_user.id,
                    "file_message_ids": [],
                    "active": True,
                }
            )
            batch = batches_collection.find_one({"_id": batch_id})
        else:
            batch_id = batch["_id"]

        # Reuse the same status message so the buttons stay together.
        old_status_id = batch.get("status_message_id")
        if old_status_id:
            try:
                status_msg = await client.get_messages(message.chat.id, int(old_status_id))
            except Exception:
                status_msg = None

        if status_msg:
            await status_msg.edit_text("⏳ File batch mein add kar raha hu...")
        else:
            status_msg = await message.reply("⏳ File batch mein add kar raha hu...", quote=True)
            batches_collection.update_one(
                {"_id": batch_id},
                {"$set": {"status_message_id": status_msg.id}},
            )

        # DIRECT Bot API call. No Pyrogram peer lookup.
        result = bot_api(
            "forwardMessage",
            {
                "chat_id": log_channel,
                "from_chat_id": message.chat.id,
                "message_id": message.id,
            },
        )
        logged_message_id = int(result["message_id"])

        batches_collection.update_one(
            {"_id": batch_id},
            {"$push": {"file_message_ids": logged_message_id}},
        )

        updated_batch = batches_collection.find_one({"_id": batch_id})
        count = len(updated_batch.get("file_message_ids", []))

        await status_msg.edit_text(
            f"📦 **Batch Ready!**\n\n"
            f"📁 Files in this batch: **{count}**\n\n"
            "➕ Aur files ke liye **ADD MORE** dabayein.\n"
            "🔗 Sabhi files ka ek link banane ke liye **GET FREE LINK** dabayein.",
            reply_markup=batch_keyboard(batch_id),
        )
    except Exception as e:
        logging.exception("File handling error")
        if status_msg:
            await status_msg.edit_text(
                f"❌ **Error!**\n\nKuch galat ho gaya.\n`Details: {e}`"
            )
        else:
            await message.reply(f"❌ **Error!**\n\n`Details: {e}`")

# =========================
# /SETLOG
# =========================
@app.on_message(filters.command("setlog") & filters.private)
async def setlog_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ Aapke paas permission nahi hai.")
        return

    if not message.reply_to_message:
        await message.reply(
            "❌ Database channel ka koi message yahan forward karo, "
            "phir us forwarded message par reply karke /setlog bhejo."
        )
        return

    forwarded = message.reply_to_message
    chat = forwarded.forward_from_chat
    if not chat:
        await message.reply(
            "❌ Ye forwarded channel message nahi lag raha. Database channel se "
            "ek normal message forward karke us par /setlog reply karo."
        )
        return

    settings_collection.update_one(
        {"_id": "log_channel"},
        {"$set": {"chat_id": chat.id, "title": chat.title or "LOG Channel"}},
        upsert=True,
    )
    await message.reply(
        f"✅ **LOG channel configured!**\n\n"
        f"📁 **{chat.title or 'LOG Channel'}**\n"
        f"🆔 `{chat.id}`"
    )

# =========================
# /SETUPDATE (optional, admin)
# =========================
@app.on_message(filters.command("setupdate") & filters.private)
async def setupdate_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ Aapke paas permission nahi hai.")
        return

    if not message.reply_to_message:
        await message.reply(
            "❌ Update channel ka koi message yahan forward karo, "
            "phir us message par reply karke /setupdate bhejo."
        )
        return

    chat = message.reply_to_message.forward_from_chat
    if not chat:
        await message.reply("❌ Please Update channel ka normal forwarded message use karo.")
        return

    username = getattr(chat, "username", None)
    settings_collection.update_one(
        {"_id": "update_channel"},
        {"$set": {"chat_id": chat.id, "title": chat.title or "Update Channel", "username": username}},
        upsert=True,
    )
    await message.reply(
        f"✅ **Update channel configured!**\n\n"
        f"📢 **{chat.title or 'Update Channel'}**\n"
        f"🆔 `{chat.id}`\n"
        f"🔗 `{username or 'Username not available'}`"
    )

# =========================
# /SETINVITE (optional, admin)
# =========================
@app.on_message(filters.command("setinvite") & filters.private)
async def setinvite_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ Aapke paas permission nahi hai.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("http"):
        await message.reply("Usage: `/setinvite https://t.me/+xxxxx`")
        return
    settings_collection.update_one(
        {"_id": "update_invite"}, {"$set": {"url": parts[1].strip()}}, upsert=True
    )
    await message.reply("✅ Update invite link saved!")

# =========================
# /SETTINGS
# =========================
@app.on_message(filters.command("settings") & filters.private)
async def settings_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ Aapke paas permission nahi hai.")
        return

    current_mode = await get_bot_mode()
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")],
            [InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")],
        ]
    )
    await message.reply(
        f"⚙️ **Bot Settings**\n\n"
        f"File upload mode: **{current_mode.upper()}**\n\n"
        "**Public:** Koi bhi file bhej sakta hai.\n"
        "**Private:** Sirf admins file bhej sakte hain.\n\n"
        "Mode select karein:",
        reply_markup=keyboard,
    )

@app.on_callback_query(filters.regex(r"^set_mode_(public|private)$"))
async def set_mode_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in ADMINS:
        await callback_query.answer("Permission Denied!", show_alert=True)
        return
    new_mode = callback_query.data.split("_")[-1]
    settings_collection.update_one(
        {"_id": "bot_mode"}, {"$set": {"mode": new_mode}}, upsert=True
    )
    await callback_query.answer(f"Mode {new_mode.upper()} par set ho gaya!", show_alert=True)
    await callback_query.message.edit_text(
        f"⚙️ **Bot Settings**\n\nBot ka file upload mode ab **{new_mode.upper()}** hai."
    )

# =========================
# BATCH CONTROLS
# =========================
@app.on_callback_query(filters.regex(r"^batch_add_(.+)$"))
async def batch_add_callback(client: Client, callback_query: CallbackQuery):
    batch_id = callback_query.matches[0].group(1)
    batch = batches_collection.find_one(
        {"_id": batch_id, "user_id": callback_query.from_user.id, "active": True}
    )
    if not batch:
        await callback_query.answer("Ye batch active nahi hai.", show_alert=True)
        return

    count = len(batch.get("file_message_ids", []))
    await callback_query.answer("Ab next file bhejo 👍")
    await callback_query.message.edit_text(
        f"📦 **Batch mein {count} file(s) hain.**\n\n"
        "📤 Ab next file bhejo.\n"
        "🔗 Jab sab files bhej do, **GET FREE LINK** dabana.",
        reply_markup=batch_keyboard(batch_id),
    )


@app.on_callback_query(filters.regex(r"^batch_done_(.+)$"))
async def batch_done_callback(client: Client, callback_query: CallbackQuery):
    batch_id = callback_query.matches[0].group(1)
    batch = batches_collection.find_one(
        {"_id": batch_id, "user_id": callback_query.from_user.id}
    )
    if not batch:
        await callback_query.answer("Batch not found!", show_alert=True)
        return

    if batch.get("share_link"):
        await callback_query.answer("Link already generated.")
        await callback_query.message.edit_text(
            f"✅ **Batch Link Ready!**\n\n"
            f"📁 Files: **{len(batch.get('file_message_ids', []))}**\n\n"
            f"🔗 Your Link: `{batch['share_link']}`",
            disable_web_page_preview=True,
        )
        return

    file_ids = batch.get("file_message_ids", [])
    if not file_ids:
        await callback_query.answer("Pehle kam se kam 1 file bhejo.", show_alert=True)
        return

    try:
        bot_username = await get_bot_username(client)
        share_link = f"https://t.me/{bot_username}?start={batch_id}"
        batches_collection.update_one(
            {"_id": batch_id},
            {"$set": {"active": False, "share_link": share_link}},
        )
        await callback_query.answer("✅ Link generated!")
        await callback_query.message.edit_text(
            f"✅ **Link Generated Successfully!**\n\n"
            f"📁 Files in batch: **{len(file_ids)}**\n\n"
            f"🔗 Your Link: `{share_link}`",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logging.exception("Batch link generation error")
        await callback_query.answer("Link generate nahi hua.", show_alert=True)
        await callback_query.message.edit_text(f"❌ **Error!**\n`{e}`")


@app.on_callback_query(filters.regex(r"^batch_cancel_(.+)$"))
async def batch_cancel_callback(client: Client, callback_query: CallbackQuery):
    batch_id = callback_query.matches[0].group(1)
    result = batches_collection.delete_one(
        {"_id": batch_id, "user_id": callback_query.from_user.id, "active": True}
    )
    if not result.deleted_count:
        await callback_query.answer("Batch not found!", show_alert=True)
        return
    await callback_query.answer("Batch cancelled.")
    await callback_query.message.edit_text("❌ **Batch cancelled.**")

# =========================
# JOIN CHECK -> FILE DELIVERY
# =========================
@app.on_callback_query(filters.regex(r"^check_join_(.+)$"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    file_id_str = callback_query.matches[0].group(1)

    if not await is_user_member(client, user_id):
        await callback_query.answer(
            "Aapne abhi tak Update Channel join nahi kiya hai.", show_alert=True
        )
        return

    content_kind, content_record = find_content_record(file_id_str)
    if not content_record:
        await callback_query.answer("File not found!", show_alert=True)
        return

    try:
        await callback_query.answer("✅ Verified! File bhej raha hu...", show_alert=False)
        copy_content_to_user(user_id, file_id_str)
        await callback_query.message.delete()
    except Exception as e:
        logging.error(f"Callback file delivery error: {e}")
        await callback_query.message.edit_text(f"❌ File bhejte waqt error aa gaya.\n`{e}`")

# =========================
# STARTUP
# =========================
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("ADMIN_IDS is not set.")

    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Bot is starting...")
    app.run()
    logging.info("Bot has stopped.")
        
