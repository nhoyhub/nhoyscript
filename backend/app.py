import os
import json
import requests
import pathlib
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
from bson.errors import InvalidId
from werkzeug.utils import secure_filename
import base64
from datetime import timedelta

# =========================
# Load ENV
# =========================
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key")

# =========================
# App Setup
# =========================
# frontend/ folder is one level above this file (backend/frontend structure)
frontend_path = pathlib.Path(__file__).parent.parent / "frontend"

app = Flask(__name__, static_folder=str(frontend_path))
app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24),
)

# CORS (for Vercel + Render + local)
CORS(
    app,
    supports_credentials=True,
    origins=[
        "https://nhoyscript.vercel.app",
        "https://nhoyscript.onrender.com",
        "http://127.0.0.1:5000",
        "http://localhost:5000",
    ],
)

# =========================
# MongoDB
# =========================
try:
    client = MongoClient(MONGO_URI)
    db = client["nhoy_hub"]

    scripts_collection = db["scripts"]
    accounts_collection = db["accounts"]

    print("✅ MongoDB Connected")

    # Seed scripts if empty
    if scripts_collection.count_documents({}) == 0 and os.path.exists("default_scripts.json"):
        with open("default_scripts.json", "r", encoding="utf-8") as f:
            default_scripts = json.load(f)
        scripts_collection.insert_many(default_scripts)
        print(f"✅ Default Scripts Imported: {len(default_scripts)}")

    # Seed accounts if empty
    if accounts_collection.count_documents({}) == 0 and os.path.exists("default_accounts.json"):
        with open("default_accounts.json", "r", encoding="utf-8") as f:
            default_accounts = json.load(f)
        accounts_collection.insert_many(default_accounts)
        print(f"✅ Default Accounts Imported: {len(default_accounts)}")

except Exception as e:
    print("❌ MongoDB Error:", e)


# =========================
# Telegram Notify
# =========================
def send_telegram_notification(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠ Telegram config missing. Skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
    except Exception as e:
        print("❌ Telegram Error:", e)


# =========================
# Auth Helper
# =========================
def check_admin_auth() -> bool:
    return session.get("is_admin") is True


# =========================
# Frontend Routes
# =========================
@app.route("/")
def index():
    # main public page
    return send_from_directory(app.static_folder, "index.html")


@app.route("/admin")
def admin():
    # admin dashboard page
    return send_from_directory(app.static_folder, "admin.html")


# =========================
# AUTH API
# =========================
@app.route("/api/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    password = data.get("password")

    if password == ADMIN_PASSWORD:
        session["is_admin"] = True
        session.permanent = True

        send_telegram_notification(
            f"🔐 *Admin Login Success!*\nIP: `{request.remote_addr}`"
        )

        return jsonify({"success": True}), 200

    return jsonify({"success": False, "message": "Incorrect password"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True}), 200


@app.route("/api/check-auth", methods=["GET"])
def check_auth():
    return jsonify({"authenticated": check_admin_auth()}), 200


# =========================
# Image Upload API (Base64 Data URL)
# =========================
@app.route("/api/upload-image", methods=["POST"])
def upload_image():
    if not check_admin_auth():
        return jsonify({"message": "Unauthorized"}), 401

    if "image" not in request.files:
        return jsonify({"message": "No image file provided"}), 400

    file = request.files["image"]

    if file.filename == "":
        return jsonify({"message": "No selected file"}), 400

    try:
        file_data = file.read()
        ext = file.filename.rsplit(".", 1)[1].lower() if "." in file.filename else "png"
        mime_type = f"image/{ext}"
        b64 = base64.b64encode(file_data).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        return jsonify(
            {
                "success": True,
                "imageUrl": data_url,
                "filename": secure_filename(file.filename),
            }
        ), 200
    except Exception as e:
        print("❌ Image Upload Error:", e)
        return jsonify({"message": f"Error processing image: {e}"}), 500


# =========================
# Scripts API
# =========================
@app.route("/api/scripts", methods=["GET", "POST"])
@app.route("/api/scripts/<string:script_id>", methods=["PUT", "DELETE"])
def scripts(script_id=None):
    # ---------- GET (Public) ----------
    if request.method == "GET":
        scripts = list(scripts_collection.find({}))
        for s in scripts:
            s["_id"] = str(s["_id"])
        return jsonify(scripts), 200

    # For POST/PUT/DELETE require admin
    if not check_admin_auth():
        return jsonify({"message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    # ---------- POST (Create) ----------
    if request.method == "POST":
        if not all(k in data for k in ("title", "image", "key")):
            return jsonify({"message": "Missing required fields"}), 400

        result = scripts_collection.insert_one(
            {
                "title": data["title"],
                "image": data["image"],
                "key": data["key"],
            }
        )

        new_script = {
            "_id": str(result.inserted_id),
            "title": data["title"],
            "image": data["image"],
            "key": data["key"],
        }

        send_telegram_notification(
            f"➕ *New Script Added:*\n`{new_script['title']}`"
        )

        return jsonify({"message": "Script added", "script": new_script}), 201

    # ---------- PUT (Update) ----------
    if request.method == "PUT":
        if not all(k in data for k in ("title", "image", "key")):
            return jsonify({"message": "Missing required fields"}), 400
        try:
            update_result = scripts_collection.update_one(
                {"_id": ObjectId(script_id)},
                {
                    "$set": {
                        "title": data["title"],
                        "image": data["image"],
                        "key": data["key"],
                    }
                },
            )
        except InvalidId:
            return jsonify({"message": "Invalid script ID format"}), 400

        if update_result.matched_count == 0:
            return jsonify({"message": "Script not found"}), 404

        send_telegram_notification(
            f"✏ *Script Updated:*\nID: `{script_id}`\nTitle: `{data['title']}`"
        )
        return jsonify({"message": "Script updated"}), 200

    # ---------- DELETE ----------
    if request.method == "DELETE":
        try:
            result = scripts_collection.delete_one({"_id": ObjectId(script_id)})
        except InvalidId:
            return jsonify({"message": "Invalid script ID format"}), 400

        if result.deleted_count == 0:
            return jsonify({"message": "Script not found"}), 404

        send_telegram_notification(
            f"🗑 *Script Deleted:*\nID: `{script_id}`"
        )
        return jsonify({"message": "Script deleted"}), 200

    return jsonify({"message": "Method not allowed"}), 405


# =========================
# Accounts / Profiles API
# =========================
@app.route("/api/accounts", methods=["GET", "POST"])
@app.route("/api/accounts/<string:account_id>", methods=["PUT", "DELETE"])
def accounts(account_id=None):
    # ---------- GET (Admin only) ----------
    if request.method == "GET":
        if not check_admin_auth():
            return jsonify({"message": "Unauthorized"}), 401

        accounts = list(accounts_collection.find({}))
        for acc in accounts:
            acc["_id"] = str(acc["_id"])
        return jsonify(accounts), 200

    # Other methods require auth
    if not check_admin_auth():
        return jsonify({"message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    # ---------- POST (Create profile) ----------
    if request.method == "POST":
        required = ("name", "image", "username", "password")
        if not all(k in data for k in required):
            return jsonify({"message": "Missing required fields"}), 400

        accent_color = data.get("accentColor", "#0ea5e9")
        doc = {
            "name": data["name"],
            "image": data["image"],
            "username": data["username"],
            "password": data["password"],
            "accentColor": accent_color,
        }

        result = accounts_collection.insert_one(doc)
        doc["_id"] = str(result.inserted_id)

        send_telegram_notification(
            f"👤 *New Profile Added:*\n{doc['name']} (@{doc['username']})"
        )

        return jsonify({"message": "Profile added", "account": doc}), 201

    # ---------- PUT (Update profile) ----------
    if request.method == "PUT":
        required = ("name", "image", "username", "password")
        if not all(k in data for k in required):
            return jsonify({"message": "Missing required fields"}), 400

        update_doc = {
            "name": data["name"],
            "image": data["image"],
            "username": data["username"],
            "password": data["password"],
            "accentColor": data.get("accentColor", "#0ea5e9"),
        }

        try:
            update_result = accounts_collection.update_one(
                {"_id": ObjectId(account_id)},
                {"$set": update_doc},
            )
        except InvalidId:
            return jsonify({"message": "Invalid account ID format"}), 400

        if update_result.matched_count == 0:
            return jsonify({"message": "Account not found"}), 404

        send_telegram_notification(
            f"📝 *Profile Updated:*\n{update_doc['name']} (@{update_doc['username']})"
        )

        return jsonify({"message": "Profile updated"}), 200

    # ---------- DELETE ----------
    if request.method == "DELETE":
        try:
            result = accounts_collection.delete_one({"_id": ObjectId(account_id)})
        except InvalidId:
            return jsonify({"message": "Invalid account ID format"}), 400

        if result.deleted_count == 0:
            return jsonify({"message": "Account not found"}), 404

        send_telegram_notification(
            f"🗑 *Profile Deleted:*\nID: `{account_id}`"
        )
        return jsonify({"message": "Profile deleted"}), 200

    return jsonify({"message": "Method not allowed"}), 405


# =========================
# Notify Script Copy
# =========================
@app.route("/api/notify/copy", methods=["POST"])
def notify_copy():
    data = request.get_json(silent=True) or {}

    title = data.get("title", "Unknown Script")
    key = data.get("key", "")
    time_str = data.get("time", "Unknown time")

    msg = f"""📋 *Script Copied!*

*Title:* `{title}`
*Time:* `{time_str}`

*Snippet:*
"""
    send_telegram_notification(msg)
    return jsonify({"success": True, "message": "Notification sent"}), 200


# =========================
# Run (for local dev)
# =========================
if __name__ == "__main__":
    # On Render, gunicorn app:app will be used, this is only for local testing.
    app.run(host="0.0.0.0", port=5000, debug=True)
