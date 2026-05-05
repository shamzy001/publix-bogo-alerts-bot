# bot.py — Publix BOGO Telegram Bot

import asyncio
import logging
import os
import requests
from collections import defaultdict
from datetime import time
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
from scraper import (
    load_users, save_users,
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

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

HELP_TEXT = (
    "🤖 Publix BOGO Alert Bot — Commands\n"
    "─────────────────────────────────────\n"
    "/findstore <zip>  — find nearby stores and their IDs\n"
    "/store <id>       — set your Publix store\n"
    "/add <item>       — add item to your watch list\n"
    "/remove <item>    — remove item from your watch list\n"
    "/list             — show your watch list and store\n"
    "/clear            — clear your entire watch list\n"
    "/scan             — scan Publix now and send your deals\n"
    "/stop             — unregister and delete all your data\n"
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
    "every Thursday at 2pm 📅\n"
    "You can also check anytime with /scan 🎉"
)


def is_registered(users, chat_id):
    """Returns True only for fully approved/active users."""
    return chat_id in users and users[chat_id].get("status", "active") == "active"


def is_admin(chat_id):
    return str(chat_id) == str(ADMIN_CHAT_ID)


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
            f"📋 Watch list: {len(keywords)} item(s)\n\n"
            + HELP_TEXT
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
        send_telegram(
            ADMIN_CHAT_ID,
            f"🔔 New registration request:\n"
            f"Name: {name}\n"
            f"Chat ID: {chat_id}\n\n"
            f"Approve: /approve {chat_id}\n"
            f"Deny:    /deny {chat_id}"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)


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

    await update.effective_message.reply_text(msg)


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
        await update.effective_message.reply_text(f"✅ Removed '{match}' from your watch list.")
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
            "Your watch list is empty.\nUse /add <keyword> to add items."
        )
        return

    msg = (
        f"🏪 Store ID: {store_id}\n\n"
        f"📋 Your watch list ({len(keywords)} items):\n"
        + "\n".join(f"• {k}" for k in keywords)
    )
    await update.effective_message.reply_text(msg)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("You're not registered — nothing to remove.")
        return

    await update.effective_message.reply_text(
        "⚠️ Are you sure you want to stop?\n\n"
        "This will remove you from all future alerts and delete your watch list and store settings.\n\n"
        "Send /confirmstop to confirm, or just ignore this message to stay registered."
    )


async def confirm_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()

    if not is_registered(users, chat_id):
        await update.effective_message.reply_text("You're not registered — nothing to remove.")
        return

    del users[chat_id]
    save_users(users)
    await update.effective_message.reply_text(
        "✅ You've been unregistered. Your data has been deleted and you won't receive any more alerts.\n\n"
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
    await update.effective_message.reply_text("🗑️ Your watch list has been cleared.")


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

    # Run the blocking scraper in a background thread so the bot stays responsive
    df = await asyncio.to_thread(get_bogo_deals, store_id)
    df_filtered = find_matching_deals(df, keywords)
    await asyncio.to_thread(send_deals_to_user, chat_id, name, df_filtered)


async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not is_admin(chat_id):
        await update.effective_message.reply_text("❌ You don't have permission to use this command.")
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /approve <chat_id>")
        return

    target_id = context.args[0].strip()
    users = load_users()

    if target_id not in users:
        await update.effective_message.reply_text(f"No pending request found for {target_id}.")
        return

    users[target_id]["status"] = "active"
    save_users(users)

    name = users[target_id].get("name", "User")
    await update.effective_message.reply_text(f"✅ Approved {name} ({target_id}).")

    from scraper import send_telegram
    send_telegram(
        target_id,
        f"✅ You've been approved, {name}! You're now registered for Publix BOGO alerts.\n\n"
        + SETUP_STEP1
    )


async def deny_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not is_admin(chat_id):
        await update.effective_message.reply_text("❌ You don't have permission to use this command.")
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /deny <chat_id>")
        return

    target_id = context.args[0].strip()
    users = load_users()

    if target_id not in users:
        await update.effective_message.reply_text(f"No request found for {target_id}.")
        return

    name = users[target_id].get("name", "User")
    del users[target_id]
    save_users(users)

    await update.effective_message.reply_text(f"🚫 Denied and removed {name} ({target_id}).")

    from scraper import send_telegram
    send_telegram(target_id, "Sorry, your registration request was not approved.")


async def weekly_scan(context: ContextTypes.DEFAULT_TYPE):
    """Runs every Thursday at 2pm ET — scrapes each store once, sends deals to all users."""
    logger.info("Running weekly BOGO scan...")
    users = load_users()

    if not users:
        logger.warning("No users found — skipping weekly scan.")
        return

    # Group users by store so each store is only scraped once
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


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_keyword))
    app.add_handler(CommandHandler("remove", remove_keyword))
    app.add_handler(CommandHandler("list", list_keywords))
    app.add_handler(CommandHandler("clear", clear_keywords))
    app.add_handler(CommandHandler("store", set_store))
    app.add_handler(CommandHandler("findstore", find_store))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("confirmstop", confirm_stop))
    app.add_handler(CommandHandler("approve", approve_user))
    app.add_handler(CommandHandler("deny", deny_user))

    # Weekly scan — every Thursday at 2:00pm Eastern
    eastern = ZoneInfo("America/New_York")
    app.job_queue.run_daily(
        weekly_scan,
        time=time(14, 0, 0, tzinfo=eastern),
        days=(3,),  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
        name="weekly_bogo_scan"
    )

    logger.info("Bot is running... (Ctrl+C to stop)")
    logger.info("Weekly scan scheduled for Thursdays at 2:00pm ET")
    app.run_polling()


if __name__ == "__main__":
    main()
