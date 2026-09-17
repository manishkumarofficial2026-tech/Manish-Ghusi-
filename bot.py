import os
import logging
import random
import string
import json
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
    # For the public Update channel, prefer the username saved by /setupdate.
    # The username is safer than a stale/wrong numeric ID in Render ENV.
    setting = settings_collection.find_one({"_id": "update_channel"})
    username = setting.get("username") if setting else None
    if username:
        return tg_chat_id(username)

    if UPDATE_CHANNEL:
        return tg_chat_id(UPDATE_CHANNEL)

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

    file_record = files_collection.find_one({"_id": file_id_str})
    if not file_record:
        await message.reply("🤔 **File not found!** Ho sakta hai link galat ya expire ho gaya ho.")
        return

    try:
        log_channel = configured_log_channel()
        if not log_channel:
            raise RuntimeError("LOG channel configured nahi hai.")
        bot_api(
            "copyMessage",
            {
                "chat_id": message.from_user.id,
                "from_chat_id": log_channel,
                "message_id": int(file_record["message_id"]),
            },
        )
    except Exception as e:
        logging.error(f"File delivery error: {e}")
        await message.reply(f"❌ File bhejte waqt error aa gaya.\n`{e}`")

# =========================
# FILE UPLOAD -> ONLY LOG/DB CHANNEL
# =========================
@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
async def file_handler(client: Client, message: Message):
    bot_mode = await get_bot_mode()
    if bot_mode == "private" and message.from_user.id not in ADMINS:
        await message.reply("😔 **Sorry!** Abhi sirf Admins hi files upload kar sakte hain.")
        return

    status_msg = await message.reply("⏳ Please wait, file upload kar raha hu...", quote=True)

    try:
        log_channel = configured_log_channel()
        if not log_channel:
            raise RuntimeError("LOG channel configured nahi hai.")

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

        file_id_str = generate_random_string()
        files_collection.insert_one(
            {
                "_id": file_id_str,
                "message_id": logged_message_id,
                "log_channel": str(log_channel),
                "created_by": message.from_user.id,
            }
        )

        bot_username = await get_bot_username(client)
        share_link = f"https://t.me/{bot_username}?start={file_id_str}"

        await status_msg.edit_text(
            f"✅ **Link Generated Successfully!**\n\n"
            f"🔗 Your Link: `{share_link}`",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logging.exception("File handling error")
        await status_msg.edit_text(
            f"❌ **Error!**\n\nKuch galat ho gaya.\n`Details: {e}`"
        )

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

    file_record = files_collection.find_one({"_id": file_id_str})
    if not file_record:
        await callback_query.answer("File not found!", show_alert=True)
        return

    try:
        log_channel = configured_log_channel()
        if not log_channel:
            raise RuntimeError("LOG channel configured nahi hai.")

        await callback_query.answer("✅ Verified! File bhej raha hu...", show_alert=False)
        bot_api(
            "copyMessage",
            {
                "chat_id": user_id,
                "from_chat_id": log_channel,
                "message_id": int(file_record["message_id"]),
            },
        )
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
        
