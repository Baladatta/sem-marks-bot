## Removed duplicate import of Application, CommandHandler, ContextTypes


# Function to calculate future attendance
def future_attendance(attended, total, future_attend, future_total, target=75):
    A_new = attended + future_attend
    T_new = total + future_total
    final_percent = (A_new / T_new) * 100
    max_skip = (A_new - (target/100) * T_new) / (target/100)
    max_skip = int(max_skip) if max_skip > 0 else 0
    return A_new, T_new, round(final_percent, 2), max_skip

# /future command
#!/usr/bin/env python3
"""
Telegram bot: marks calculator + YouTube search + persistent storage (SQLite).
Requires:
  - python-telegram-bot==20.4
  - requests
Env:
  - BOT_TOKEN
  - YOUTUBE_API_KEY
  - DATABASE_URL (optional; default sqlite file students.db)
"""

import os
import sqlite3
import logging
import math
import urllib.parse
import requests
from typing import List, Tuple

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
(MID1, MID2, WEEKLIES, CONFIRM_SAVE) = range(4)

# DB helper
def get_db_connection():
    db_path = os.environ.get("DATABASE_URL")
    # If DATABASE_URL provided and it's sqlite file path like sqlite:///... handle, else default to local sqlite file
    if db_path and db_path.startswith("sqlite:///"):
        path = db_path.replace("sqlite:///", "")
    else:
        path = os.environ.get("DATABASE_URL") or "students.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS students (
               tg_id INTEGER PRIMARY KEY,
               name TEXT,
               mid1 REAL,
               mid2 REAL,
               weekly TEXT,       -- comma-separated floats
               last_internals REAL
           );"""
    )
    conn.commit()
    return conn

DB = get_db_connection()

# ---- Core calculation functions ----
def compute_mids_component(mid1: float, mid2: float) -> float:
    """
    According to rule:
      - Take 80% of the higher mid
      - Take 20% of the lower mid
      - Total marks from mids scaled to 25
    Assumption: mid marks are out of 25 each? The user didn't state per-mid maximum.
    We'll assume mid exams are scored out of 25 each (common). If different, adapt.
    We'll compute weighted average then scale to 25 (i.e., treat mid raw out of 25).
    """
    # Take high and low
    high = mid1 if mid1 >= mid2 else mid2
    low = mid2 if mid1 >= mid2 else mid1
    weighted = 0.8 * high + 0.2 * low  # still out of same max as mid marks
    # If mid max is 25, weighted is directly out of 25 -> mids_component = weighted
    # To be robust, we return weighted (interpreted as out of 25).
    return weighted

def compute_weekly_component(weeklies: List[float]) -> float:
    """
    Weekly tests:
      - 7-8 tests; pick best 4, average them
      - Then scale result to 5 marks (weekly contribution)
    We'll assume weekly test raw marks are out of 5 (or 10?) — user didn't specify.
    Approach: We compute average of best 4, then map to 5 by:
       (avg_best4 / max_weekly_raw) * 5
    But since max isn't specified, we'll **assume weekly tests are out of 5** (common small tests).
    So the average of best4 is already out of 5 -> weekly_component = avg_best4.
    If your weekly tests are out of different max, change code to scale accordingly.
    """
    if not weeklies:
        return 0.0
    sorted_marks = sorted(weeklies, reverse=True)
    best4 = sorted_marks[:4] if len(sorted_marks) >= 4 else sorted_marks
    avg_best4 = sum(best4) / len(best4)
    # Assume weekly raw maximum equals 5, so weekly component (out of 5) = avg_best4
    return avg_best4

def compute_internals(mid1: float, mid2: float, weeklies: List[float]) -> Tuple[float,float,float]:
    mids_comp = compute_mids_component(mid1, mid2)  # out of 25
    weekly_comp = compute_weekly_component(weeklies)  # out of 5
    # Internals total of 30
    internals_total = mids_comp + weekly_comp
    # Clamp to 30 maximum
    internals_total = max(0.0, min(internals_total, 30.0))
    # Return components and total
    return mids_comp, weekly_comp, internals_total

def needed_external_to_pass(internals: float, passing_percent: float = 40.0) -> Tuple[float, float]:
    """
    Compute how many marks are needed in external (out of 70) to reach an overall passing percent.
    Default passing_percent = 40 (% of total 100).
    Returns (marks_needed_in_external, minimum_total_required_in_external_if_bound)
    """
    total_needed = (passing_percent / 100.0) * 100.0  # e.g., 40.0
    # student has internals out of 30. External is out of 70. We need:
    # internals + external >= total_needed
    # external_needed = total_needed - internals
    external_needed = total_needed - internals
    # Convert percentage points to marks-out-of-70
    # Wait: total_needed is percentage points (e.g., 40 of 100), internals is raw marks (out of 30).
    # So we compare directly: required_total_marks = (passing_percent/100)*100 = e.g., 40 marks of 100.
    # But internals and externals are in marks, not percent. So:
    required_overall_marks = (passing_percent / 100.0) * 100.0
    # marks_needed_in_external = required_overall_marks - internals
    marks_needed_in_external = required_overall_marks - internals
    # If marks_needed <= 0 -> already passed irrespective of external
    marks_needed_in_external = max(0.0, marks_needed_in_external)
    # However external max is 70: if needed > 70, impossible
    return marks_needed_in_external, min(marks_needed_in_external, 70.0)

# ---- DB helpers ----
def save_student_data(tg_id: int, name: str, mid1: float, mid2: float, weeklies: List[float], internals: float):
    cur = DB.cursor()
    weekly_str = ",".join(str(x) for x in weeklies)
    cur.execute(
        "INSERT OR REPLACE INTO students (tg_id, name, mid1, mid2, weekly, last_internals) VALUES (?, ?, ?, ?, ?, ?);",
        (tg_id, name, mid1, mid2, weekly_str, internals),
    )
    DB.commit()

def load_student_data(tg_id: int):
    cur = DB.cursor()
    cur.execute("SELECT name, mid1, mid2, weekly, last_internals FROM students WHERE tg_id = ?;", (tg_id,))
    row = cur.fetchone()
    if not row:
        return None
    name, mid1, mid2, weekly_str, internals = row
    weeklies = [float(x) for x in weekly_str.split(",")] if weekly_str else []
    return {"name": name, "mid1": mid1, "mid2": mid2, "weeklies": weeklies, "internals": internals}

def reset_student_data(tg_id: int):
    cur = DB.cursor()
    cur.execute("DELETE FROM students WHERE tg_id = ?;", (tg_id,))
    DB.commit()

# ---- YouTube search ----
def youtube_search_links(query: str, api_key: str, max_results: int = 5) -> List[Tuple[str,str]]:
    """
    Return a list of tuples (title, url) for top videos.
    """
    if not api_key:
        return []
    base = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": str(max_results),
        "key": api_key,
        "order": "relevance",
    }
    r = requests.get(base, params=params, timeout=10)
    results = []
    if r.status_code != 200:
        logger.error("YouTube API error: %s %s", r.status_code, r.text)
        return results
    j = r.json()
    for item in j.get("items", []):
        vid = item["id"]["videoId"]
        title = item["snippet"]["title"]
        url = f"https://www.youtube.com/watch?v={vid}"
        results.append((title, url))
    return results

# ---- Bot handlers ----
def future_attendance(attended, total, future_attend, future_total, target=75):
    A_new = attended + future_attend
    T_new = total + future_total
    final_percent = (A_new / T_new) * 100
    max_skip = (A_new - (target/100) * T_new) / (target/100)
    max_skip = int(max_skip) if max_skip > 0 else 0
    return A_new, T_new, round(final_percent, 2), max_skip

async def future(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        attended, total, future_attend, future_total = map(int, context.args)
        A_new, T_new, percent, skip = future_attendance(
            attended, total, future_attend, future_total
        )
        msg = (
            f"\U0001F4CA Attendance Forecast:\n\n"
            f"\u27A1\uFE0F Final: {A_new}/{T_new} ({percent}%)\n"
            f"\u27A1\uFE0F You can skip up to {skip} classes and still stay \u2265 75%."
        )
        await update.message.reply_text(msg)
    except Exception:
        await update.message.reply_text("\u26A0\uFE0F Usage: /future <attended> <total> <future_attend> <future_total>")
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.full_name or user.first_name or "Student"
    reply = (
        f"Hi {name}! 👋\n\n"
        "I can help you calculate internals, forecast attendance, and search YouTube for study topics.\n\n"
        "Commands:\n"
        "/marks - Enter marks and calculate\n"
        "/mystats - Show your saved marks\n"
        "/yt <topic> - Search YouTube for topic (top 5)\n"
        "/reset - Reset your saved marks\n"
        "/future <attended> <total> <future_attend> <future_total> - Attendance forecast\n"
        "/help - Show help\n"
    )
    await update.message.reply_text(reply)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# Conversation to collect marks
async def marks_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Enter your *Mid-1* marks (just number). If not taken, send 0.",
        parse_mode="Markdown",
    )
    return MID1

async def mid1_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        mid1 = float(txt)
        context.user_data["mid1"] = mid1
    except:
        await update.message.reply_text("Please send a valid number for Mid-1 (e.g., 18.5).")
        return MID1
    await update.message.reply_text("Now enter your *Mid-2* marks (just number). If not taken, send 0.", parse_mode="Markdown")
    return MID2

async def mid2_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        mid2 = float(txt)
        context.user_data["mid2"] = mid2
    except:
        await update.message.reply_text("Please send a valid number for Mid-2 (e.g., 20).")
        return MID2
    await update.message.reply_text(
        "Enter your weekly test marks separated by spaces (7 or 8 values). If you haven't taken them, send 0.\nExample: `5 4 4.5 3 5 4 3.5 4`",
        parse_mode="Markdown",
    )
    return WEEKLIES

async def weeklies_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    parts = txt.split()
    weeklies = []
    try:
        for p in parts:
            weeklies.append(float(p))
    except:
        await update.message.reply_text("Please send weekly marks as numbers separated by spaces.")
        return WEEKLIES

    # Save temp and compute
    mid1 = float(context.user_data.get("mid1", 0.0))
    mid2 = float(context.user_data.get("mid2", 0.0))
    mids_comp, weekly_comp, internals_total = compute_internals(mid1, mid2, weeklies)

    # Round sensibly (digit-by-digit caution -> we'll round to 2 decimals)
    mids_comp_rounded = round(mids_comp + 1e-9, 2)
    weekly_comp_rounded = round(weekly_comp + 1e-9, 2)
    internals_total_rounded = round(internals_total + 1e-9, 2)

    # How many marks needed in external to reach 40% overall (default)
    marks_needed, capped_needed = needed_external_to_pass(internals_total_rounded, passing_percent=40.0)
    marks_needed_rounded = round(marks_needed + 1e-9, 2)

    reply = (
        f"📝 *Calculation Results:*\n\n"
        f"Mid component (out of 25): {mids_comp_rounded}\n"
        f"Weekly component (out of 5): {weekly_comp_rounded}\n"
        f"*Internals total (out of 30):* {internals_total_rounded}\n\n"
        f"To reach *40% overall* (i.e., 40 marks out of 100), you need *{marks_needed_rounded}* marks in the external (out of 70).\n"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")

    # Ask to save
    context.user_data["weeklies"] = weeklies
    context.user_data["internals_total"] = internals_total_rounded
    reply2 = "Do you want me to save this data for you? (Yes/No)"
    await update.message.reply_text(reply2)
    return CONFIRM_SAVE

async def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt in ("yes", "y", "sure"):
        user = update.effective_user
        name = user.full_name or user.first_name or "Student"
        mid1 = float(context.user_data.get("mid1", 0.0))
        mid2 = float(context.user_data.get("mid2", 0.0))
        weeklies = context.user_data.get("weeklies", [])
        internals = float(context.user_data.get("internals_total", 0.0))
        try:
            save_student_data(update.effective_user.id, name, mid1, mid2, weeklies, internals)
            await update.message.reply_text("Saved ✅. You can later view with /mystats")
        except Exception as e:
            logger.exception("Error saving: %s", e)
            await update.message.reply_text("Sorry, I couldn't save your data (internal error).")
    else:
        await update.message.reply_text("Okay, not saved.")
    # clear temporary user_data
    context.user_data.pop("mid1", None)
    context.user_data.pop("mid2", None)
    context.user_data.pop("weeklies", None)
    context.user_data.pop("internals_total", None)
    return ConversationHandler.END

async def marks_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Marks entry cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# /mystats
async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    row = load_student_data(tg_id)
    if not row:
        await update.message.reply_text("No saved data found. Use /marks to compute and save your data.")
        return
    mid1 = row["mid1"]
    mid2 = row["mid2"]
    weeklies = row["weeklies"]
    internals = row["internals"]
    mids_comp, weekly_comp, computed_total = compute_internals(mid1, mid2, weeklies)
    mids_comp = round(mids_comp + 1e-9, 2)
    weekly_comp = round(weekly_comp + 1e-9, 2)
    computed_total = round(computed_total + 1e-9, 2)
    marks_needed, _ = needed_external_to_pass(computed_total, passing_percent=40.0)
    marks_needed = round(marks_needed + 1e-9, 2)

    wk_display = " ".join(str(x) for x in weeklies) if weeklies else "No weekly marks"
    resp = (
        f"Saved data for {row['name']}:\n\n"
        f"Mid-1: {mid1}\nMid-2: {mid2}\n"
        f"Weeklies: {wk_display}\n\n"
        f"Computed mids component (out of 25): {mids_comp}\n"
        f"Computed weekly component (out of 5): {weekly_comp}\n"
        f"Internals (out of 30): {computed_total}\n\n"
        f"Marks needed in external (out of 70) to reach 40% overall: *{marks_needed}*"
    )
    await update.message.reply_text(resp, parse_mode="Markdown")

# /reset
async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_student_data(update.effective_user.id)
    await update.message.reply_text("Your saved data has been reset.")

# /yt search
async def yt_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    parts = txt.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /yt <topic>. Example: /yt Data Structures linked lists")
        return
    query = parts[1].strip()
    await update.message.reply_text(f"Searching YouTube for: {query} ...")
    # Hardcoded YouTube API key for local testing
    api_key = "AIzaSyCvQlXoUGCjbt9A1El7I-_5BKsCnjqvcNY"
    try:
        results = youtube_search_links(query, api_key, max_results=5)
    except Exception as e:
        logger.exception("YouTube search failed: %s", e)
        results = []
    if not results:
        await update.message.reply_text("No results or API error. Make sure YOUTUBE_API_KEY is set and valid.")
        return
    reply = "Top YouTube results:\n\n"
    for title, url in results:
        # shorten title to avoid long messages
        t = title if len(title) <= 80 else title[:77] + "..."
        reply += f"• {t}\n{url}\n\n"
    await update.message.reply_text(reply)

# Unknown messages
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sorry, I didn't understand that. Type /help for commands.")

# Main
def main():
    # Hardcoded BOT_TOKEN for local testing
    token = "8242785354:AAGXvN7i2GLAsexnmeD0CSq7TBZ3mWuqTFE"
    application = ApplicationBuilder().token(token).build()

    # Marks conversation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("marks", marks_entry)],
        states={
            MID1: [MessageHandler(filters.TEXT & ~filters.COMMAND, mid1_received)],
            MID2: [MessageHandler(filters.TEXT & ~filters.COMMAND, mid2_received)],
            WEEKLIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, weeklies_received)],
            CONFIRM_SAVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_save)],
        },
        fallbacks=[CommandHandler("cancel", marks_cancel)],
        allow_reentry=True,
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("mystats", mystats))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("yt", yt_search))
    application.add_handler(CommandHandler("future", future))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
