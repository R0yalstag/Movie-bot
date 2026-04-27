"""
Telegram Movie Watchlist Bot — TMDb edition.
Uses The Movie Database (TMDb) for up-to-date movie info,
Flask keepalive for Render free tier, and inline buttons
for picking between same-named movies.
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
TMDB_API_KEY   = os.environ["TMDB_API_KEY"]
DB_PATH        = os.environ.get("DB_PATH", "watchlist.db")
TMDB_BASE      = "https://api.themoviedb.org/3"
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
    """Search TMDb for movies matching the query."""
    try:
        r = requests.get(
            f"{TMDB_BASE}/search/movie",
            params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "false"},
            timeout=10,
        ).json()
    except requests.RequestException:
        return []
    results = r.get("results", [])
    # Sort by popularity (TMDb already does this, but make it explicit)
    results.sort(key=lambda m: m.get("popularity", 0), reverse=True)
    return results[:max_results]


def fetch_movie_by_id(tmdb_id):
    """Fetch full movie details using TMDb ID, including cast."""
    try:
        r = requests.get(
            f"{TMDB_BASE}/movie/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "append_to_response": "credits"},
            timeout=10,
        ).json()
    except requests.RequestException:
        return None
    if not r.get("id"):
        return None

    cast = r.get("credits", {}).get("cast", [])
    actor = cast[0]["name"] if cast else "N/A"
    genres = ", ".join(g["name"] for g in r.get("genres", [])) or "N/A"
    rating = r.get("vote_average")
    rating_str = f"{rating:.1f}" if rating else "N/A"
    year = (r.get("release_date") or "????")[:4]

    return {
        "title":   r.get("title", "Unknown"),
        "year":    year,
        "rating":  rating_str,
        "genre":   genres,
        "actor":   actor,
        "tmdb_id": str(r.get("id")),
    }


def format_movie(m: dict) -> str:
    return (
        f"🎬 *{m['title']}* ({m['year']})\n"
        f"⭐ TMDb: {m['rating']}/10\n"
        f"🎭 Genre: {m['genre']}\n"
        f"👤 Lead: {m['actor']}"
    )


def add_button(tmdb_id: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ Add to watchlist", callback_data=f"add:{tmdb_id}")]]
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
        movie = fetch_movie_by_id(results[0]["id"])
        if movie:
            await update.message.reply_text(
                format_movie(movie),
                parse_mode="Markdown",
                reply_markup=add_button(movie["tmdb_id"]),
            )
        return

    # Multiple results → show pickable buttons
    keyboard = [
        [InlineKeyboardButton(
            f"{m.get('title','?')} ({(m.get('release_date') or '????')[:4]})",
            callback_data=f"info:{m['id']}",
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
        action, tmdb_id = q.data.split(":", 1)
    except ValueError:
        return

    movie = fetch_movie_by_id(tmdb_id)
    if not movie:
        await q.edit_message_text("❌ Couldn't fetch details.")
        return

    if action == "info":
        await q.edit_message_text(
            format_movie(movie),
            parse_mode="Markdown",
            reply_markup=add_button(tmdb_id),
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
