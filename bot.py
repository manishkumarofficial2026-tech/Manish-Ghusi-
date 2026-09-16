import os
import logging
import random
import string
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import UserNotParticipant
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)
from pymongo import MongoClient
from flask import Flask
from threading import Thread

# =========================================================
# FLASK / RENDER KEEP-ALIVE
# =========================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return "Bot is alive!", 200


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


# =========================================================
# CONFIG
# =========================================================

logging.basicConfig(level=logging.INFO)
load_dotenv()

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Fallback IDs from Render Environment Variables
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "0"))
UPDATE_CHANNEL = int(os.environ.get("UPDATE_CHANNEL", "0"))

# Optional invite link for private UPDATE channel.
# Example:
# UPDATE_INVITE=https://t.me/+xxxxxxxx
UPDATE_INVITE = os.environ.get("UPDATE_INVITE", "")

ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [
    int(x.strip())
    for x in ADMIN_IDS_STR.split(",")
    if x.strip()
]


# =========================================================
# MONGODB
# =========================================================

try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client["file_link_bot"]

    files_collection = db["files"]
    settings_collection = db["settings"]

    logging.info("MongoDB Connected Successfully!")

except Exception as e:
    logging.error(f"Error connecting to MongoDB: {e}")
    raise


# =========================================================
# PYROGRAM
# =========================================================

app = Client(
    "FileLinkBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# =========================================================
# HELPERS
# =========================================================

def generate_random_string(length=6):
    return "".join(
        random.choices(
            string.ascii_lowercase + string.digits,
            k=length
        )
    )


def get_saved_channel(setting_id, fallback_id=0):
    """
    MongoDB se saved channel ID nikalta hai.
    Agar saved nahi hai to Render ENV ka fallback use karta hai.
    """
    setting = settings_collection.find_one({"_id": setting_id})

    if setting and setting.get("chat_id"):
        return int(setting["chat_id"])

    return int(fallback_id)


def get_saved_log_channel():
    return get_saved_channel(
        "log_channel",
        LOG_CHANNEL
    )


def get_saved_update_channel():
    return get_saved_channel(
        "update_channel",
        UPDATE_CHANNEL
    )


def get_update_invite():
    """
    Private UPDATE channel ke liye invite link.
    """
    setting = settings_collection.find_one(
        {"_id": "update_channel"}
    )

    if setting and setting.get("invite_link"):
        return setting["invite_link"]

    return UPDATE_INVITE


async def is_user_member(client: Client, user_id: int) -> bool:
    """
    Private UPDATE channel membership check.
    """
    update_channel = get_saved_update_channel()

    if not update_channel:
        logging.error("UPDATE channel is not configured.")
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
        logging.error(
            f"Error checking membership for {user_id}: {e}"
        )
        return False


async def get_bot_mode() -> str:
    setting = settings_collection.find_one(
        {"_id": "bot_mode"}
    )

    if setting:
        return setting.get("mode", "public")

    settings_collection.update_one(
        {"_id": "bot_mode"},
        {"$set": {"mode": "public"}},
        upsert=True
    )

    return "public"


# =========================================================
# /START
# =========================================================

@app.on_message(
    filters.command("start") & filters.private
)
async def start_handler(
    client: Client,
    message: Message
):

    if len(message.command) > 1:

        file_id_str = message.command[1]

        # ---------------------------------------------
        # UPDATE CHANNEL MEMBERSHIP
        # ---------------------------------------------

        if not await is_user_member(
            client,
            message.from_user.id
        ):

            invite_link = get_update_invite()

            if invite_link:
                join_button = InlineKeyboardButton(
                    "🔗 Join Channel",
                    url=invite_link
                )
            else:
                join_button = InlineKeyboardButton(
                    "🔗 Join Channel",
                    url="https://t.me/"
                )

            joined_button = InlineKeyboardButton(
                "✅ I Have Joined",
                callback_data=f"check_join_{file_id_str}"
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [join_button],
                    [joined_button]
                ]
            )

            await message.reply(
                f"👋 **Hello, {message.from_user.first_name}!**\n\n"
                "Ye file access karne ke liye, "
                "aapko hamara update channel join karna hoga.",
                reply_markup=keyboard
            )

            return

        # ---------------------------------------------
        # GET FILE RECORD
        # ---------------------------------------------

        file_record = files_collection.find_one(
            {"_id": file_id_str}
        )

        if not file_record:

            await message.reply(
                "🤔 File not found!\n\n"
                "Ho sakta hai link galat ya expire ho gaya ho."
            )

            return

        # ---------------------------------------------
        # SEND FILE FROM LOG CHANNEL
        # ---------------------------------------------

        try:

            log_channel = get_saved_log_channel()

            await client.copy_message(
                chat_id=message.from_user.id,
                from_chat_id=log_channel,
                message_id=file_record["message_id"]
            )

        except Exception as e:

            logging.error(
                f"File delivery error: {e}"
            )

            await message.reply(
                "❌ Sorry, file bhejte waqt "
                "ek error aa gaya.\n\n"
                f"`Error: {e}`"
            )

    else:

        await message.reply(
            "**Hello! Mai ek File-to-Link bot hu.**\n\n"
            "Mujhe koi bhi file bhejo, "
            "aur mai aapko uska shareable link dunga."
        )


# =========================================================
# FILE UPLOAD
# IMPORTANT:
# FILE SIRF LOG CHANNEL ME JAYEGI
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
async def file_handler(
    client: Client,
    message: Message
):

    bot_mode = await get_bot_mode()

    if (
        bot_mode == "private"
        and message.from_user.id not in ADMINS
    ):

        await message.reply(
            "😔 **Sorry!**\n\n"
            "Abhi sirf Admins hi files upload kar sakte hain."
        )

        return

    status_msg = await message.reply(
        "⏳ Please wait, file upload kar raha hu...",
        quote=True
    )

    try:

        # ---------------------------------------------
        # ONLY LOG CHANNEL
        # ---------------------------------------------

        log_channel = get_saved_log_channel()

        if not log_channel:
            raise Exception(
                "LOG channel configured nahi hai."
            )

        # File ONLY LOG channel me forward hogi.
        forwarded_message = await message.forward(
            log_channel
        )

        # ---------------------------------------------
        # SAVE FILE RECORD
        # ---------------------------------------------

        file_id_str = generate_random_string()

        files_collection.insert_one(
            {
                "_id": file_id_str,
                "message_id": forwarded_message.id,
                "log_channel": log_channel,
            }
        )

        # ---------------------------------------------
        # CREATE LINK
        # ---------------------------------------------

        me = await client.get_me()

        bot_username = me.username

        share_link = (
            f"https://t.me/"
            f"{bot_username}"
            f"?start={file_id_str}"
        )

        await status_msg.edit_text(
            "✅ **Link Generated Successfully!**\n\n"
            f"🔗 Your Link:\n`{share_link}`",
            disable_web_page_preview=True
        )

    except Exception as e:

        logging.error(
            f"File handling error: {e}"
        )

        await status_msg.edit_text(
            "❌ **Error!**\n\n"
            "Kuch galat ho gaya. "
            "Please try again.\n\n"
            f"`Details: {e}`"
        )


# =========================================================
# /SETLOG
# =========================================================

@app.on_message(
    filters.command("setlog") & filters.private
)
async def setlog_handler(
    client: Client,
    message: Message
):

    if message.from_user.id not in ADMINS:

        await message.reply(
            "❌ Aapke paas /setlog "
            "use karne ki permission nahi hai."
        )

        return

    source = None

    # ---------------------------------------------
    # FORWARDED MESSAGE REPLY
    # ---------------------------------------------

    if message.reply_to_message:

        source = (
            message.reply_to_message.forward_from_chat
        )

        if source is None:

            try:

                origin = (
                    message.reply_to_message.forward_origin
                )

                if origin and hasattr(origin, "chat"):
                    source = origin.chat

            except Exception:
                pass

    # ---------------------------------------------
    # /SETLOG ITSELF FORWARDED
    # ---------------------------------------------

    if (
        source is None
        and message.forward_from_chat
    ):

        source = message.forward_from_chat

    if source is None:

        await message.reply(
            "❌ LOG channel detect nahi hua.\n\n"
            "1. Private LOG channel ki koi message "
            "bot ko forward karo.\n"
            "2. Us forwarded message ke direct reply "
            "me `/setlog` bhejo.\n\n"
            "⚠️ Content Protection ON hai to forwarding "
            "allowed nahi hogi."
        )

        return

    chat_id = source.id

    if chat_id >= 0:

        await message.reply(
            "❌ Ye channel nahi lag raha.\n\n"
            "Private LOG **channel** ki message "
            "forward karo."
        )

        return

    try:

        chat = await client.get_chat(chat_id)

        settings_collection.update_one(
            {"_id": "log_channel"},
            {
                "$set": {
                    "chat_id": chat.id,
                    "title": chat.title or "LOG Channel",
                }
            },
            upsert=True
        )

        await message.reply(
            "✅ **LOG Channel Set Successfully!**\n\n"
            f"📢 Channel: "
            f"**{chat.title or 'Private Channel'}**\n"
            f"🆔 ID: `{chat.id}`\n\n"
            "Ab UPDATE channel set karne ke liye "
            "alag se `/setupdate` use karo."
        )

    except Exception as e:

        logging.error(
            f"/setlog error: {e}"
        )

        await message.reply(
            "❌ LOG channel detect ho gaya, "
            "lekin Pyrogram us peer ko resolve nahi "
            "kar paaya.\n\n"
            f"`Error: {e}`\n\n"
            "Check karo ki bot LOG channel ka member/admin hai."
        )


# =========================================================
# /SETUPDATE
# IMPORTANT:
# YE LOG CHANNEL KO TOUCH NAHI KAREGA
# =========================================================

@app.on_message(
    filters.command("setupdate") & filters.private
)
async def setupdate_handler(
    client: Client,
    message: Message
):

    if message.from_user.id not in ADMINS:

        await message.reply(
            "❌ Aapke paas /setupdate "
            "use karne ki permission nahi hai."
        )

        return

    source = None

    # ---------------------------------------------
    # FORWARDED UPDATE MESSAGE REPLY
    # ---------------------------------------------

    if message.reply_to_message:

        source = (
            message.reply_to_message.forward_from_chat
        )

        if source is None:

            try:

                origin = (
                    message.reply_to_message.forward_origin
                )

                if origin and hasattr(origin, "chat"):
                    source = origin.chat

            except Exception:
                pass

    # ---------------------------------------------
    # /SETUPDATE ITSELF FORWARDED
    # ---------------------------------------------

    if (
        source is None
        and message.forward_from_chat
    ):

        source = message.forward_from_chat

    if source is None:

        await message.reply(
            "❌ UPDATE channel detect nahi hua.\n\n"
            "1. Private UPDATE channel ki koi message "
            "bot ko forward karo.\n"
            "2. Us forwarded message ke direct reply "
            "me `/setupdate` bhejo.\n\n"
            "⚠️ Content Protection ON hai to forwarding "
            "allowed nahi hogi."
        )

        return

    chat_id = source.id

    if chat_id >= 0:

        await message.reply(
            "❌ Ye channel nahi lag raha.\n\n"
            "Private UPDATE **channel** ki message "
            "forward karo."
        )

        return

    try:

        chat = await client.get_chat(chat_id)

        # ---------------------------------------------
        # ONLY UPDATE CHANNEL SETTING
        # LOG CHANNEL SETTING KO TOUCH NAHI KAREGA
        # ---------------------------------------------

        settings_collection.update_one(
            {"_id": "update_channel"},
            {
                "$set": {
                    "chat_id": chat.id,
                    "title": chat.title or "UPDATE Channel",
                    "invite_link": UPDATE_INVITE,
                }
            },
            upsert=True
        )

        await message.reply(
            "✅ **UPDATE Channel Set Successfully!**\n\n"
            f"📢 Channel: "
            f"**{chat.title or 'Private Channel'}**\n"
            f"🆔 ID: `{chat.id}`\n\n"
            "Ab test link se membership check karo."
        )

    except Exception as e:

        logging.error(
            f"/setupdate error: {e}"
        )

        await message.reply(
            "❌ UPDATE channel detect ho gaya, "
            "lekin Pyrogram us peer ko resolve nahi "
            "kar paaya.\n\n"
            f"`Error: {e}`\n\n"
            "Check karo ki bot UPDATE channel ka member/admin hai."
        )


# =========================================================
# /SETINVITE
# OPTIONAL
# Private UPDATE channel ke invite link ko MongoDB me save
# =========================================================

@app.on_message(
    filters.command("setinvite") & filters.private
)
async def setinvite_handler(
    client: Client,
    message: Message
):

    if message.from_user.id not in ADMINS:

        await message.reply(
            "❌ Permission denied."
        )

        return

    if len(message.command) < 2:

        await message.reply(
            "Usage:\n"
            "`/setinvite https://t.me/+xxxxxxxx`"
        )

        return

    invite_link = message.command[1].strip()

    settings_collection.update_one(
        {"_id": "update_channel"},
        {
            "$set": {
                "invite_link": invite_link
            }
        },
        upsert=True
    )

    await message.reply(
        "✅ **UPDATE invite link saved!**\n\n"
        f"`{invite_link}`"
    )


# =========================================================
# /SETTINGS
# =========================================================

@app.on_message(
    filters.command("settings") & filters.private
)
async def settings_handler(
    client: Client,
    message: Message
):

    if message.from_user.id not in ADMINS:

        await message.reply(
            "❌ Aapke paas is command ko use karne "
            "ki permission nahi hai."
        )

        return

    current_mode = await get_bot_mode()

    public_button = InlineKeyboardButton(
        "🌍 Public (Anyone)",
        callback_data="set_mode_public"
    )

    private_button = InlineKeyboardButton(
        "🔒 Private (Admins Only)",
        callback_data="set_mode_private"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [public_button],
            [private_button]
        ]
    )

    await message.reply(
        f"⚙️ **Bot Settings**\n\n"
        f"Abhi bot ka file upload mode "
        f"**{current_mode.upper()}** hai.\n\n"
        "**Public:** Koi bhi file bhej kar "
        "link bana sakta hai.\n"
        "**Private:** Sirf admins hi file bhej sakte hain.\n\n"
        "Naya mode select karein:",
        reply_markup=keyboard
    )


# =========================================================
# MODE CALLBACK
# =========================================================

@app.on_callback_query(
    filters.regex(r"^set_mode_")
)
async def set_mode_callback(
    client: Client,
    callback_query: CallbackQuery
):

    if callback_query.from_user.id not in ADMINS:

        await callback_query.answer(
            "Permission Denied!",
            show_alert=True
        )

        return

    new_mode = (
        callback_query.data.split("_")[2]
    )

    settings_collection.update_one(
        {"_id": "bot_mode"},
        {"$set": {"mode": new_mode}},
        upsert=True
    )

    await callback_query.answer(
        f"Mode successfully "
        f"{new_mode.upper()} par set ho gaya hai!",
        show_alert=True
    )

    public_button = InlineKeyboardButton(
        "🌍 Public (Anyone)",
        callback_data="set_mode_public"
    )

    private_button = InlineKeyboardButton(
        "🔒 Private (Admins Only)",
        callback_data="set_mode_private"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [public_button],
            [private_button]
        ]
    )

    await callback_query.message.edit_text(
        f"⚙️ **Bot Settings**\n\n"
        f"✅ Bot ka file upload mode ab "
        f"**{new_mode.upper()}** hai.\n\n"
        "Naya mode select karein:",
        reply_markup=keyboard
    )


# =========================================================
# CHECK JOIN CALLBACK
# =========================================================

@app.on_callback_query(
    filters.regex(r"^check_join_")
)
async def check_join_callback(
    client: Client,
    callback_query: CallbackQuery
):

    user_id = callback_query.from_user.id

    file_id_str = (
        callback_query.data.split("_", 2)[2]
    )

    # ---------------------------------------------
    # CHECK UPDATE MEMBERSHIP
 
