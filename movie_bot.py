"""
Telegram Movie Watchlist Bot — Render free tier version.
Adds a tiny Flask endpoint so Render's web service tier is happy
and UptimeRobot can ping it to prevent sleep.
"""

import os
import sqlite3
import threading
import requests
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)

# ---------- CONFIG (from env vars) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OMDB_API_KEY   = os.environ["OMDB_API_KEY"]
DB_PATH        = os.environ.get("DB_PATH", "watchlist.db")
# --------------------------------------------


# ---------- Tiny keepalive web server ----------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "🎬 Movie watchlist bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
# -----------------------------------------------


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title   TEXT,
                year    TEXT,
                rating  TEXT,
                genre   TEXT,
                actor   TEXT,
                UNIQUE(user_id, title)
            )
        """)


def fetch_movie(title: str):
    try:
        r = requests.get(
            "http://www.omdbapi.com/",
            params={"t": title, "apikey": OMDB_API_KEY},
            timeout=10,
        ).json()
    except requests.RequestException:
        return None
    if r.get("Response") == "False":
        return None
    return {
        "title":  r.get("Title", "Unknown"),
        "year":   r.get("Year", "N/A"),
        "rating": r.get("imdbRating", "N/A"),
        "genre":  r.get("Genre", "N/A"),
        "actor":  (r.get("Actors") or "N/A").split(",")[0].strip(),
    }


def format_movie(m: dict) -> str:
    return (
        f"🎬 *{m['title']}* ({m['year']})\n"
        f"⭐ IMDb: {m['rating']}\n"
        f"🎭 Genre: {m['genre']}\n"
        f"👤 Lead: {m['actor']}"
    )


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! Send me any movie name to get its details.\n\n"
        "Commands:\n"
        "  /add <n>   – save a movie to your watchlist\n"
        "  /list         – show your watchlist\n"
        "  /remove <id>  – remove a movie by its list id\n"
        "  /clear        – clear the whole watchlist"
    )


async def lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    movie = fetch_movie(title)
    if not movie:
        await update.message.reply_text("❌ Movie not found. Check the spelling.")
        return
    await update.message.reply_text(
        format_movie(movie) + f"\n\n_Use_ `/add {movie['title']}` _to save it_",
        parse_mode="Markdown",
    )


async def add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /add <movie name>")
        return
    title = " ".join(ctx.args)
    movie = fetch_movie(title)
    if not movie:
        await update.message.reply_text("❌ Movie not found.")
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO watchlist (user_id,title,year,rating,genre,actor) "
                "VALUES (?,?,?,?,?,?)",
                (update.effective_user.id, movie["title"], movie["year"],
                 movie["rating"], movie["genre"], movie["actor"]),
            )
        await update.message.reply_text(
            f"✅ Added *{movie['title']}* to your watchlist.",
            parse_mode="Markdown",
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text("⚠️ That movie is already in your list.")


async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id,title,year,rating,genre FROM watchlist WHERE user_id=? ORDER BY id",
            (update.effective_user.id,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("📭 Your watchlist is empty.")
        return
    lines = ["📋 *Your Watchlist*\n"]
    for rid, title, year, rating, genre in rows:
        lines.append(f"`{rid}.` {title} ({year}) — ⭐{rating} — _{genre}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /remove <id>  (id from /list)")
        return
    mid = int(ctx.args[0])
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM watchlist WHERE id=? AND user_id=?",
            (mid, update.effective_user.id),
        )
    if cur.rowcount:
        await update.message.reply_text("🗑️ Removed.")
    else:
        await update.message.reply_text("Nothing matched that id.")


async def clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM watchlist WHERE user_id=?", (update.effective_user.id,))
    await update.message.reply_text("🧹 Watchlist cleared.")


def main():
    init_db()

    # Start keepalive server in background thread
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("add",    add))
    app.add_handler(CommandHandler("list",   show_list))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("clear",  clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup))
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
