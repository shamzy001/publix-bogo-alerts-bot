# send_alerts.py — manual runner to send BOGO deals to all users
# The bot (bot.py) runs this automatically every Thursday at 2pm ET.
# Use this script to trigger a scan manually outside the schedule.

from collections import defaultdict
from scraper import get_bogo_deals, find_matching_deals, send_deals_to_user, load_users, DEFAULT_STORE_ID

if __name__ == "__main__":

    users = load_users()
    if not users:
        print("⚠️  No users found in users.json — nothing to send.")
        exit()

    # Group users by store ID so each store is only scraped once
    store_groups = defaultdict(list)
    for chat_id, user_data in users.items():
        store_id = user_data.get("store_id", DEFAULT_STORE_ID)
        store_groups[store_id].append((chat_id, user_data))

    for store_id, user_list in store_groups.items():
        print(f"\n🔎 Scraping store {store_id} for {len(user_list)} user(s)...")
        df = get_bogo_deals(store_id)
        print(f"✅ Found {len(df)} total BOGO items")

        for chat_id, user_data in user_list:
            name = user_data.get("name", "User")
            keywords = user_data.get("keywords", [])

            if not keywords:
                print(f"⚠️  Skipping {name} — no keywords set.")
                continue

            df_filtered = find_matching_deals(df, keywords)
            print(f"📬 {name}: {len(df_filtered)} matching deals")
            send_deals_to_user(chat_id, name, df_filtered)

    print("\n✓ Done sending alerts!")
