import os
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# 🔹 Test czy serwer działa
@app.route("/")
def home():
    return "Bot działa", 200


# 🔹 Webhook od Telegrama
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True)
    print("UPDATE:", update)

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"Otrzymałem: {text}"
            },
            timeout=10
        )

        print("sendMessage status:", resp.status_code)
        print("sendMessage body:", resp.text)

    return "OK", 200
