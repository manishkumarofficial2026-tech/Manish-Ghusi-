import os
import logging
import random
import string
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pymongo import MongoClient
from flask import Flask
from threading import Thread


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MONGO_URI = os.environ.get("MONGO_URI", "")

LOG_CHANNEL_ENV = os.environ.get("LOG_CHANNEL", "")
UPDATE_CHANNEL_ENV = os.environ.get("UPDATE_CHANNEL", "")
UPDATE_INVITE_ENV = os.environ.get("UPDATE_INVITE", "")

ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")

ADMINS = [
    int(x.strip())
    for x in ADMIN_IDS_STR.split(",")
    if x.strip().isdigit()
]


# =========================================================
# HELPERS FOR CHANNEL IDs
# =========================================================

def parse_chat_id(value):
    """
    Numeric Telegram channel ID ko int mein convert karta hai.
    Username ko string hi rakhta hai.
    """
    if not value:
        return None

    value = str(value).strip()

    try:
        return int(value)
    except ValueError:
        return value


DEFAULT_LOG_CHANNEL = parse_chat_id(LOG_CHANNEL_ENV)
DEFAULT_UPDATE_CHANNEL = parse_chat_id(UPDATE_CHANNEL_ENV)


# =========================================================
# FLASK SERVER - RENDER KEEP ALIVE
# =========================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return "Bot is alive!", 200


def run_flask():
    port = int(os.environ.get("PORT", "8080"))

    flask_app.run(
        host="0.0.0.0",
        port=port
    )


# =========================================================
# MONGODB
# =========================================================

try:
    mongo_client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=10000
    )

    # Connection test
    mongo_client.admin.command("ping")

    db = mongo_client["file_link_bot"]

    files_collection = db["files"]
    settings_collection = db["settings"]

    logger.info("MongoDB Connected Successfully!")

except Exception as e:
    logger.exception("MongoDB connection failed: %s", e)
    raise


# =========================================================
# PYROGRAM
# =========================================================

app = Client(
    "FileLinkBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)


# =========================================================
# GENERAL HELPERS
# =========================================================

def generate_random_string(length=8):
    return "".join(
        random.choices(
            string.ascii_lowercase + string.digits,
            k=length
        )
    )


def is_admin(user_id):
    return user_id in ADMINS


def get_setting(key, default=None):
    data = settings_collection.find_one({"_id": key})

    if data:
        return data.get("value", default)

    return default


def set_setting(key, value):
    settings_collection.update_one(
        {"_id": key},
        {
            "$set": {
                "value": value
            }
        },
        upsert=True
    )


def get_log_channel():
    return get_setting(
        "log_channel",
        DEFAULT_LOG_CHANNEL
    )


def get_update_channel():
    return get_setting(
        "update_channel",
        DEFAULT_UPDATE_CHANNEL
    )


def get_update_invite():
    return get_setting(
        "update_invite",
        UPDATE_INVITE_ENV
    )


async def get_forwarded_chat(message):
    """
    Forwarded channel message se original channel identify karta hai.
    """

    try:
        if getattr(message, "forward_from_chat", None):
            return message.forward_from_chat

    except Exception:
        pass

    try:
        origin = getattr(message, "forward_origin", None)

        if origin:
            chat = getattr(origin, "chat", None)

            if chat:
                return chat

    except Exception:
        pass

    return None


# =========================================================
# MEMBERSHIP CHECK
# =========================================================

async def is_user_member(client, user_id):
    update_channel = get_update_channel()

    if not update_channel:
        logger.error("UPDATE_CHANNEL is not configured.")
        return False

    try:
        await client.get_chat_member(
            chat_id=update_channel,
            user_id=user_id
        )

        return True

    except UserNotParticipant:
        return False

    except Exception as e:
        logger.error(
            "Membership check failed for %s: %s",
            user_id,
            e
        )

        return False


# =========================================================
# BOT MODE
# =========================================================

def get_bot_mode():
    mode = get_setting(
        "bot_mode",
        "public"
    )

    if mode not in ("public", "private"):
        mode = "public"

    return mode


# =========================================================
# START COMMAND
# =========================================================

@app.on_message(
    filters.command("start") & filters.private
)
async def start_handler(client, message):

    user = message.from_user

    if not user:
        return

    # -----------------------------------------------------
    # FILE LINK
    # -----------------------------------------------------

    if len(message.command) > 1:

        file_token = message.command[1]

        file_record = files_collection.find_one(
            {"_id": file_token}
        )

        if not file_record:
            await message.reply(
                "🤔 **File not found!**\n\n"
                "Ho sakta hai link galat ya expire ho gaya ho."
            )
            return

        # -------------------------------------------------
        # CHECK UPDATE CHANNEL
        # -------------------------------------------------

        joined = await is_user_member(
            client,
            user.id
        )

        if not joined:

            invite_link = get_update_invite()

            if not invite_link:
                await message.reply(
                    "❌ Update channel invite link configured nahi hai.\n"
                    "Admin ko `/setinvite` set karna hoga."
                )
                return

            join_button = InlineKeyboardButton(
                "🔗 Join Update Channel",
                url=invite_link
            )

            joined_button = InlineKeyboardButton(
                "✅ I Have Joined",
                callback_data=f"check_join:{file_token}"
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [join_button],
                    [joined_button]
                ]
            )

            await message.reply(
                f"👋 **Hello {user.first_name}!**\n\n"
                "📢 File access karne ke liye pehle "
                "hamara update channel join karein.\n\n"
                "Join karne ke baad **I Have Joined** button dabayein.",
                reply_markup=keyboard
            )

            return

        # -------------------------------------------------
        # USER ALREADY JOINED
        # -------------------------------------------------

        log_channel = get_log_channel()

        if not log_channel:
            await message.reply(
                "❌ LOG/Database channel configured nahi hai."
            )
            return

        try:

            await client.copy_message(
                chat_id=user.id,
                from_chat_id=log_channel,
                message_id=file_record["message_id"]
            )

        except Exception as e:

            logger.exception(
                "File send failed: %s",
                e
            )

            await message.reply(
                "❌ File bhejte waqt error aa gaya.\n\n"
                f"`{e}`"
            )

        return

    # -----------------------------------------------------
    # NORMAL START
    # -----------------------------------------------------

    await message.reply(
        "👋 **Hello!**\n\n"
        "Mai ek File-to-Link bot hoon.\n\n"
        "Mujhe koi file bhejo aur mai uska "
        "shareable link bana dunga."
    )


# =========================================================
# FILE HANDLER
# =========================================================

@app.on_message(
    filters.private
    & (
        filters.document
        | filters.video
        | filters.photo
        | filters.audio
    )
)
async def file_handler(client, message):

    if not message.from_user:
        return

    # -----------------------------------------------------
    # PRIVATE MODE
    # -----------------------------------------------------

    mode = get_bot_mode()

    if mode == "private" and not is_admin(
        message.from_user.id
    ):

        await message.reply(
            "😔 **Sorry!**\n\n"
            "Abhi sirf admins files upload kar sakte hain."
        )

        return

    # -----------------------------------------------------
    # LOG CHANNEL
    # -----------------------------------------------------

    log_channel = get_log_channel()

    if not log_channel:

        await message.reply(
            "❌ LOG/Database channel configured nahi hai.\n\n"
            "Admin pehle `/setlog` kare."
        )

        return

    status_msg = await message.reply(
        "⏳ Please wait...\n\n"
        "File Database channel mein save kar raha hoon."
    )

    try:

        # IMPORTANT:
        # File ONLY LOG / DATABASE channel mein jayegi.
        # UPDATE channel mein nahi.
        forwarded_message = await message.forward(
            log_channel
        )

        file_token = generate_random_string()

        files_collection.insert_one(
            {
                "_id": file_token,
                "message_id": forwarded_message.id,
                "log_channel": log_channel
            }
        )

        me = await client.get_me()

        share_link = (
            f"https://t.me/{me.username}"
            f"?start={file_token}"
        )

        await status_msg.edit_text(
            "✅ **Link Generated Successfully!**\n\n"
            f"🔗 `{share_link}`",
            disable_web_page_preview=True
        )

    except Exception as e:

        logger.exception(
            "File handling error: %s",
            e
        )

        await status_msg.edit_text(
            "❌ **File save nahi ho payi.**\n\n"
            "Agar ye LOG channel ka error hai to "
            "admin `/setlog` dobara kare.\n\n"
            f"`{e}`"
        )


# =========================================================
# /SETLOG
# =========================================================

@app.on_message(
    filters.command("setlog") & filters.private
)
async def setlog_handler(client, message):

    if not message.from_user or not is_admin(
        message.from_user.id
    ):
        await message.reply(
            "❌ Permission denied."
        )
        return

    forwarded_chat = await get_forwarded_chat(
        message
    )

    if not forwarded_chat:

        await message.reply(
            "❌ **LOG channel detect nahi hua.**\n\n"
            "Database/LOG channel ka koi message "
            "bot ko **forward** karke `/setlog` command use karein."
        )

        return

    channel_id = forwarded_chat.id
    channel_title = getattr(
        forwarded_chat,
        "title",
        "Unknown"
    )

    set_setting(
        "log_channel",
        channel_id
    )

    await message.reply(
        "✅ **LOG Channel Set Successfully!**\n\n"
        f"📁 **Name:** {channel_title}\n"
        f"🆔 **ID:** `{channel_id}`\n\n"
        "Ab uploaded files isi Database channel mein jayengi."
    )


# =========================================================
# /SETUPDATE
# =========================================================

@app.on_message(
    filters.command("setupdate") & filters.private
)
async def setupdate_handler(client, message):

    if not message.from_user or not is_admin(
        message.from_user.id
    ):
        await message.reply(
            "❌ Permission denied."
        )
        return

    forwarded_chat = await get_forwarded_chat(
        message
    )

    if not forwarded_chat:

        await message.reply(
            "❌ **Update channel detect nahi hua.**\n\n"
            "Update channel ka koi message "
            "bot ko forward karke `/setupdate` karein."
        )

        return

    channel_id = forwarded_chat.id
    channel_title = getattr(
        forwarded_chat,
        "title",
        "Unknown"
    )

    set_setting(
        "update_channel",
        channel_id
    )

    await message.reply(
        "✅ **UPDATE Channel Set Successfully!**\n\n"
        f"📢 **Name:** {channel_title}\n"
        f"🆔 **ID:** `{channel_id}`\n\n"
        "Ab membership isi channel mein check hogi."
    )


# =========================================================
# /SETINVITE
# =========================================================

@app.on_message(
    filters.command("setinvite") & filters.private
)
async def setinvite_handler(client, message):

    if not message.from_user or not is_admin(
        message.from_user.id
    ):
        await message.reply(
            "❌ Permission denied."
        )
        return

    if len(message.command) < 2:

        await message.reply(
            "❌ Invite link missing.\n\n"
            "Example:\n"
            "`/setinvite https://t.me/+xxxxxxxx`"
        )

        return

    invite_link = message.command[1].strip()

    if not (
        invite_link.startswith("https://t.me/")
        or invite_link.startswith("http://t.me/")
    ):

        await message.reply(
            "❌ Valid Telegram invite link dein."
        )

        return

    set_setting(
        "update_invite",
        invite_link
    )

    await message.reply(
        "✅ **Update Invite Link Saved!**\n\n"
        f"`{invite_link}`"
    )


# =========================================================
# /SETTINGS
# =========================================================

@app.on_message(
    filters.command("settings") & filters.private
)
async def settings_handler(client, message):

    if not message.from_user or not is_admin(
        message.from_user.id
    ):
        await message.reply(
            "❌ Aapke paas permission nahi hai."
        )
        return

    mode = get_bot_mode()
    log_channel = get_log_channel()
    update_channel = get_update_channel()
    invite = get_update_invite()

    text = (
        "⚙️ **BOT SETTINGS**\n\n"
        f"📁 **LOG Channel:** `{log_channel}`\n"
        f"📢 **UPDATE Channel:** `{update_channel}`\n"
        f"🔗 **Invite:** "
        f"{'Set ✅' if invite else 'Not Set ❌'}\n"
        f"🔐 **Upload Mode:** `{mode.upper()}`\n\n"
        "Commands:\n"
        "`/setlog` — Database channel set\n"
        "`/setupdate` — Update channel set\n"
        "`/setinvite LINK` — Invite link set"
    )

    public_button = InlineKeyboardButton(
        "🌍 Public",
        callback_data="set_mode_public"
    )

    private_button = InlineKeyboardButton(
        "🔒 Private",
        callback_data="set_mode_private"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [public_button, private_button]
        ]
    )

    await message.reply(
        text,
        reply_markup=keyboard
    )


# =========================================================
# MODE CALLBACK
# =========================================================

@app.on_callback_query(
    filters.regex(r"^set_mode_(public|private)$")
)
async def set_mode_callback(
    client,
    callback_query
):

    if not is_admin(
        callback_query.from_user.id
    ):

        await callback_query.answer(
            "Permission Denied!",
            show_alert=True
        )

        return

    new_mode = callback_query.data.split(
        "_"
    )[-1]

    set_setting(
        "bot_mode",
        new_mode
    )

    await callback_query.answer(
        f"Mode {new_mode.upper()} set ho gaya!",
        show_alert=True
    )

    public_button = InlineKeyboardButton(
        "🌍 Public",
        callback_data="set_mode_public"
    )

    private_button = InlineKeyboardButton(
        "🔒 Private",
        callback_data="set_mode_private"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [public_button, private_button]
        ]
    )

    await callback_query.message.edit_text(
        "⚙️ **BOT SETTINGS**\n\n"
        f"✅ Upload mode: **{new_mode.upper()}**\n\n"
        "Public = koi bhi file upload kar sakta hai.\n"
        "Private = sirf admins upload kar sakte hain.",
        reply_markup=keyboard
    )


# =========================================================
# CHECK JOIN CALLBACK
# =========================================================

@app.on_callback_query(
    filters.regex(r"^check_join:")
)
async def check_join_callback(
    client,
    callback_query
):

    user_id = callback_query.from_user.id

    file_token = callback_query.data.split(
        ":",
        1
    )[1]

    # -----------------------------------------------------
    # CHECK MEMBERSHIP AGAIN
    # -----------------------------------------------------

    joined = await is_user_member(
        client,
        user_id
    )

    if not joined:

        await callback_query.answer(
            "❌ Aapne abhi channel join nahi kiya.",
            show_alert=True
        )

        return

    # -----------------------------------------------------
    # GET FILE
    # -----------------------------------------------------

    file_record = files_collection.find_one(
        {"_id": file_token}
    )

    if not file_record:

        await callback_query.answer(
            "❌ File nahi mili.",
            show_alert=True
        )

        return

    log_channel = get_log_channel()

    if not log_channel:

        await callback_query.answer(
            "❌ LOG channel configured nahi hai.",
            show_alert=True
        )

        return

    # -----------------------------------------------------
    # SEND FILE
    # -----------------------------------------------------

    try:

        await callback_query.answer(
            "✅ Membership verified! File bhej raha hoon..."
        )

        await client.copy_message(
            chat_id=user_id,
            from_chat_id=log_channel,
            message_id=file_record["message_id"]
        )

        try:
            await callback_query.message.delete()
        except Exception:
            pass

    except Exception as e:

        logger.exception(
            "File send after join failed: %s",
            e
        )

        try:

            await callback_query.message.edit_text(
                "❌ File bhejte waqt error aa gaya.\n\n"
                f"`{e}`"
            )

        except Exception:
            pass


# =========================================================
# ERROR HANDLER
# =========================================================

@app.on_callback_query()
async def generic_callback_answer(
    client,
    callback_query
):
    """
    Unknown callback ko silently hang hone se bachata hai.
    """

    try:
        await callback_query.answer()
  
