"""Run after you've sent any message to your bot in Telegram. Prints your chat id."""
import os, requests
from dotenv import load_dotenv
load_dotenv()
tok = os.getenv("TELEGRAM_BOT_TOKEN")
if not tok:
    raise SystemExit("Put your bot token in .env first (TELEGRAM_BOT_TOKEN=...).")
r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=30).json()
chats = {u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u}
print("Chat id(s):", chats or "none yet - send your bot a message like 'hi', then rerun")
