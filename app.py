import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.get("/")
def home():
    return "OK — bet-bot działa na Render ✅"

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.post("/webhook")
def webhook():
    # tu później podłączymy Telegram / OCR
    data = request.get_json(silent=True) or {}
    return jsonify(received=True, data=data)
