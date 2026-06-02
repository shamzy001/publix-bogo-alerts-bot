# bot.py — Publix BOGO Telegram Bot

import asyncio
import logging
import os
import requests
from collections import defaultdict
from datetime import time
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import NetworkError, TimedOut
from dotenv import load_dotenv
from scraper import (
    load_users, save_users, init_db,
    get_bogo_deals, find_matching_deals,
    send_deals_to_user, DEFAULT_STORE_ID
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress noisy httpx request logs (they also expose the bot token in plain text)
logging.getLogger("httpx").setLevel(logging.WARNING)
# Suppress WebDriver Manager download chatter
logging.getLogger("WDM").setLevel(logging.WARNING)

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

HELP_TEXT = (
    "🤖 Publix BOGO Alert Bot\n"
    "─────────────────────────────────────\n"
    "Tap a button below, or type a command:\n\n"
    "/add <item>       — add to watch list\n"
    "/remove <item>    — remove from watch list\n"
    "/store <id>       — set your Publix store\n"
    "/findstore <zip>  — find nearby store IDs\n"
    "/scan             — scan Publix now\n"
    "/list             — show your watch list\n"
    "/clear            — clear your watch list\n"
    "/stop             — unregister\n"
    "/help             — show this message"
)

SETUP_STEP1 = (
    "Let's get you set up! First, find your nearest Publix store.\n\n"
    "Send me your zip code like this:\n"
    "/findstore <zipcode>\n\n"
    "Example: /findstore 33458"
)

SETUP_STEP2 = (
    "Great! Now set your store using the ID from the list above:\n"
    "/store <store_id>\n\n"
    "Example: /store 2500976"
)

SETUP_STEP3 = (
    "Almost there! Now add the items you want to be alerted about:\n"
    "/add <item>\n\n"
    "Example:\n"
    "/add beer\n"
    "/add hummus\n"
    "/add pasta\n\n"
    "You can add multiple at once too:\n"
    "/add beer, hummus, pasta\n\n"
    "Once you're set up, you'll automatically receive your matching deals "
    "every Thursday at 3pm 📅\n"
    "You can also check anytime with /scan 🎉"
)


# -----------------------------------------------------
# KEYBOARD BUILDERS
# -----------------------------------------------------

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Scan Now", callback_data="scan"),
            InlineKeyboardButton("📋 My List",  callback_data="list"),
        ],
        [
            InlineKeyboardButton("➕ Add Item",   callback_data="add_prompt"),
            InlineKeyboardButton("❓ Help",        callback_data="help"),
        ],
        [InlineKeyboardButton("🚫 Stop Alerts", callback_data="stop_confirm")],
    ])


def list_keyboard(keywords):
    """Watch list with a ✕ remove button per keyword, plus a back-to-menu button."""
    rows = [
        [InlineKeyboardButton(f"✕  {k}", callback_data=f"remove:{k[:50]}")]
        for k in keywords
    ]
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def stop_confirm_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, stop", callback_data="stop_yes"),
        InlineKeyboardButton("❌ Cancel",    callback_data="stop_no"),
    ]])


def admin_approval_keyboard(target_chat_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{target_chat_id}"),
        InlineKeyboardButton("🚫 Deny",   callback_data=f"deny:{target_chat_id}"),
    ]])


# -----------------------------------------------------
# HELPERS
# -----------------------------------------------------

def is_registered(users, chat_id):
    """Returns True only for fully approved/active users."""
    return chat_id in users and users[chat_id].get("status", "active") == "active"


def is_admin(chat_id):
    return str(chat_id) == str(ADMIN_CHAT_ID)


# -----------------------------------------------------
# COMMAND HANDLERS
# -----------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    name = update.effective_user.first_name
    users = load_users()

    # Already active
    if is_registered(users, chat_id):
        store_id = users[chat_id].get("store_id", None)
        keywords = users[chat_id].get("keywords", [])
        await update.effective_message.reply_text(
            f"👋 Welcome back {name}!\n"
            f"🏪 Store: {store_id or 'not set'}\n"
            f"📋 Watch list: {len(keywords)} item(s)",
            reply_markup=main_menu_keyboard()
        )
        return

    # Already pending
    if chat_id in users and users[chat_id].get("status") == "pending":
        await update.effective_message.reply_text(
            "⏳ Your registration request is still pending admin approval. "
            "You'll be notified once you're approved."
        )
        return

    # New user — put in pending and notify admin
    users[chat_id] = {"name": name, "keywords": [], "status": "pending"}
    save_users(users)

    await update.effective_message.reply_text(
        f"👋 Hi {name}! Your registration request has been sent to the admin.\n"
        "You'll receive a message here once you're approved."
    )

    if ADMIN_CHAT_ID:
        from scraper import send_telegram
        from telegram import Bot
        bot = context.bot
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🔔 New registration request:\n"
                f"Name: {name}\n"
                f"Chat ID: {chat_id}"
            ),
            reply_markup=admin_approval_keyboard(chat_id)
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, reply_markup=main_menu_keyboard())


async def add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("Please send /start first to register.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /add <keyword>\n"
            "Multiple: /add beer, hummus, pasta"
        )
        return

    # Split on commas to support multiple keywords at once
    raw = " ".join(context.args)
    keywords_to_add = [k.strip() for k in raw.split(",") if k.strip()]

    existing = [k.lower() for k in users[chat_id]["keywords"]]
    added, skipped = [], []

    for keyword in keywords_to_add:
        if keyword.lower() not in existing:
            users[chat_id]["keywords"].append(keyword)
            existing.append(keyword.lower())
            added.append(keyword)
        else:
            skipped.append(keyword)

    save_users(users)

    msg = ""
    if added:
        msg += "✅ Added: " + ", ".join(f"'{k}'" for k in added)
    if skipped:
        msg += ("\n" if msg else "") + "⚠️ Already on list: " + ", ".join(f"'{k}'" for k in skipped)

    await update.effective_message.reply_text(msg, reply_markup=main_menu_keyboard())


async def remove_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("Please send /start first to register.")
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /remove <keyword>\nExample: /remove hummus")
        return

    keyword = " ".join(context.args)
    match = next((k for k in users[chat_id]["keywords"] if k.lower() == keyword.lower()), None)

    if match:
        users[chat_id]["keywords"].remove(match)
        save_users(users)
        await update.effective_message.reply_text(
            f"✅ Removed '{match}' from your watch list.",
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.effective_message.reply_text(f"'{keyword}' wasn't on your watch list.")


async def list_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("Please send /start first to register.")
        return

    keywords = users[chat_id].get("keywords", [])
    store_id = users[chat_id].get("store_id", DEFAULT_STORE_ID)

    if not keywords:
        await update.effective_message.reply_text(
            f"🏪 Store ID: {store_id}\n\n"
            "Your watch list is empty.\nUse /add <keyword> to add items.",
            reply_markup=main_menu_keyboard()
        )
        return

    msg = f"🏪 Store ID: {store_id}\n\n📋 Watch list — tap an item to remove it:"
    await update.effective_message.reply_text(msg, reply_markup=list_keyboard(keywords))


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("You're not registered — nothing to remove.")
        return

    await update.effective_message.reply_text(
        "⚠️ Are you sure you want to stop?\n\n"
        "This will remove you from all future alerts and delete your watch list and store settings.",
        reply_markup=stop_confirm_keyboard()
    )


async def confirm_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text fallback for /confirmstop — primary flow is now the inline button."""
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("You're not registered — nothing to remove.")
        return

    del users[chat_id]
    save_users(users)
    await update.effective_message.reply_text(
        "✅ You've been unregistered. Your data has been deleted.\n\n"
        "If you ever want to come back, just send /start."
    )


async def set_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("Please send /start first to register.")
        return

    if not context.args:
        current = users[chat_id].get("store_id", DEFAULT_STORE_ID)
        await update.effective_message.reply_text(
            f"Your current store ID is: {current}\n\n"
            "To change it: /store <store_id>\n"
            "To find nearby stores: /findstore <zip>"
        )
        return

    store_id = context.args[0].strip()
    is_first_time = "store_id" not in users[chat_id] or not users[chat_id].get("keywords")
    users[chat_id]["store_id"] = store_id
    save_users(users)
    await update.effective_message.reply_text(f"✅ Store set to {store_id}.")
    if is_first_time:
        await update.effective_message.reply_text(SETUP_STEP3)


async def clear_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("Please send /start first to register.")
        return

    users[chat_id]["keywords"] = []
    save_users(users)
    await update.effective_message.reply_text(
        "🗑️ Your watch list has been cleared.",
        reply_markup=main_menu_keyboard()
    )


async def find_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("Please send /start first to register.")
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /findstore <zip>\nExample: /findstore 33458")
        return

    zip_code = context.args[0].strip()
    await update.effective_message.reply_text(f"🔍 Looking up Publix stores near {zip_code}...")

    try:
        url = (
            f"https://services.publix.com/storelocator/api/v1/stores/"
            f"?types=R,G,H,N,S&count=10&distance=20&includeOpenAndCloseDates=true"
            f"&zip={zip_code}&isWebsite=true"
        )
        response = requests.get(url, timeout=10)
        stores = response.json().get("stores", [])

        if not stores:
            await update.effective_message.reply_text(f"No Publix stores found near {zip_code}.")
            return

        msg = f"🏪 Publix stores near {zip_code}:\n\n"
        for store in stores[:8]:
            name = store.get("name", "Unknown")
            address = store.get("address", {})
            street = address.get("streetAddress", "")
            city = address.get("city", "")
            store_id = store.get("weeklyAd", {}).get("storeId", "N/A")
            msg += f"• {name}\n  {street}, {city}\n  ID: {store_id} → /store {store_id}\n\n"

        await update.effective_message.reply_text(msg)
        await update.effective_message.reply_text(SETUP_STEP2)

    except Exception:
        await update.effective_message.reply_text("❌ Couldn't look up stores right now. Try again later.")


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("Please send /start first to register.")
        return

    keywords = users[chat_id].get("keywords", [])
    if not keywords:
        await update.effective_message.reply_text(
            "Your watch list is empty — nothing to scan for.\n"
            "Use /add <keyword> to add items first."
        )
        return

    name = users[chat_id].get("name", "User")
    store_id = users[chat_id].get("store_id", DEFAULT_STORE_ID)
    msg = update.effective_message
    if not msg:
        return

    await msg.reply_text(f"🔍 Scanning store {store_id}, hang tight... (this takes 2-3 minutes)")

    df = await asyncio.to_thread(get_bogo_deals, store_id)
    df_filtered = find_matching_deals(df, keywords)
    await asyncio.to_thread(send_deals_to_user, chat_id, name, df_filtered)
    await context.bot.send_message(chat_id=chat_id, text="What's next?", reply_markup=main_menu_keyboard())


async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not is_admin(chat_id):
        await update.effective_message.reply_text("❌ You don't have permission to use this command.")
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /approve <chat_id>")
        return

    target_id = context.args[0].strip()
    await _approve(context, target_id, update.effective_message)


async def deny_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not is_admin(chat_id):
        await update.effective_message.reply_text("❌ You don't have permission to use this command.")
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /deny <chat_id>")
        return

    target_id = context.args[0].strip()
    await _deny(context, target_id, update.effective_message)


# -----------------------------------------------------
# SHARED APPROVE / DENY LOGIC
# (used by both slash commands and inline buttons)
# -----------------------------------------------------

async def _approve(context, target_id, reply_to=None):
    users = load_users()
    if target_id not in users:
        if reply_to:
            await reply_to.reply_text(f"No pending request found for {target_id}.")
        return

    users[target_id]["status"] = "active"
    save_users(users)

    name = users[target_id].get("name", "User")
    if reply_to:
        await reply_to.reply_text(f"✅ Approved {name} ({target_id}).")

    from scraper import send_telegram
    send_telegram(
        target_id,
        f"✅ You've been approved, {name}! You're now registered for Publix BOGO alerts.\n\n"
        + SETUP_STEP1
    )


async def _deny(context, target_id, reply_to=None):
    users = load_users()
    if target_id not in users:
        if reply_to:
            await reply_to.reply_text(f"No request found for {target_id}.")
        return

    name = users[target_id].get("name", "User")
    del users[target_id]
    save_users(users)

    if reply_to:
        await reply_to.reply_text(f"🚫 Denied and removed {name} ({target_id}).")

    from scraper import send_telegram
    send_telegram(target_id, "Sorry, your registration request was not approved.")


# -----------------------------------------------------
# INLINE BUTTON HANDLER
# -----------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # must acknowledge within 10s or Telegram shows a spinner

    chat_id = str(query.from_user.id)
    data = query.data
    users = load_users()

    # ── Main menu ──────────────────────────────────────
    if data == "menu":
        if not is_registered(users, chat_id):
            await query.edit_message_text("Please send /start first to register.")
            return
        store_id = users[chat_id].get("store_id", "not set")
        keywords = users[chat_id].get("keywords", [])
        await query.edit_message_text(
            f"🏪 Store: {store_id}  |  📋 {len(keywords)} item(s) on watch list",
            reply_markup=main_menu_keyboard()
        )

    # ── Scan ───────────────────────────────────────────
    elif data == "scan":
        if not is_registered(users, chat_id):
            await query.edit_message_text("Please send /start first to register.")
            return
        keywords = users[chat_id].get("keywords", [])
        if not keywords:
            await query.edit_message_text(
                "Your watch list is empty.\nUse /add <keyword> to add items first.",
                reply_markup=main_menu_keyboard()
            )
            return
        name = users[chat_id].get("name", "User")
        store_id = users[chat_id].get("store_id", DEFAULT_STORE_ID)
        await query.edit_message_text(f"🔍 Scanning store {store_id}, hang tight... (this takes 2-3 minutes)")
        df = await asyncio.to_thread(get_bogo_deals, store_id)
        df_filtered = find_matching_deals(df, keywords)
        await asyncio.to_thread(send_deals_to_user, chat_id, name, df_filtered)
        await context.bot.send_message(chat_id=chat_id, text="Scan complete! What's next?", reply_markup=main_menu_keyboard())

    # ── List ───────────────────────────────────────────
    elif data == "list":
        if not is_registered(users, chat_id):
            await query.edit_message_text("Please send /start first to register.")
            return
        keywords = users[chat_id].get("keywords", [])
        store_id = users[chat_id].get("store_id", DEFAULT_STORE_ID)
        if not keywords:
            await query.edit_message_text(
                f"🏪 Store ID: {store_id}\n\nYour watch list is empty.\nUse /add <keyword> to add items.",
                reply_markup=main_menu_keyboard()
            )
        else:
            await query.edit_message_text(
                f"🏪 Store ID: {store_id}\n\n📋 Watch list — tap an item to remove it:",
                reply_markup=list_keyboard(keywords)
            )

    # ── Help ───────────────────────────────────────────
    elif data == "help":
        await query.edit_message_text(HELP_TEXT, reply_markup=main_menu_keyboard())

    # ── Add prompt ────────────────────────────────────
    elif data == "add_prompt":
        await query.edit_message_text(
            "Type your item(s) like this:\n\n"
            "/add beer\n"
            "/add hummus, pasta, wine\n\n"
            "Multiple keywords separated by commas are supported.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]])
        )

    # ── Remove keyword (from list view) ───────────────
    elif data.startswith("remove:"):
        if not is_registered(users, chat_id):
            await query.edit_message_text("Please send /start first to register.")
            return
        keyword = data[7:]
        match = next((k for k in users[chat_id]["keywords"] if k.lower() == keyword.lower()), None)
        if match:
            users[chat_id]["keywords"].remove(match)
            save_users(users)
        # Refresh the list view
        keywords = users[chat_id].get("keywords", [])
        store_id = users[chat_id].get("store_id", DEFAULT_STORE_ID)
        if not keywords:
            await query.edit_message_text(
                f"🏪 Store ID: {store_id}\n\nWatch list is now empty.",
                reply_markup=main_menu_keyboard()
            )
        else:
            await query.edit_message_text(
                f"🏪 Store ID: {store_id}\n\n📋 Watch list — tap an item to remove it:",
                reply_markup=list_keyboard(keywords)
            )

    # ── Stop confirmation ─────────────────────────────
    elif data == "stop_confirm":
        if not is_registered(users, chat_id):
            await query.edit_message_text("You're not registered — nothing to remove.")
            return
        await query.edit_message_text(
            "⚠️ Are you sure you want to stop?\n\n"
            "This will remove you from all future alerts and delete your watch list and store settings.",
            reply_markup=stop_confirm_keyboard()
        )

    elif data == "stop_yes":
        if not is_registered(users, chat_id):
            await query.edit_message_text("You're not registered — nothing to remove.")
            return
        del users[chat_id]
        save_users(users)
        await query.edit_message_text(
            "✅ You've been unregistered. Your data has been deleted.\n\n"
            "If you ever want to come back, just send /start."
        )

    elif data == "stop_no":
        await query.edit_message_text(
            "Cancelled — you're still registered. 👍",
            reply_markup=main_menu_keyboard()
        )

    # ── Admin: approve / deny ─────────────────────────
    elif data.startswith("approve:"):
        if not is_admin(chat_id):
            await query.answer("❌ Admins only.", show_alert=True)
            return
        target_id = data[8:]
        await _approve(context, target_id)
        await query.edit_message_text(
            query.message.text + "\n\n✅ Approved."
        )

    elif data.startswith("deny:"):
        if not is_admin(chat_id):
            await query.answer("❌ Admins only.", show_alert=True)
            return
        target_id = data[5:]
        await _deny(context, target_id)
        await query.edit_message_text(
            query.message.text + "\n\n🚫 Denied."
        )


# -----------------------------------------------------
# ERROR HANDLER
# -----------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f"Network error (bot will retry): {context.error}")
        return
    logger.error(f"Unexpected error: {context.error}", exc_info=context.error)
    if ADMIN_CHAT_ID:
        from scraper import send_telegram
        send_telegram(ADMIN_CHAT_ID, f"⚠️ Bot error: {context.error}")


# -----------------------------------------------------
# WEEKLY SCAN JOB
# -----------------------------------------------------

async def weekly_scan(context: ContextTypes.DEFAULT_TYPE):
    """Runs every Thursday at 3pm ET — scrapes each store once, sends deals to all users."""
    logger.info("Running weekly BOGO scan...")
    users = load_users()

    if not users:
        logger.warning("No users found — skipping weekly scan.")
        return

    store_groups = defaultdict(list)
    for chat_id, user_data in users.items():
        store_id = user_data.get("store_id", DEFAULT_STORE_ID)
        store_groups[store_id].append((chat_id, user_data))

    for store_id, user_list in store_groups.items():
        logger.info(f"Scraping store {store_id} for {len(user_list)} user(s)...")
        df = await asyncio.to_thread(get_bogo_deals, store_id)

        for chat_id, user_data in user_list:
            name = user_data.get("name", "User")
            keywords = user_data.get("keywords", [])
            if not keywords:
                continue
            df_filtered = find_matching_deals(df, keywords)
            await asyncio.to_thread(send_deals_to_user, chat_id, name, df_filtered)

    logger.info("Weekly scan complete.")


# -----------------------------------------------------
# MAIN
# -----------------------------------------------------

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_command))
    app.add_handler(CommandHandler("add",         add_keyword))
    app.add_handler(CommandHandler("remove",      remove_keyword))
    app.add_handler(CommandHandler("list",        list_keywords))
    app.add_handler(CommandHandler("clear",       clear_keywords))
    app.add_handler(CommandHandler("store",       set_store))
    app.add_handler(CommandHandler("findstore",   find_store))
    app.add_handler(CommandHandler("scan",        scan))
    app.add_handler(CommandHandler("stop",        stop))
    app.add_handler(CommandHandler("confirmstop", confirm_stop))
    app.add_handler(CommandHandler("approve",     approve_user))
    app.add_handler(CommandHandler("deny",        deny_user))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    eastern = ZoneInfo("America/New_York")
    app.job_queue.run_daily(
        weekly_scan,
        time=time(15, 0, 0, tzinfo=eastern),
        days=(4,),  # 0=Sun … 4=Thu … 6=Sat
        name="weekly_bogo_scan",
        job_kwargs={"misfire_grace_time": 300}
    )

    init_db()
    logger.info("Bot is running... (Ctrl+C to stop)")
    logger.info("Weekly scan scheduled for Thursdays at 3:00pm ET")
    app.run_polling()


if __name__ == "__main__":
    main()
