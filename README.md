# Publix BOGO Alerts

A Telegram bot that scrapes Publix's weekly BOGO deals and sends personalized alerts to users based on their watch lists.

## Features

- Scrapes the Publix weekly BOGO page using Selenium
- Sends personalized deals to each user via Telegram every Thursday at 2:00pm ET
- Each user manages their own keyword watch list and store via bot commands
- Supports multiple users across different Publix store locations
- When multiple users share the same store, the page is only scraped once
- Users can opt out at any time and have their data fully deleted

## Project Structure

```
publix-bogo-alerts/
├── bot.py                # Telegram bot — handles user commands and weekly schedule
├── scraper.py            # Shared scraping, filtering, and Telegram utilities
├── send_alerts.py        # Optional manual runner — scrapes and sends deals to all users
├── users.json            # User data — gitignored, created automatically by the bot
├── users.example.json    # Template showing the users.json structure
├── .env                  # Credentials — gitignored, never commit this
├── .env.example          # Template for .env
├── requirements.txt      # Python dependencies
└── .gitignore
```

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Create your `.env` file
Copy `.env.example` to `.env` and fill in your Telegram bot token:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
```

Get a bot token from [@BotFather](https://t.me/BotFather) on Telegram.

### 3. Run the bot
```bash
python bot.py
```
Keep this running — it listens for user commands and automatically runs the weekly scan every Thursday at 2:00pm ET.

### 4. Manual scraper (optional)
```bash
python send_alerts.py
```
Run this manually if you need to trigger a scan outside the bot's schedule.

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Register and begin setup |
| `/findstore <zip>` | Find nearby Publix stores and their IDs |
| `/store <store_id>` | Set your Publix store |
| `/add <item>` | Add one or more items to your watch list (comma-separated) |
| `/remove <item>` | Remove an item from your watch list |
| `/list` | Show your current watch list and store |
| `/clear` | Clear your entire watch list |
| `/scan` | Scan Publix right now and send your matching deals |
| `/stop` | Unregister — prompts for confirmation before deleting data |
| `/confirmstop` | Confirms opt-out and permanently deletes all your data |
| `/help` | Show the command list |

### Adding multiple items at once
```
/add beer, hummus, pasta, kefir
```

## New User Flow

1. Find the bot on Telegram and send `/start`
2. Use `/findstore <zip>` to find your nearest store
3. Use `/store <store_id>` to set your store
4. Use `/add <item>` to build your watch list
5. Deals will be sent automatically every Thursday at 2:00pm ET
6. Use `/scan` to check for deals anytime on demand

## Opting Out

Users can unregister at any time:
1. Send `/stop` — bot will warn that all data will be deleted
2. Send `/confirmstop` — data is permanently removed, no further alerts will be sent
3. To re-register, simply send `/start` again

## Special Filtering Rules

Some keywords have extra rules to avoid irrelevant matches:

- **kellogg** — only matches cereal products
- **popcorn** — excludes shrimp, chicken, and Popcorners
- **pasta** — excludes pasta bowls
- **bertolli** — only matches sauce products

## Deployment

### Windows
Use Task Scheduler to launch `bot.py` at startup so it keeps running in the background.

### Linux/Debian (recommended)
Run `bot.py` as a `systemd` service so it starts automatically on boot and restarts if it crashes:

```ini
[Unit]
Description=Publix BOGO Alert Bot
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/bot.py
WorkingDirectory=/path/to/publix-bogo-alerts
Restart=always

[Install]
WantedBy=multi-user.target
```

## Notes

- `users.json` is created and managed automatically by the bot — it is gitignored and should never be committed
- Copy `users.example.json` to understand the structure if setting up manually
- `.env` and credentials should never be committed to git
- Publix typically refreshes their BOGO deals on Wednesdays; alerts go out Thursdays at 2pm ET
