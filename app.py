import os
import re
import json
import time
import hashlib
import threading
from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List

import requests
from flask import Flask, request, jsonify

# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)

# ============================================================
# ENV / CONFIG (Render -> Environment Variables)
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Brak BOT_TOKEN w Environment (Render).")

OCR_API_KEY = os.environ.get("OCR_API_KEY")  # OCR.space API key (optional, needed for OCR)
BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

DATA_FILE = "data.json"  # On Render: najlepiej podpiąć Persistent Disk i ustawić path na /var/data/data.json

# ============================================================
# CHALLENGE
# ============================================================
CHALLENGE_START_DATE = "2026-02-22"
CHALLENGE_START_PROFIT = 4372.0

# ============================================================
# ZASADY (NA SESJĘ)
# ============================================================
MAX_LOSS_STREAK = 2
PROFIT_LOCK_THRESHOLD = 400.0

# ============================================================
# PORANEK (CHECKLIST)
# ============================================================
DEFAULT_TASKS = [
    {"text": "🧘‍♂️ 22 min medytacji", "done": False},
    {"text": "📖 30 min czytania", "done": False},
]

HELP_TEXT = (
    "✅ Komendy:\n"
    "/start /help\n\n"
    "🧠 Poranek (blokuje /bet dopóki nie zaliczone):\n"
    "/tasks\n"
    "/done 1\n"
    "/done 2\n"
    "/ready\n\n"
    "📌 Zakłady:\n"
    "/bet opis | kurs | stawka\n"
    "/list\n"
    "/settle ID W\n"
    "/settle ID L\n"
    "/delete ID\n\n"
    "📊 Podsumowania:\n"
    "/day\n"
    "/endday\n"
    "/month [YYYY-MM]\n"
    "/monthdetails [YYYY-MM]\n"
    "/challenge\n\n"
    "🧹 Reset:\n"
    "/clearday\n\n"
    "📷 OCR (BetInAsia My Orders):\n"
    "Wyślij screena → bot spróbuje wyłapać single i je zapisać.\n"
)

STOP_MSG = (
    "🛑 STOP. Koniec sesji.\n"
    "System > emocje. Wracasz do gry dopiero w kolejnej sesji / jutro."
)

MORNING_BLOCK_MSG = (
    "⛔ STOP. Najpierw poranek:\n"
    "🧘‍♂️ 22 min medytacji\n"
    "📖 30 min czytania\n\n"
    "1) /tasks\n"
    "2) /done 1 i /done 2\n"
    "3) /ready"
)

# ============================================================
# SESJE (minuty od 00:00)
# ============================================================
AM_START_MIN = 2 * 60
AM_END_MIN = 13 * 60 + 30  # 13:30

# ============================================================
# UTILS: TIME / SESSION
# ============================================================
def now_local() -> datetime:
    return datetime.now()

def minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute

def session_label(sess: str) -> str:
    return "Poranna (02:00–13:30)" if sess == "AM" else "Wieczorna (13:30–02:00)"

def session_key_for_time(dt: datetime) -> Tuple[str, str]:
    """
    - AM: [02:00, 13:30) -> dzisiaj
    - PM: >= 13:30 -> dzisiaj
    - PM: < 02:00 -> wczorajszy PM
    """
    m = minutes_of_day(dt)
    if AM_START_MIN <= m < AM_END_MIN:
        return dt.date().isoformat(), "AM"
    if m < AM_START_MIN:
        return (dt.date() - timedelta(days=1)).isoformat(), "PM"
    return dt.date().isoformat(), "PM"

# ============================================================
# TELEGRAM API
# ============================================================
def tg_api(method: str, params: Optional[dict] = None, http_method: str = "GET") -> dict:
    url = f"{BASE}/{method}"
    if http_method == "POST":
        r = requests.post(url, json=params, timeout=60)
    else:
        r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()

def send(chat_id: int, text: str) -> None:
    tg_api("sendMessage", {"chat_id": chat_id, "text": text}, http_method="POST")

def get_file_bytes(file_id: str) -> bytes:
    info = tg_api("getFile", {"file_id": file_id})
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    file_path = info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    r = requests.get(file_url, timeout=60)
    r.raise_for_status()
    return r.content

# ============================================================
# DB (JSON)
# ============================================================
def load_db() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"chats": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            if not isinstance(d, dict):
                return {"chats": {}}
            d.setdefault("chats", {})
            return d
    except Exception:
        return {"chats": {}}

def save_db(db: Dict[str, Any]) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def ensure_chat(db: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
    chats = db.setdefault("chats", {})
    c = chats.setdefault(str(chat_id), {})
    c.setdefault("days", {})
    c.setdefault("tasks", [dict(t) for t in DEFAULT_TASKS])
    c.setdefault("meta", {})
    c["meta"].setdefault("last_session_report", {})
    c["meta"].setdefault("last_month_report", "")
    c["meta"].setdefault("ocr_seen_hashes", [])  # anty-duplikaty OCR
    return c

def empty_session_state() -> Dict[str, Any]:
    return {
        "wins": 0,
        "losses": 0,
        "loss_streak": 0,
        "settle_seq": 0,
        "profit": 0.0,
        "staked_settled": 0.0,
        "yield_pct": 0.0,
        "profit_lock_armed": False,
        "locked": False,
        "lock_reason": "",
    }

def ensure_day(db: Dict[str, Any], chat_id: int, day_key: str) -> Dict[str, Any]:
    c = ensure_chat(db, chat_id)
    days = c["days"]

    if day_key not in days:
        days[day_key] = {
            "bets": [],
            "next_bet_id": 1,
            "sessions": {"AM": empty_session_state(), "PM": empty_session_state()},
            "prep_done": False,
            "updated_at": now_local().isoformat(timespec="seconds"),
        }
    else:
        d = days[day_key]
        d.setdefault("bets", [])
        d.setdefault("next_bet_id", 1)
        d.setdefault("sessions", {})
        d["sessions"].setdefault("AM", empty_session_state())
        d["sessions"].setdefault("PM", empty_session_state())
        d.setdefault("prep_done", False)

    return days[day_key]

def morning_done(db: Dict[str, Any], chat_id: int) -> bool:
    c = ensure_chat(db, chat_id)
    tasks = c.get("tasks", [])
    if not tasks:
        return True
    return all(bool(t.get("done")) for t in tasks)

# ============================================================
# BET LOGIC
# ============================================================
def parse_bet(text: str) -> Optional[Tuple[str, float, float]]:
    raw = text[len("/bet"):].strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3:
        return None

    desc = parts[0]
    try:
        odds = float(parts[1].replace(",", "."))
        stake = float(parts[2].replace(",", "."))
    except ValueError:
        return None

    if not desc or odds <= 1.0 or stake <= 0:
        return None
    return desc, odds, stake

def find_bet(day: Dict[str, Any], bet_id: int) -> Optional[Dict[str, Any]]:
    for b in day["bets"]:
        if int(b.get("id", -1)) == bet_id:
            return b
    return None

def recompute_session(day: Dict[str, Any], session: str) -> None:
    s = day["sessions"][session]
    wins = losses = 0
    profit = staked = 0.0

    for b in day["bets"]:
        if b.get("session") != session:
            continue

        if b.get("status") == "W":
            wins += 1
            staked += float(b["stake"])
            profit += float(b["stake"]) * (float(b["odds"]) - 1.0)
        elif b.get("status") == "L":
            losses += 1
            staked += float(b["stake"])
            profit -= float(b["stake"])

    s["wins"] = wins
    s["losses"] = losses
    s["profit"] = round(profit, 2)
    s["staked_settled"] = round(staked, 2)
    s["yield_pct"] = round((profit / staked * 100.0) if staked > 0 else 0.0, 2)

    if profit >= PROFIT_LOCK_THRESHOLD:
        s["profit_lock_armed"] = True
    else:
        s["profit_lock_armed"] = bool(s.get("profit_lock_armed", False))

def recompute_loss_streak(day: Dict[str, Any], session: str) -> None:
    settled = [
        b for b in day["bets"]
        if b.get("session") == session
        and b.get("status") in ("W", "L")
        and isinstance(b.get("settle_seq"), int)
    ]
    settled.sort(key=lambda x: x["settle_seq"])

    streak = 0
    for b in reversed(settled):
        if b["status"] == "L":
            streak += 1
        else:
            break

    day["sessions"][session]["loss_streak"] = streak

def check_and_lock(day: Dict[str, Any], session: str, last_result: Optional[str]) -> None:
    s = day["sessions"][session]
    if s.get("locked"):
        return

    recompute_loss_streak(day, session)

    if s.get("loss_streak", 0) >= MAX_LOSS_STREAK:
        s["locked"] = True
        s["lock_reason"] = "2 przegrane pod rząd = KONIEC SESJI"
        return

    if last_result == "L" and s.get("profit_lock_armed", False):
        s["locked"] = True
        s["lock_reason"] = f"Profit ≥ {PROFIT_LOCK_THRESHOLD} i przegrana = ochrona zysku (sesja)"

def crypto_split(profit: float) -> Tuple[float, float]:
    if profit <= 0:
        return 0.0, 0.0
    crypto = round(profit * 0.5, 2)
    bankroll = round(profit - crypto, 2)
    return crypto, bankroll

def session_summary_text(day: Dict[str, Any], day_key: str, session: str) -> str:
    recompute_session(day, session)
    recompute_loss_streak(day, session)
    s = day["sessions"][session]

    profit = float(s["profit"])
    w = int(s["wins"])
    l = int(s["losses"])
    y = float(s["yield_pct"])
    streak = int(s.get("loss_streak", 0))
    crypto, bankroll = crypto_split(profit)

    lines = [
        f"📌 Podsumowanie sesji ({day_key})",
        f"🕒 Sesja: {session_label(session)}",
        f"Profit: {profit} zł",
        f"W/L: {w}/{l}",
        f"Yield: {y:.2f}%",
        f"Seria przegranych: {streak}",
    ]
    if profit > 0:
        lines += [
            "",
            "💰 Odkładanie 50/50 (sesja):",
            f"– krypto: {crypto} zł",
            f"– bankroll: {bankroll} zł",
        ]
    if s.get("locked"):
        lines += ["", f"🛑 Sesja zablokowana: {s.get('lock_reason')}"]
    return "\n".join(lines)

def day_summary_text(db: Dict[str, Any], chat_id: int, day_key: str) -> str:
    day = ensure_day(db, chat_id, day_key)
    for sess in ("AM", "PM"):
        recompute_session(day, sess)
        recompute_loss_streak(day, sess)

    am = day["sessions"]["AM"]
    pm = day["sessions"]["PM"]

    total_profit = round(float(am.get("profit", 0.0)) + float(pm.get("profit", 0.0)), 2)
    total_staked = round(float(am.get("staked_settled", 0.0)) + float(pm.get("staked_settled", 0.0)), 2)
    total_w = int(am.get("wins", 0)) + int(pm.get("wins", 0))
    total_l = int(am.get("losses", 0)) + int(pm.get("losses", 0))
    total_y = round((total_profit / total_staked * 100.0) if total_staked > 0 else 0.0, 2)

    return "\n".join([
        f"📊 Podsumowanie dnia ({day_key})",
        "",
        session_summary_text(day, day_key, "AM"),
        "",
        session_summary_text(day, day_key, "PM"),
        "",
        "— — —",
        f"✅ Razem dzień: Profit {total_profit} zł | W/L {total_w}/{total_l} | Staked {total_staked} zł | Yield {total_y:.2f}%",
    ])

def month_summary_text(db: Dict[str, Any], chat_id: int, ym: Optional[str] = None) -> str:
    c = ensure_chat(db, chat_id)
    days = c.get("days", {})

    if ym is None:
        ym = date.today().strftime("%Y-%m")

    if len(ym) != 7 or ym[4] != "-" or not (ym[:4].isdigit() and ym[5:].isdigit()):
        return "❌ Zły format. Użyj: /month albo /month 2026-02"

    total_profit = total_staked = 0.0
    total_wins = total_losses = 0
    days_with_bets = 0
    all_days_in_month = 0

    for k in sorted(days.keys()):
        if not k.startswith(ym):
            continue

        all_days_in_month += 1
        d = ensure_day(db, chat_id, k)

        for sess in ("AM", "PM"):
            recompute_session(d, sess)
            s = d["sessions"][sess]
            total_profit += float(s.get("profit", 0.0) or 0.0)
            total_staked += float(s.get("staked_settled", 0.0) or 0.0)
            total_wins += int(s.get("wins", 0) or 0)
            total_losses += int(s.get("losses", 0) or 0)

        if isinstance(d.get("bets"), list) and len(d["bets"]) > 0:
            days_with_bets += 1

    yield_pct = (total_profit / total_staked * 100.0) if total_staked > 0 else 0.0
    crypto, bankroll = crypto_split(total_profit)

    lines = [
        f"📅 Podsumowanie miesiąca ({ym})",
        f"Dni w bazie: {all_days_in_month}",
        f"Dni z zakładami: {days_with_bets}",
        "",
        f"Profit: {round(total_profit, 2)} zł",
        f"W/L: {total_wins}/{total_losses}",
        f"Obrócone (staked): {round(total_staked, 2)} zł",
        f"Yield: {round(yield_pct, 2)}%",
    ]

    if total_profit > 0:
        lines += [
            "",
            "💰 Odkładanie 50/50 (miesiąc):",
            f"– krypto: {round(crypto,2)} zł",
            f"– bankroll: {round(bankroll,2)} zł",
        ]

    return "\n".join(lines)

def month_details_text(db: Dict[str, Any], chat_id: int, ym: Optional[str]) -> str:
    if ym is None:
        ym = date.today().strftime("%Y-%m")

    if len(ym) != 7 or ym[4] != "-" or not (ym[:4].isdigit() and ym[5:].isdigit()):
        return "❌ Zły format. Użyj: /monthdetails 2026-02"

    c = ensure_chat(db, chat_id)
    days = c.get("days", {})

    rows: List[str] = [f"🧾 Historia zakładów ({ym}) — data | sesja | kurs | stawka | W/L | profit | opis"]
    found = 0

    for day_key in sorted(days.keys()):
        if not day_key.startswith(ym):
            continue

        d = ensure_day(db, chat_id, day_key)
        for b in d.get("bets", []):
            if b.get("status") not in ("W", "L"):
                continue

            stake = float(b["stake"])
            odds = float(b["odds"])
            profit = stake * (odds - 1.0) if b["status"] == "W" else -stake

            rows.append(
                f"{day_key} | {b.get('session')} | {odds:.3f} | {stake:.2f} | {b['status']} | {profit:.2f} | {b.get('desc','')}"
            )
            found += 1

    if found == 0:
        return f"Brak rozliczonych zakładów w {ym}."

    text = "\n".join(rows)
    if len(text) > 3500:
        text = text[:3500] + "\n…(ucięte, za dużo wpisów na 1 wiadomość)"
    return text

def challenge_profit(db: Dict[str, Any], chat_id: int) -> float:
    c = ensure_chat(db, chat_id)
    days = c.get("days", {})

    add_profit = 0.0
    for k in days.keys():
        if k < CHALLENGE_START_DATE:
            continue
        d = ensure_day(db, chat_id, k)
        for sess in ("AM", "PM"):
            recompute_session(d, sess)
            add_profit += float(d["sessions"][sess].get("profit", 0.0) or 0.0)

    return round(CHALLENGE_START_PROFIT + add_profit, 2)

def challenge_text(db: Dict[str, Any], chat_id: int) -> str:
    total = challenge_profit(db, chat_id)
    return (
        f"🏁 Challenge od {CHALLENGE_START_DATE}\n"
        f"Startowy wynik (offset): {CHALLENGE_START_PROFIT} zł\n"
        f"Aktualny wynik challengu: {total} zł"
    )

def tasks_text(tasks: list) -> str:
    lines = ["🧠 Poranek – checklist:"]
    for i, t in enumerate(tasks, start=1):
        mark = "✅" if t.get("done") else "⬜"
        lines.append(f"{mark} {i}. {t.get('text','')}")
    lines += ["", "Odhacz: /done 1, /done 2", "Odblokuj: /ready"]
    return "\n".join(lines)

def list_text(day: Dict[str, Any], day_key: str) -> str:
    if not day["bets"]:
        return "Brak zakładów w tej dacie. Dodaj: /bet opis | kurs | stawka"

    lines: List[str] = [f"📋 Zakłady {day_key}:"]
    for sess in ("AM", "PM"):
        group = [b for b in day["bets"] if b.get("session") == sess]
        if not group:
            continue
        lines.append("")
        lines.append(f"— {session_label(sess)} —")
        for b in group:
            extra = ""
            if b.get("status") in ("W", "L") and "settle_seq" in b:
                extra = f" (seq:{b['settle_seq']})"
            lines.append(f"#{b['id']} [{b['status']}] {b['stake']} @ {b['odds']}{extra}\n{b['desc']}")
    return "\n".join(lines)

# ============================================================
# OCR (BetInAsia My Orders)
# ============================================================
_ODDS_RE = re.compile(r"\b([1-9]\d?)\.(\d{3})\b")                  # 1.892, 2.030
_MONEY_RE = re.compile(r"(\$|€|£)\s?(\d{1,6}\.\d{2})")            # $59.97
_PROFIT_RE = re.compile(r"([+\-])\s?(\$|€|£)\s?(\d{1,6}\.\d{2})") # + $54.09 / - $55.95

def ocr_space(image_bytes: bytes) -> str:
    if not OCR_API_KEY:
        raise RuntimeError("Brak OCR_API_KEY w Environment (Render).")

    url = "https://api.ocr.space/parse/image"
    files = {"file": ("image.jpg", image_bytes)}
    data = {
        "apikey": OCR_API_KEY,
        "language": "eng",
        "OCREngine": "2",
        "isOverlayRequired": "false",
        "scale": "true",
    }
    r = requests.post(url, files=files, data=data, timeout=90)
    r.raise_for_status()
    out = r.json()

    if out.get("IsErroredOnProcessing"):
        msg = out.get("ErrorMessage") or out.get("ErrorDetails") or "OCR error"
        raise RuntimeError(f"OCR.space error: {msg}")

    parsed = out.get("ParsedResults") or []
    if not parsed:
        return ""
    return parsed[0].get("ParsedText", "") or ""

def parse_betinasia_orders(ocr_text: str) -> List[Dict[str, Any]]:
    """
    Heurystyka pod BetInAsia 'My Orders' (single).
    Zwraca listę orderów: {desc, odds, stake, currency, profit, status_guess}
    status_guess: OPEN/W/L (W/L tylko jeśli na screenie był profit +/−)
    """
    text = ocr_text.replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    vs_idx = [i for i, ln in enumerate(lines) if "vs." in ln.lower() or " vs " in ln.lower()]
    if not vs_idx:
        return []

    blocks = []
    for j, start in enumerate(vs_idx):
        end = vs_idx[j + 1] if j + 1 < len(vs_idx) else len(lines)
        blocks.append(lines[start:end])

    results: List[Dict[str, Any]] = []

    for blk in blocks:
        block_text = "\n".join(blk)
        event = blk[0]

        market = ""
        for ln in blk[1:8]:
            lnl = ln.lower()
            if "2nd set" in lnl or "(2nd" in lnl or "set)" in lnl:
                market = ln
                break

        odds_m = _ODDS_RE.search(block_text)
        odds = float(odds_m.group(0)) if odds_m else None

        money = _MONEY_RE.findall(block_text)
        stake = None
        currency = None
        if money:
            currency = money[0][0]
            stake = float(money[0][1])

        profit = None
        status_guess = "OPEN"
        pm = _PROFIT_RE.search(block_text)
        if pm:
            sign = pm.group(1)
            currency = pm.group(2)
            val = float(pm.group(3))
            profit = val if sign == "+" else -val
            status_guess = "W" if profit > 0 else "L"

        if odds is None or stake is None:
            continue

        desc = event + (f" ({market})" if market else "")
        results.append({
            "desc": desc,
            "odds": odds,
            "stake": stake,
            "currency": currency or "$",
            "profit": profit,
            "status_guess": status_guess,
        })

    return results

def auto_add_and_settle_from_orders(chat_id: int, orders: List[Dict[str, Any]], image_hash: str) -> str:
    db = load_db()
    c = ensure_chat(db, chat_id)

    # anty-duplikat (Telegram czasem wysyła ponownie)
    seen = c["meta"].setdefault("ocr_seen_hashes", [])
    if image_hash in seen:
        return "⚠️ Ten screen już był przetworzony (duplikat)."
    seen.append(image_hash)
    # ogranicz listę
    if len(seen) > 50:
        del seen[:-50]

    dt = now_local()
    day_key, sess = session_key_for_time(dt)
    day = ensure_day(db, chat_id, day_key)

    # poranek gate
    today_key = date.today().isoformat()
    day_today = ensure_day(db, chat_id, today_key)
    if not day_today.get("prep_done", False):
        save_db(db)
        return "⛔ Najpierw poranek (/tasks → /done 1 /done 2 → /ready). Dopiero potem OCR zapisuje zakłady."

    s = day["sessions"][sess]
    if s.get("locked", False):
        reason = (s.get("lock_reason") or "brak powodu").strip()
        save_db(db)
        return f"🛑 Sesja zablokowana ({session_label(sess)}).\nPowód: {reason}\n\n{STOP_MSG}"

    created = 0
    settled = 0
    msgs: List[str] = []

    for o in orders:
        new_id = int(day.get("next_bet_id", 1))
        day["next_bet_id"] = new_id + 1

        bet = {
            "id": new_id,
            "session": sess,
            "desc": o["desc"],
            "odds": float(o["odds"]),
            "stake": float(o["stake"]),
            "status": "OPEN",
            "created_at": dt.isoformat(timespec="seconds"),
            "source": "OCR_BETINASIA",
            "currency": o.get("currency") or "$",
            "raw_profit": o.get("profit"),
        }
        day["bets"].append(bet)
        created += 1

        if o.get("status_guess") in ("W", "L"):
            res = o["status_guess"]
            bet["status"] = res
            s["settle_seq"] = int(s.get("settle_seq", 0)) + 1
            bet["settle_seq"] = s["settle_seq"]
            bet["settled_at"] = dt.isoformat(timespec="seconds")
            settled += 1

            recompute_session(day, sess)
            check_and_lock(day, session=sess, last_result=res)

        msgs.append(f"#{new_id} [{bet['status']}] {bet['stake']} {bet['currency']} @ {bet['odds']:.3f} — {bet['desc'][:90]}")
        if day["sessions"][sess].get("locked"):
            break

    day["updated_at"] = dt.isoformat(timespec="seconds")
    save_db(db)

    out = [
        f"📷 OCR → zapisane: {created} | auto-rozliczone: {settled}",
        f"Data: {day_key} | Sesja: {session_label(sess)}",
        "",
        "Dodane:",
        *msgs[:15],
    ]
    if len(msgs) > 15:
        out.append("…(ucięte)")

    out += ["", session_summary_text(day, day_key, sess)]
    if day["sessions"][sess].get("locked"):
        out += ["", STOP_MSG]

    return "\n".join(out)

# ============================================================
# SCHEDULER (auto-raporty) - w tle, nie blokuje webhooka
# ============================================================
def previous_month_ym(dt: datetime) -> str:
    first = dt.replace(day=1)
    prev_last = first - timedelta(days=1)
    return prev_last.strftime("%Y-%m")

def scheduler_tick(db: Dict[str, Any]) -> None:
    dt = now_local()
    m = minutes_of_day(dt)

    for chat_id_str, c in db.get("chats", {}).items():
        try:
            chat_id = int(chat_id_str)
        except Exception:
            continue

        meta = c.setdefault("meta", {})
        last_session_report = meta.setdefault("last_session_report", {})
        last_month_report = meta.setdefault("last_month_report", "")

        # koniec AM o 13:30
        if m == AM_END_MIN:
            day_key = dt.date().isoformat()
            key = f"{day_key}_AM"
            if not last_session_report.get(key):
                day = ensure_day(db, chat_id, day_key)
                msg = session_summary_text(day, day_key, "AM")
                send(chat_id, "🤖 AUTO — koniec sesji AM\n\n" + msg)
                last_session_report[key] = dt.isoformat(timespec="seconds")

        # koniec PM o 02:00 (dla wczoraj)
        if m == AM_START_MIN:
            day_key = (dt.date() - timedelta(days=1)).isoformat()
            key = f"{day_key}_PM"
            if not last_session_report.get(key):
                day = ensure_day(db, chat_id, day_key)
                msg = session_summary_text(day, day_key, "PM")
                send(chat_id, "🤖 AUTO — koniec sesji PM\n\n" + msg)
                last_session_report[key] = dt.isoformat(timespec="seconds")

        # 1. dzień miesiąca 00:10 — podsumowanie poprzedniego
        if dt.day == 1 and dt.hour == 0 and dt.minute == 10:
            ym_prev = previous_month_ym(dt)
            if last_month_report != ym_prev:
                msg = month_summary_text(db, chat_id, ym_prev)
                send(chat_id, "🤖 AUTO — podsumowanie miesiąca\n\n" + msg)
                meta["last_month_report"] = ym_prev

def scheduler_loop() -> None:
    last_min = None
    while True:
        try:
            db = load_db()
            dt = now_local()
            cur_min = (dt.year, dt.month, dt.day, dt.hour, dt.minute)
            if cur_min != last_min:
                scheduler_tick(db)
                save_db(db)
                last_min = cur_min
        except Exception as e:
            print("scheduler error:", e)
        time.sleep(2)

# ============================================================
# MESSAGE HANDLER (komendy + OCR)
# ============================================================
def handle_text(chat_id: int, text: str) -> None:
    db = load_db()
    c = ensure_chat(db, chat_id)

    if text in ("/start", "/help"):
        send(chat_id, HELP_TEXT)
        return

    if text == "/tasks":
        send(chat_id, tasks_text(c["tasks"]))
        return

    if text.startswith("/done"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            send(chat_id, "Użycie: /done 1  lub  /done 2")
            return
        idx = int(parts[1])
        if idx < 1 or idx > len(c["tasks"]):
            send(chat_id, "Nie ma takiego numeru.")
            return
        c["tasks"][idx - 1]["done"] = True
        save_db(db)
        send(chat_id, "✅ Odhaczone.\n\n" + tasks_text(c["tasks"]))
        return

    if text == "/ready":
        if not morning_done(db, chat_id):
            send(chat_id, "⛔ Jeszcze nie. Najpierw odhacz oba zadania.\n\n" + tasks_text(c["tasks"]))
            return
        today = date.today().isoformat()
        day = ensure_day(db, chat_id, today)
        day["prep_done"] = True
        day["updated_at"] = now_local().isoformat(timespec="seconds")
        save_db(db)
        send(chat_id, "🔓 Poranek zaliczony. Możesz dodać zakłady ✅")
        return

    if text == "/challenge":
        send(chat_id, challenge_text(db, chat_id))
        return

    if text.startswith("/monthdetails"):
        parts = text.split()
        ym = parts[1].strip() if len(parts) == 2 else None
        send(chat_id, month_details_text(db, chat_id, ym))
        return

    if text.startswith("/month"):
        parts = text.split()
        ym = parts[1].strip() if len(parts) == 2 else None
        send(chat_id, month_summary_text(db, chat_id, ym=ym))
        return

    if text.startswith("/bet"):
        today = date.today().isoformat()
        day_today = ensure_day(db, chat_id, today)
        if not day_today.get("prep_done", False):
            send(chat_id, MORNING_BLOCK_MSG)
            return

        parsed = parse_bet(text)
        if not parsed:
            send(chat_id, "Użycie: /bet opis | kurs | stawka\nPrzykład: /bet Birrell wygra seta | 2.15 | 200")
            return

        dt = now_local()
        day_key, sess = session_key_for_time(dt)
        day = ensure_day(db, chat_id, day_key)

        s = day["sessions"][sess]
        if s.get("locked", False):
            reason = (s.get("lock_reason") or "brak powodu").strip()
            send(chat_id, f"🛑 Sesja zablokowana ({session_label(sess)}).\nPowód: {reason}\n\n{STOP_MSG}")
            return

        desc, odds, stake = parsed
        new_id = int(day.get("next_bet_id", 1))
        day["next_bet_id"] = new_id + 1

        day["bets"].append({
            "id": new_id,
            "session": sess,
            "desc": desc,
            "odds": odds,
            "stake": stake,
            "status": "OPEN",
            "created_at": dt.isoformat(timespec="seconds"),
            "source": "MANUAL",
        })

        day["updated_at"] = dt.isoformat(timespec="seconds")
        save_db(db)
        send(chat_id, f"✅ Dodano zakład #{new_id} ({day_key}, {session_label(sess)})\n{desc}\nKurs: {odds} | Stawka: {stake}")
        return

    if text == "/list":
        dt = now_local()
        day_key, _ = session_key_for_time(dt)
        day = ensure_day(db, chat_id, day_key)
        send(chat_id, list_text(day, day_key))
        return

    if text.startswith("/settle"):
        parts = text.split()
        if len(parts) != 3 or (not parts[1].isdigit()) or parts[2].upper() not in ("W", "L"):
            send(chat_id, "Użycie: /settle ID W  lub  /settle ID L")
            return

        bet_id = int(parts[1])
        res = parts[2].upper()

        dt = now_local()
        day_key, _ = session_key_for_time(dt)
        day = ensure_day(db, chat_id, day_key)

        b = find_bet(day, bet_id)
        if not b:
            send(chat_id, f"Nie ma takiego ID w {day_key}. Użyj /list.")
            return
        if b.get("status") in ("W", "L"):
            send(chat_id, "Ten zakład jest już rozliczony.")
            return

        sess = b.get("session", "AM")
        s = day["sessions"][sess]

        b["status"] = res
        s["settle_seq"] = int(s.get("settle_seq", 0)) + 1
        b["settle_seq"] = s["settle_seq"]
        b["settled_at"] = dt.isoformat(timespec="seconds")

        recompute_session(day, sess)
        check_and_lock(day, session=sess, last_result=res)

        day["updated_at"] = dt.isoformat(timespec="seconds")
        save_db(db)

        out = [
            f"✅ Rozliczono #{bet_id}: {res} ({day_key}, {session_label(sess)})",
            "",
            session_summary_text(day, day_key, sess),
        ]
        if day["sessions"][sess].get("locked"):
            out += ["", STOP_MSG]

        send(chat_id, "\n".join(out))
        return

    if text.startswith("/delete"):
        parts = text.split()
        if len(parts) != 2 or (not parts[1].isdigit()):
            send(chat_id, "Użycie: /delete ID")
            return

        bet_id = int(parts[1])

        dt = now_local()
        day_key, _ = session_key_for_time(dt)
        day = ensure_day(db, chat_id, day_key)

        b = find_bet(day, bet_id)
        if not b:
            send(chat_id, f"Nie ma takiego ID w {day_key}.")
            return

        day["bets"] = [x for x in day["bets"] if int(x.get("id", -1)) != bet_id]
        for sess in ("AM", "PM"):
            recompute_session(day, sess)
            recompute_loss_streak(day, sess)

        day["updated_at"] = dt.isoformat(timespec="seconds")
        save_db(db)
        send(chat_id, f"🗑 Usunięto zakład #{bet_id} z {day_key}.")
        return

    if text == "/clearday":
        today = date.today().isoformat()
        c["days"][today] = {
            "bets": [],
            "next_bet_id": 1,
            "sessions": {"AM": empty_session_state(), "PM": empty_session_state()},
            "prep_done": False,
            "updated_at": now_local().isoformat(timespec="seconds"),
        }
        c["tasks"] = [dict(t) for t in DEFAULT_TASKS]
        save_db(db)
        send(chat_id, "🧹 Wyczyściłem DZISIAJ (poranek + sesje).")
        return

    if text == "/day":
        dt = now_local()
        day_key, _ = session_key_for_time(dt)
        send(chat_id, day_summary_text(db, chat_id, day_key) + "\n\n" + challenge_text(db, chat_id))
        return

    if text == "/endday":
        dt = now_local()
        day_key, _ = session_key_for_time(dt)
        send(chat_id, "🌙 Koniec dnia\n\n" + day_summary_text(db, chat_id, day_key) + "\n\n" + challenge_text(db, chat_id))
        return

    send(chat_id, "Nie rozumiem. Użyj /help żeby zobaczyć komendy.")

# ============================================================
# OCR job (w tle)
# ============================================================
def process_photo_ocr_async(chat_id: int, file_id: str) -> None:
    try:
        if not OCR_API_KEY:
            send(chat_id, "❌ OCR nie działa, bo nie ma OCR_API_KEY w Render → Environment.")
            return

        img = get_file_bytes(file_id)
        img_hash = hashlib.sha256(img).hexdigest()[:16]

        send(chat_id, "📷 OCR: przetwarzam screena… (to może potrwać ~10–30s)")

        txt = ocr_space(img)
        if not txt.strip():
            send(chat_id, "❌ OCR: nie udało się odczytać tekstu ze screena.")
            return

        orders = parse_betinasia_orders(txt)
        if not orders:
            send(chat_id, "❌ OCR: nie wykryłem żadnych orderów (upewnij się, że screen to 'My Orders' i widać kurs + stawkę).")
            return

        result = auto_add_and_settle_from_orders(chat_id, orders, image_hash=img_hash)
        send(chat_id, result)

    except Exception as e:
        send(chat_id, f"❌ OCR error: {e}")

# ============================================================
# WEBHOOK ROUTES
# ============================================================
@app.get("/")
def home():
    return "Bot działa ✅", 200

@app.post("/telegram")
def telegram_webhook():
    try:
        update = request.get_json(force=True) or {}
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return jsonify({"ok": True})

        chat_id = msg["chat"]["id"]

        # TEXT
        text = (msg.get("text") or "").strip()
        if text:
            handle_text(chat_id, text)
            return jsonify({"ok": True})

        # PHOTO (OCR)
        # Telegram daje listę rozmiarów - bierzemy największy (ostatni)
        if "photo" in msg and isinstance(msg["photo"], list) and msg["photo"]:
            largest = msg["photo"][-1]
            file_id = largest.get("file_id")
            if file_id:
                # nie blokujemy webhooka
                t = threading.Thread(target=process_photo_ocr_async, args=(chat_id, file_id), daemon=True)
                t.start()
            else:
                send(chat_id, "❌ Nie widzę file_id zdjęcia.")
            return jsonify({"ok": True})

        # DOCUMENT (czasem screenshot idzie jako plik)
        if "document" in msg and isinstance(msg["document"], dict):
            mime = (msg["document"].get("mime_type") or "").lower()
            if "image" in mime:
                file_id = msg["document"].get("file_id")
                if file_id:
                    t = threading.Thread(target=process_photo_ocr_async, args=(chat_id, file_id), daemon=True)
                    t.start()
                else:
                    send(chat_id, "❌ Nie widzę file_id dokumentu.")
                return jsonify({"ok": True})

        send(chat_id, "Wyślij komendę /help albo screenshot (photo) do OCR.")
        return jsonify({"ok": True})

    except Exception as e:
        print("webhook error:", e)
        return jsonify({"ok": True})

# ============================================================
# START background scheduler thread ON IMPORT (Render/gunicorn)
# ============================================================
_scheduler_started = False
def start_scheduler_once():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    th = threading.Thread(target=scheduler_loop, daemon=True)
    th.start()

start_scheduler_once()

# ============================================================
# Local run (optional)
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
