"""
Telegram Movie Watchlist Bot — Render free tier version.
Now with multi-result search + inline buttons for disambiguation.
"""

import os
import sqlite3
import threading
import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
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
                UNIQUE(user_id, title, year)
            )
        """)


def search_movies(query: str, max_results: int = 6):
    """Search OMDb for multiple movies matching the query."""
    try:
        r = requests.get(
            "http://www.omdbapi.com/",
            params={"s": query, "type": "movie", "apikey": OMDB_API_KEY},
            timeout=10,
        ).json()
    except requests.RequestException:
        return []
    if r.get("Response") == "False":
        return []
    # Sort newest first so 2026 versions come above 1997 versions
    results = r.get("Search", [])
    results.sort(key=lambda m: m.get("Year", "0"), reverse=True)
    return results[:max_results]


def fetch_movie_by_id(imdb_id: str):
    """Fetch full movie details using its IMDb ID."""
    try:
        r = requests.get(
            "http://www.omdbapi.com/",
            params={"i": imdb_id, "apikey": OMDB_API_KEY},
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
        "imdb_id": r.get("imdbID", imdb_id),
    }


def format_movie(m: dict) -> str:
    return (
        f"🎬 *{m['title']}* ({m['year']})\n"
        f"⭐ IMDb: {m['rating']}\n"
        f"🎭 Genre: {m['genre']}\n"
        f"👤 Lead: {m['actor']}"
    )


def add_button(imdb_id: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ Add to watchlist", callback_data=f"add:{imdb_id}")]]
    )


# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! Send me any movie name to get its details.\n\n"
        "If multiple movies share the name, I'll show you all matches.\n\n"
        "Commands:\n"
        "  /list   – show your watchlist\n"
        "  /remove <id> – remove by id\n"
        "  /clear  – wipe watchlist"
    )


async def lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    results = search_movies(query)

    if not results:
        await update.message.reply_text("❌ No movies found. Check the spelling.")
        return

    # Single result → show details right away
    if len(results) == 1:
        movie = fetch_movie_by_id(results[0]["imdbID"])
        if movie:
            await update.message.reply_text(
                format_movie(movie),
                parse_mode="Markdown",
                reply_markup=add_button(movie["imdb_id"]),
            )
        return

    # Multiple results → show pickable buttons
    keyboard = [
        [InlineKeyboardButton(
            f"{m.get('Title','?')} ({m.get('Year','?')})",
            callback_data=f"info:{m['imdbID']}",
        )]
        for m in results
    ]
    await update.message.reply_text(
        f"Found {len(results)} matches for *{query}*. Pick one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        action, imdb_id = q.data.split(":", 1)
    except ValueError:
        return

    movie = fetch_movie_by_id(imdb_id)
    if not movie:
        await q.edit_message_text("❌ Couldn't fetch details.")
        return

    if action == "info":
        await q.edit_message_text(
            format_movie(movie),
            parse_mode="Markdown",
            reply_markup=add_button(imdb_id),
        )

    elif action == "add":
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO watchlist (user_id,title,year,rating,genre,actor) "
                    "VALUES (?,?,?,?,?,?)",
                    (q.from_user.id, movie["title"], movie["year"],
                     movie["rating"], movie["genre"], movie["actor"]),
                )
            await q.edit_message_text(
                format_movie(movie) + "\n\n✅ Added to your watchlist!",
                parse_mode="Markdown",
            )
        except sqlite3.IntegrityError:
            await q.edit_message_text(
                format_movie(movie) + "\n\n⚠️ Already in your list.",
                parse_mode="Markdown",
            )


async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id,title,year,rating,genre FROM watchlist "
            "WHERE user_id=? ORDER BY id",
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
        await update.message.reply_text("Usage: /remove <id> (id from /list)")
        return
    mid = int(ctx.args[0])
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM watchlist WHERE id=? AND user_id=?",
            (mid, update.effective_user.id),
        )
    await update.message.reply_text(
        "🗑️ Removed." if cur.rowcount else "Nothing matched that id."
    )


async def clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM watchlist WHERE user_id=?",
                     (update.effective_user.id,))
    await update.message.reply_text("🧹 Watchlist cleared.")


def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("list",   show_list))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("clear",  clear))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup))
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
