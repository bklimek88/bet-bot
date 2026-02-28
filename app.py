import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

@app.get("/")
def home():
    return "Bot działa", 200

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True)
    print("UPDATE:", update)

    # Odpowiedź do użytkownika
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
