import os
import logging
import random
import string
from threading import Thread

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

load_dotenv()

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Render Environment Variables
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "0"))
UPDATE_CHANNEL = int(os.environ.get("UPDATE_CHANNEL", "0"))

# Private UPDATE channel invite link
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

def generate_random_string(length=8):
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

    setting = settings_collection.find_one(
        {"_id": setting_id}
    )

    if setting and setting.get("chat_id"):
        try:
            return int(setting["chat_id"])
        except Exception:
            pass

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
    setting = settings_collection.find_one(
        {"_id": "update_channel"}
    )

    if setting and setting.get("invite_link"):
        return setting["invite_link"]

    return UPDATE_INVITE


def save_channel_setting(
    setting_id,
    chat_id,
    title,
    invite_link=None
):
    """
    Channel configuration MongoDB me save karta hai.
    """

    data = {
        "chat_id": int(chat_id),
        "title": title or "Private Channel",
    }

    if invite_link is not None:
        data["invite_link"] = invite_link

    settings_collection.update_one(
        {"_id": setting_id},
        {"$set": data},
        upsert=True
    )


async def is_user_member(
    client: Client,
    user_id: int
) -> bool:
    """
    UPDATE channel membership check.
    """

    update_channel = get_saved_update_channel()

    if not update_channel:
        logging.error(
            "UPDATE channel is not configured."
        )
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
            f"Membership check error: {e}"
        )

        return False


async def get_bot_mode() -> str:

    setting = settings_collection.find_one(
        {"_id": "bot_mode"}
    )

    if setting:
        return setting.get(
            "mode",
            "public"
        )

    settings_collection.update_one(
        {"_id": "bot_mode"},
        {
            "$set": {
                "mode": "public"
            }
        },
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

    # -----------------------------------------------------
    # START WITH FILE ID
    # -----------------------------------------------------

    if len(message.command) > 1:

        file_id_str = message.command[1]

        # -------------------------------------------------
        # CHECK UPDATE CHANNEL MEMBERSHIP
        # -------------------------------------------------

        if not await is_user_member(
            client,
            message.from_user.id
        ):

            invite_link = get_update_invite()

            buttons = []

            if invite_link:

                buttons.append(
                    [
                        InlineKeyboardButton(
                            "🔗 Join Channel",
                            url=invite_link
                        )
                    ]
                )

            buttons.append(
                [
                    InlineKeyboardButton(
                        "✅ I Have Joined",
                        callback_data=(
                            f"check_join_{file_id_str}"
                        )
                    )
                ]
            )

            keyboard = InlineKeyboardMarkup(
                buttons
            )

            await message.reply(
                f"👋 **Hello, "
                f"{message.from_user.first_name}!**\n\n"
                "Ye file access karne ke liye "
                "aapko hamara update channel "
                "join karna hoga.",
                reply_markup=keyboard
            )

            return

        # -------------------------------------------------
        # GET FILE RECORD
        # -------------------------------------------------

        file_record = files_collection.find_one(
            {"_id": file_id_str}
        )

        if not file_record:

            await message.reply(
                "🤔 **File not found!**\n\n"
                "Ho sakta hai link galat ya "
                "expire ho gaya ho."
            )

            return

        # -------------------------------------------------
        # SEND FILE FROM LOG CHANNEL ONLY
        # -------------------------------------------------

        try:

            log_channel = int(
                file_record.get(
                    "log_channel",
                    get_saved_log_channel()
                )
            )

            message_id = int(
                file_record["message_id"]
            )

            await client.copy_message(
                chat_id=message.from_user.id,
                from_chat_id=log_channel,
                message_id=message_id
            )

        except Exception as e:

            logging.error(
                f"File delivery error: {e}"
            )

            await message.reply(
                "❌ **Sorry!** File bhejte waqt "
                "error aa gaya.\n\n"
                f"`Error: {e}`"
            )

        return

    # -----------------------------------------------------
    # NORMAL /START
    # -----------------------------------------------------

    await message.reply(
        "👋 **Hello! Mai ek File-to-Link bot hu.**\n\n"
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
            "Abhi sirf Admins hi files "
            "upload kar sakte hain."
        )

        return

    status_msg = await message.reply(
        "⏳ Please wait, file upload kar raha hu...",
        quote=True
    )

    try:

        # -------------------------------------------------
        # LOG CHANNEL ONLY
        # -------------------------------------------------

        log_channel = get_saved_log_channel()

        if not log_channel:

            raise Exception(
                "LOG channel configured nahi hai."
            )

        # -------------------------------------------------
        # FILE ONLY GOES TO LOG CHANNEL
        # -------------------------------------------------

        forwarded_message = await message.forward(
            log_channel
        )

        # -------------------------------------------------
        # GENERATE UNIQUE FILE ID
        # -------------------------------------------------

        file_id_str = generate_random_string()

        # Make sure ID is unique
        while files_collection.find_one(
            {"_id": file_id_str}
        ):

            file_id_str = generate_random_string()

        # -------------------------------------------------
        # SAVE DATABASE RECORD
        # -------------------------------------------------

        files_collection.insert_one(
            {
                "_id": file_id_str,
                "message_id": forwarded_message.id,
                "log_channel": log_channel,
            }
        )

        # -------------------------------------------------
        # CREATE SHARE LINK
        # -------------------------------------------------

        me = await client.get_me()

        if not me.username:

            raise Exception(
                "Bot username available nahi hai."
            )

        share_link = (
            f"https://t.me/"
            f"{me.username}"
            f"?start={file_id_str}"
        )

        await status_msg.edit_text(
            "✅ **Link Generated Successfully!**\n\n"
            f"🔗 Your Link:\n`{share_link}`",
            disable_web_page_preview=True
        )

    except Exception as e:

        logging.exception(
            "File handling error"
        )

        await status_msg.edit_text(
            "❌ **Error!**\n\n"
            "Kuch galat ho gaya. "
            "Please try again.\n\n"
            f"`Details: {e}`"
        )


# =========================================================
# CHANNEL DETECTION HELPER
# =========================================================

def get_forwarded_channel(message: Message):
    """
    Forwarded channel message se channel information
    nikalta hai.

    IMPORTANT:
    Yahan get_chat() use nahi kiya gaya.
    Isse private channel ke numeric peer ko dobara
    unnecessarily resolve karne ki problem kam hoti hai.
    """

    source = None

    if message.reply_to_message:

        source = (
            message.reply_to_message.forward_from_chat
        )

        if source is None:

            try:

                origin = (
                    message.reply_to_message.forward_origin
                )

                if origin and hasattr(
                    origin,
                    "chat"
                ):
                    source = origin.chat

            except Exception:
                pass

    if (
        source is None
        and message.forward_from_chat
    ):

        source = message.forward_from_chat

    return source


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

    source = get_forwarded_channel(message)

    if source is None:

        await message.reply(
            "❌ **LOG channel detect nahi hua.**\n\n"
            "1. Private LOG/Database channel ki "
            "koi message bot ko forward karo.\n"
            "2. Us forwarded message ke direct reply "
            "me `/setlog` bhejo.\n\n"
            "⚠️ Agar Content Protection ON hai, "
            "forwarding allowed nahi ho sakti."
        )

        return

    chat_id = int(source.id)

    if chat_id >= 0:

        await message.reply(
            "❌ Ye channel nahi lag raha.\n\n"
            "Private LOG **channel** ki message "
            "forward karo."
        )

        return

    try:

        title = (
            getattr(source, "title", None)
            or "LOG Channel"
        )

        # -------------------------------------------------
        # IMPORTANT:
        # get_chat(chat_id) intentionally removed.
        # -------------------------------------------------

        save_channel_setting(
            "log_channel",
            chat_id,
            title
        )

        await message.reply(
            "✅ **LOG Channel Set Successfully!**\n\n"
            f"📢 Channel: **{title}**\n"
            f"🆔 ID: `{chat_id}`\n\n"
            "Ab uploaded files isi LOG channel "
            "me jayengi.\n\n"
            "UPDATE channel ke liye "
            "`/setupdate` use karo."
        )

    except Exception as e:

        logging.exception(
            "/setlog error"
        )

        await message.reply(
            "❌ LOG channel save nahi ho paaya.\n\n"
            f"`Error: {e}`"
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

    source = get_forwarded_channel(message)

    if source is None:

        await message.reply(
            "❌ **UPDATE channel detect nahi hua.**\n\n"
            "1. Private UPDATE channel ki "
            "koi message bot ko forward karo.\n"
            "2. Us forwarded message ke direct reply "
            "me `/setupdate` bhejo."
        )

        return

    chat_id = int(source.id)

    if chat_id >= 0:

        await message.reply(
            "❌ Ye channel nahi lag raha.\n\n"
            "Private UPDATE **channel** ki message "
            "forward karo."
        )

        return

    try:

        title = (
            getattr(source, "title", None)
            or "UPDATE Channel"
        )

        # -------------------------------------------------
        # ONLY UPDATE SETTING
        # LOG SETTING IS NOT TOUCHED
        # -------------------------------------------------

        save_channel_setting(
            "update_channel",
            chat_id,
            title,
            get_update_invite()
        )

        await message.reply(
            "✅ **UPDATE Channel Set Successfully!**\n\n"
            f"📢 Channel: **{title}**\n"
            f"🆔 ID: `{chat_id}`\n\n"
            "Ab is channel ko membership "
            "check ke liye use kiya jayega.\n\n"
            "⚠️ Uploaded files yahan nahi jayengi."
        )

    except Exception as e:

        logging.exception(
            "/setupdate error"
        )

        await message.reply(
            "❌ UPDATE channel save nahi ho paaya.\n\n"
            f"`Error: {e}`"
        )


# =========================================================
# /SETINVITE
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

    invite_link = (
        message.command[1].strip()
    )

    if not invite_link.startswith(
        "https://t.me/"
    ):

        await message.reply(
            "❌ Valid Telegram invite link do.\n\n"
            "Example:\n"
            "`https://t.me/+xxxxxxxx`"
        )

        return

    # -----------------------------------------------------
    # Only invite link update.
    # Existing UPDATE channel ID remains untouched.
    # -----------------------------------------------------

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
            "❌ Aapke paas is command ko use "
            "karne ki permission nahi hai."
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
        "**Private:** Sirf admins hi file bhej "
        "sakte hain.\n\n"
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

    if new_mode not in (
        "public",
        "private"
    ):

        await callback_query.answer(
            "In
