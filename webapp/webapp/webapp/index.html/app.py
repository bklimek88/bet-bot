import os
from flask import Flask, request, jsonify, send_file
from google.cloud import vision
import re

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

@app.get("/")
def health():
    return "Bot działa ✅"

@app.get("/webapp")
def webapp():
    return send_file("webapp.html")

@app.post("/api/scan")
def scan():
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "No image"}), 400

    image_bytes = request.files["image"].read()

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.text_detection(image=image)

    if response.error.message:
        return jsonify({"ok": False, "error": response.error.message}), 500

    texts = response.text_annotations
    if not texts:
        return jsonify({"ok": False, "error": "Brak tekstu"}), 400

    ocr_text = texts[0].description

    # proste parsowanie
    odds = None
    stake = None

    odds_match = re.findall(r"\b([1-9]\d*[.,]\d{2})\b", ocr_text)
    if odds_match:
        odds = float(odds_match[-1].replace(",", "."))

    stake_match = re.findall(r"\b(\d{1,5})\s?(zł|PLN)\b", ocr_text, re.I)
    if stake_match:
        stake = float(stake_match[-1][0])

    result_guess = None
    low = ocr_text.lower()
    if "wygr" in low or "won" in low:
        result_guess = "W"
    if "przegr" in low or "lost" in low:
        result_guess = "L"

    return jsonify({
        "ok": True,
        "result": {
            "day_key": "auto",
            "session": "auto",
            "desc": ocr_text[:150],
            "odds": odds,
            "stake": stake,
            "result_guess": result_guess
        }
    })
