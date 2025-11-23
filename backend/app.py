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

# --- Load Environment Variables ---
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this")

# --- Flask App Setup with Correct Path ---
frontend_path = pathlib.Path(__file__).parent.parent / 'frontend'
app = Flask(__name__, static_folder=str(frontend_path))
app.secret_key = SECRET_KEY
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Configure CORS properly
CORS(app, supports_credentials=True, origins=[
    "https://nhoyscript.vercel.app",
    "https://nhoyscript.onrender.com",   # ✅ ត្រឹមត្រូវ
    "http://127.0.0.1:5000",
    "http://localhost:5000"
])


# --- MongoDB Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client['nhoy_hub'] 
    scripts_collection = db['scripts']
    accounts_collection = db['accounts']
    print("Successfully connected to MongoDB.")
    
    # --- Data Seeding (Initial setup) ---
    default_scripts = json.loads(open('default_scripts.json').read()) if os.path.exists('default_scripts.json') else []
    if scripts_collection.count_documents({}) == 0 and default_scripts:
        scripts_collection.insert_many(default_scripts)
        print(f"Inserted {len(default_scripts)} default scripts.")

    default_accounts = json.loads(open('default_accounts.json').read()) if os.path.exists('default_accounts.json') else []
    if accounts_collection.count_documents({}) == 0 and default_accounts:
        accounts_collection.insert_many(default_accounts)
        print(f"Inserted {len(default_accounts)} default accounts.")

except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    
# ----------------------------------------------------------------------
## --- Utility Functions ---
# ----------------------------------------------------------------------

def send_telegram_notification(message):
    """Sends a formatted message to the Telegram chat ID."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram config missing. Skipping notification.")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending Telegram message: {e}")

def check_admin_auth():
    """Check if user is authenticated as admin"""
    return session.get('is_admin') == True

# ----------------------------------------------------------------------
## --- Frontend & Static Routes ---
# ----------------------------------------------------------------------

@app.route('/')
def serve_index():
    """Serves the main public page (index.html)."""
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/admin')
def serve_admin():
    """Serves the admin dashboard page (admin.html)."""
    return send_from_directory(app.static_folder, 'admin.html')
    
# ----------------------------------------------------------------------
## --- API Routes ---
# ----------------------------------------------------------------------

@app.route('/api/login', methods=['POST'])
def admin_login():
    """Handles admin login by checking the password against the .env variable."""
    data = request.json
    password = data.get('password')
    
    if password == ADMIN_PASSWORD:
        session['is_admin'] = True
        session.permanent = True
        send_telegram_notification(f"🔐 *Admin Login Success!* (IP: {request.remote_addr})")
        return jsonify({"success": True}), 200
    else:
        return jsonify({"success": False, "message": "Incorrect password"}), 401

@app.route('/api/logout', methods=['POST'])
def admin_logout():
    """Handles admin logout"""
    session.pop('is_admin', None)
    return jsonify({"success": True}), 200

@app.route('/api/check-auth', methods=['GET'])
def check_auth():
    """Check if user is authenticated"""
    return jsonify({"authenticated": check_admin_auth()}), 200

@app.route('/api/upload-image', methods=['POST'])
def upload_image():
    """Handle image upload and return base64 data URL"""
    if not check_admin_auth():
        return jsonify({"message": "Unauthorized"}), 401
    
    if 'image' not in request.files:
        return jsonify({"message": "No image file provided"}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({"message": "No selected file"}), 400
    
    # Read file and convert to base64
    try:
        file_data = file.read()
        file_extension = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
        mime_type = f"image/{file_extension}"
        base64_data = base64.b64encode(file_data).decode('utf-8')
        data_url = f"data:{mime_type};base64,{base64_data}"
        
        return jsonify({
            "success": True, 
            "imageUrl": data_url,
            "filename": secure_filename(file.filename)
        }), 200
    except Exception as e:
        print(f"Error processing image: {e}")
        return jsonify({"message": f"Error processing image: {str(e)}"}), 500

@app.route('/api/scripts', methods=['GET', 'POST'])
@app.route('/api/scripts/<string:script_id>', methods=['DELETE', 'PUT'])
def manage_scripts(script_id=None):
    """Handles fetching, adding, deleting, and updating scripts."""
    
    if request.method == 'GET':
        # READ: Fetch all scripts, converting ObjectId to string for the frontend.
        scripts = list(scripts_collection.find({}))
        for script in scripts:
            script['_id'] = str(script['_id'])
        return jsonify(scripts)
        
    # Check authentication for write operations
    if not check_admin_auth():
        return jsonify({"message": "Unauthorized"}), 401
        
    if request.method == 'POST':
        # CREATE: Add a new script.
        data = request.json
        required_fields = ['title', 'image', 'key']
        if not all(field in data for field in required_fields):
            return jsonify({"message": "Missing required fields"}), 400

        result = scripts_collection.insert_one(data)
        
        inserted_script = data.copy()
        inserted_script['_id'] = str(result.inserted_id)

        send_telegram_notification(f"➕ *New Script Added:* {inserted_script['title']}")
        return jsonify({"message": "Script added successfully", "script": inserted_script}), 201

    elif request.method == 'PUT' and script_id:
        # UPDATE: Modify an existing script by its ID.
        data = request.json
        required_fields = ['title', 'image', 'key']
        if not all(field in data for field in required_fields):
            return jsonify({"message": "Missing required fields for update"}), 400

        try:
            update_result = scripts_collection.update_one(
                {"_id": ObjectId(script_id)},
                {"$set": {"title": data['title'], "image": data['image'], "key": data['key']}}
            )
        except InvalidId:
            return jsonify({"message": "Invalid script ID format"}), 400
        
        if update_result.matched_count == 0:
            return jsonify({"message": "Script not found"}), 404

        send_telegram_notification(f"📝 *Script Updated:* ID {script_id} - {data['title']}")
        return jsonify({"message": "Script updated successfully"}), 200
        
    elif request.method == 'DELETE' and script_id:
        # DELETE: Remove a script by its ID.
        try:
            result = scripts_collection.delete_one({"_id": ObjectId(script_id)})
        except InvalidId:
            return jsonify({"message": "Invalid script ID format"}), 400
            
        if result.deleted_count == 1:
            send_telegram_notification(f"🗑️ *Script Deleted:* ID {script_id}")
            return jsonify({"message": "Script deleted successfully"}), 200
        else:
            return jsonify({"message": "Script not found"}), 404
            
    return jsonify({"message": "Method not allowed for this endpoint"}), 405

@app.route('/api/accounts', methods=['GET', 'POST'])
@app.route('/api/accounts/<string:account_id>', methods=['DELETE', 'PUT'])
def manage_accounts(account_id=None):
    """Handles fetching, adding, deleting, and updating profile accounts."""
    
    if request.method == 'GET':
        # READ: Fetch all accounts
        if not check_admin_auth():
            return jsonify({"message": "Unauthorized"}), 401
        
        accounts = list(accounts_collection.find({}))
        for account in accounts:
            account['_id'] = str(account['_id'])
        return jsonify(accounts)
    
    # Check authentication for write operations
    if not check_admin_auth():
        return jsonify({"message": "Unauthorized"}), 401
    
    if request.method == 'POST':
        # CREATE: Add a new profile account
        data = request.json
        required_fields = ['name', 'image', 'username', 'password']
        if not all(field in data for field in required_fields):
            return jsonify({"message": "Missing required fields"}), 400
        
        # Add default accent color if not provided
        if 'accentColor' not in data:
            data['accentColor'] = '#0ea5e9'
        
        result = accounts_collection.insert_one(data)
        
        inserted_account = data.copy()
        inserted_account['_id'] = str(result.inserted_id)
        
        send_telegram_notification(f"👤 *New Profile Added:* {inserted_account['name']} (@{inserted_account['username']})")
        return jsonify({"message": "Profile added successfully", "account": inserted_account}), 201
    
    elif request.method == 'PUT' and account_id:
        # UPDATE: Modify an existing account by its ID
        data = request.json
        required_fields = ['name', 'image', 'username', 'password']
        if not all(field in data for field in required_fields):
            return jsonify({"message": "Missing required fields for update"}), 400
        
        try:
            update_data = {
                "name": data['name'],
                "image": data['image'],
                "username": data['username'],
                "password": data['password'],
                # FIX: Ensure accentColor is always set, using default if not provided in the update
                "accentColor": data.get('accentColor', '#0ea5e9') 
            }
            
            update_result = accounts_collection.update_one(
                {"_id": ObjectId(account_id)},
                {"$set": update_data}
            )
        except InvalidId:
            return jsonify({"message": "Invalid account ID format"}), 400
        
        if update_result.matched_count == 0:
            return jsonify({"message": "Account not found"}), 404
        
        send_telegram_notification(f"📝 *Profile Updated:* {data['name']} (@{data['username']})")
        return jsonify({"message": "Profile updated successfully"}), 200
    
    elif request.method == 'DELETE' and account_id:
        # DELETE: Remove an account by its ID
        try:
            result = accounts_collection.delete_one({"_id": ObjectId(account_id)})
        except InvalidId:
            return jsonify({"message": "Invalid account ID format"}), 400
        
        if result.deleted_count == 1:
            send_telegram_notification(f"🗑️ *Profile Deleted:* ID {account_id}")
            return jsonify({"message": "Profile deleted successfully"}), 200
        else:
            return jsonify({"message": "Account not found"}), 404
    
    return jsonify({"message": "Method not allowed for this endpoint"}), 405

@app.route('/api/notify/copy', methods=['POST'])
def notify_copy():
    """Endpoint to securely send script copy notifications via Telegram."""
    data = request.json
    script_title = data.get('title', 'Unknown Script')
    script_key = data.get('key', 'N/A')
    
    # Escape special characters for Telegram markdown
    message = f"🔔 *Script Copied!* 🔔\n\n*Title:* {script_title}\n*Time:* {data.get('time', 'N/A')}\n\n*Copied Script:*\n```\n{script_key[:100]}...\n```"
    send_telegram_notification(message)
    return jsonify({"success": True, "message": "Notification sent"}), 200

# ----------------------------------------------------------------------
## --- Server Run ---
# ----------------------------------------------------------------------

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')