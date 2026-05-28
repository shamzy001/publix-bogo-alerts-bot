# migrate.py — one-time script to migrate users.json into Railway Postgres
#
# Usage:
#   1. Add DATABASE_URL to your .env (copy from Railway dashboard)
#   2. Run: python migrate.py
#   3. Done — users.json data is now in Postgres

import json
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")

if not DATABASE_URL:
    print("❌ DATABASE_URL not set in .env — add it and try again.")
    exit(1)

if not os.path.exists(USERS_FILE):
    print("❌ users.json not found — nothing to migrate.")
    exit(1)

with open(USERS_FILE) as f:
    users = json.load(f)

if not users:
    print("⚠️  users.json is empty — nothing to migrate.")
    exit(0)

conn = psycopg2.connect(DATABASE_URL)
with conn:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                data JSONB NOT NULL
            )
        """)
        for chat_id, data in users.items():
            cur.execute(
                """
                INSERT INTO users (chat_id, data) VALUES (%s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET data = EXCLUDED.data
                """,
                (chat_id, psycopg2.extras.Json(data))
            )
            print(f"  ✓ {data.get('name', chat_id)} ({chat_id})")

conn.close()
print(f"\n✅ Migrated {len(users)} user(s) to Postgres.")
