# scraper.py — shared scraping, filtering, and Telegram utilities

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from pandas import DataFrame
from dotenv import load_dotenv
import os
import json
import time
import requests

load_dotenv()

DEFAULT_STORE_ID = ""  # set via /store command in the bot or in users.json
USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


# -----------------------------------------------------
# USER MANAGEMENT
# -----------------------------------------------------
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return json.load(f)
    return {}


def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


# -----------------------------------------------------
# TELEGRAM
# -----------------------------------------------------
def send_telegram(chat_id, message_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": message_text})
    return response.ok


# -----------------------------------------------------
# SCRAPER
# -----------------------------------------------------
def get_bogo_deals(store_id=DEFAULT_STORE_ID):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    url = f"https://www.publix.com/savings/weekly-ad/bogo/?storeId={store_id}"
    driver.get(url)

    try:
        WebDriverWait(driver, 40).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "li[id^='bogo-']"))
        )

        seen = set()
        stagnant = 0
        max_stagnant = 3
        results = []

        while True:
            bogo_cards = driver.find_elements(By.CSS_SELECTOR, "li[id^='bogo-']")
            new_found = 0

            for card in bogo_cards:
                try:
                    product = card.find_element(By.CSS_SELECTOR, "div[data-qa-automation='prod-title']").text.strip()
                except:
                    continue

                if not product or product in seen:
                    continue
                seen.add(product)
                new_found += 1

                try:
                    offer = card.find_element(By.CSS_SELECTOR, ".p-savings-badge__text span").text.strip()
                except:
                    offer = ""

                try:
                    valid = card.find_element(By.CSS_SELECTOR, ".valid-dates").text.strip()
                    valid = valid.replace("Valid ", "")
                except:
                    valid = ""

                results.append({
                    "Product": product,
                    "Deal": offer,
                    "Validity": valid
                })

            print(f"Collected {len(results)} items so far...")

            driver.execute_script("window.scrollBy(0, window.innerHeight);")
            time.sleep(3)

            if new_found == 0:
                stagnant += 1
                if stagnant >= max_stagnant:
                    break
            else:
                stagnant = 0

    finally:
        driver.quit()

    return DataFrame(results)


# -----------------------------------------------------
# FILTER LOGIC
# -----------------------------------------------------
def passes_special_filters(product):
    p = product.lower()
    if "kellogg" in p and "cereal" not in p:
        return False
    if "popcorn" in p and any(x in p for x in ["shrimp", "chicken", "popcorners"]):
        return False
    if "pasta" in p and "bowl" in p:
        return False
    if "bertolli" in p and "sauce" not in p:
        return False
    return True


def find_matching_deals(df, keywords):
    filtered = []
    seen_products = set()

    for keyword in keywords:
        matched = df[df["Product"].str.contains(keyword, case=False, na=False)].values.tolist()
        for row in matched:
            product, deal, validity = row
            if product in seen_products:
                continue
            if not passes_special_filters(product):
                continue
            seen_products.add(product)
            filtered.append(row)

    if not filtered:
        return DataFrame(columns=["Product", "Deal", "Validity"])

    df2 = DataFrame(filtered, columns=["Product", "Deal", "Validity"])
    df2 = df2.groupby("Product").agg({
        "Deal": " | ".join,
        "Validity": " | ".join,
    }).reset_index()

    return df2


# -----------------------------------------------------
# SEND DEALS TO A SINGLE USER
# -----------------------------------------------------
def send_deals_to_user(chat_id, name, df_filtered):
    if df_filtered.empty:
        send_telegram(chat_id, f"🛒 No matching Publix BOGO deals for you this week, {name}.")
        return

    send_telegram(chat_id, f"🛒 Publix BOGO Deals for you this week, {name}!\n{'─' * 28}")
    time.sleep(0.5)

    for _, row in df_filtered.iterrows():
        msg = f"📦 {row['Product']}\n💰 {row['Deal']}\n📅 {row['Validity']}"
        send_telegram(chat_id, msg)
        time.sleep(1)
