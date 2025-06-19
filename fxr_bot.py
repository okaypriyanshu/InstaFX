import os
import time
import asyncio
import requests
import random

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TESTMAIL_API_KEY = os.getenv("TESTMAIL_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID"))

# Map certain usernames to specific domains (others use DEFAULT_DOMAIN)
DOMAIN_MAP = {
    "user1": "domain1.snv.email",
    "user2": "domain2.snv.email",
}
DEFAULT_DOMAIN = "snv.email"

# In-memory store of the last email generated per Telegram user
user_emails = {}

def get_domain_for_user(username):
    return DOMAIN_MAP.get(username.lower(), DEFAULT_DOMAIN)

def generate_email(username):
    """Generate a unique temp email address."""
    timestamp = int(time.time())
    domain = get_domain_for_user(username)
    return f"{username}_{timestamp}@{domain}"

def extract_confirmation_link(html):
    """Parse the FX Replay confirmation link from email HTML."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("a", href=True)
    return link["href"] if link else None

def match_fxreplay_email(msg):
    """Detect if a Testmail message is the FX Replay confirmation."""
    sender = msg.get("from", "").lower()
    subject = msg.get("subject", "").lower()
    body = msg.get("html", "").lower()
    return (
        "fx replay" in subject or
        "confirm your email" in subject or
        "fx replay" in body or
        "mandrillapp.com" in sender
    )

async def poll_and_confirm(email, chat_id, context: ContextTypes.DEFAULT_TYPE):
    """
    Poll the Testmail inbox for up to 3 minutes,
    click the confirmation link when found, then notify.
    """
    headers = {"Authorization": f"Bearer {TESTMAIL_API_KEY}"}
    url = f"https://api.testmail.app/api/inboxes/{email}"
    timeout = 180
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        res = requests.get(url, headers=headers)
        if res.ok:
            for msg in res.json().get("messages", []):
                if match_fxreplay_email(msg):
                    link = extract_confirmation_link(msg.get("html", ""))
                    if link:
                        try:
                            requests.get(link)
                            await context.bot.send_message(chat_id, "✅ Email confirmed!")
                        except:
                            await context.bot.send_message(chat_id, "⚠️ Found link but click failed.")
                        return
        await asyncio.sleep(5)

    await context.bot.send_message(chat_id, "❌ Timed out—no confirmation email in 3 minutes.")

def signup_and_start_trial(email, chat_id, bot):
    """
    Use Selenium+Firefox (headless) to sign up on FXReplay,
    detect & alert on CAPTCHA, start the Pro Trader trial,
    then select the first server.
    """
    opts = Options()
    opts.add_argument("--headless")
    driver = webdriver.Firefox(options=opts)

    try:
        driver.get("https://fxreplay.com/signup")

        # ==== CAPTCHA DETECTION ====
        # 1) Look for reCAPTCHA container
        try:
            driver.find_element(By.CLASS_NAME, "g-recaptcha")
            bot.send_message(chat_id, "⚠️ CAPTCHA detected on signup page—please solve manually.")
            return
        except NoSuchElementException:
            pass

        # 2) Look for any iframe with "recaptcha"
        try:
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                if "recaptcha" in frame.get_attribute("src"):
                    bot.send_message(chat_id, "⚠️ CAPTCHA iframe detected—please solve manually.")
                    return
        except:
            pass
        # ==== end CAPTCHA DETECTION ====

        # Fill signup form
        user_prefix = email.split("@")[0]
        driver.find_element(By.NAME, "email").send_keys(email)
        driver.find_element(By.NAME, "username").send_keys(user_prefix)
        driver.find_element(By.NAME, "password").send_keys("Asdf@123")
        driver.find_element(By.NAME, "agree").click()
        driver.find_element(By.CSS_SELECTOR, "button[type=submit]").click()

        # Handle "username taken" error
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".error.username"))
            )
            new_user = f"{user_prefix}{random.randint(100,999)}"
            uname_field = driver.find_element(By.NAME, "username")
            uname_field.clear()
            uname_field.send_keys(new_user)
            driver.find_element(By.CSS_SELECTOR, "button[type=submit]").click()
        except:
            pass

        # Wait for dashboard redirect
        WebDriverWait(driver, 15).until(
            EC.url_contains("/dashboard")
        )

        # Start trial
        driver.get("https://fxreplay.com/trial")
        driver.find_element(By.NAME, "cardnumber").send_keys("4513650025995998")
        driver.find_element(By.NAME, "expdate").send_keys("01/28")
        driver.find_element(By.NAME, "cvv").send_keys("050")
        driver.find_element(By.CSS_SELECTOR, "button.start-trial").click()

        # Select first server
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.NAME, "server"))
        )
        select = driver.find_element(By.NAME, "server")
        for opt in select.find_elements(By.TAG_NAME, "option"):
            if opt.get_attribute("value"):
                opt.click()
                break
        driver.find_element(By.CSS_SELECTOR, "button.confirm-server").click()

    finally:
        driver.quit()

async def handle_fxr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /fxr <username> command (admin only)."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /fxr <username>")
        return

    username = context.args[0]
    email = generate_email(username)
    user_emails[user_id] = email

    await update.message.reply_text(
        f"📨 Generated email: `{email}`\nSigning up & starting trial...",
        parse_mode="Markdown"
    )

    # 1. Sign up & trial
    signup_and_start_trial(email, update.effective_chat.id, context.bot)
    await update.message.reply_text("✅ Trial started! Now confirming email...")

    # 2. Poll + confirm
    await poll_and_confirm(email, update.effective_chat.id, context)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start (admin only)."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return

    await update.message.reply_text(
        "Welcome, admin! Use /fxr <username> to automate FXReplay signup, trial, and email confirmation."
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fxr", handle_fxr))
    app.run_polling()
