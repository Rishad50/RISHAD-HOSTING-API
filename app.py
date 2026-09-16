import os
import sys
import json
import zipfile
import subprocess
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__)
# ফ্রন্টএন্ড যাতে যেকোনো ডোমেইন থেকে রিকোয়েস্ট পাঠাতে পারে সেজন্য CORS সক্রিয় করা হলো
CORS(app, resources={r"/api/*": {"origins": "*"}})

# সার্ভারগুলোর ফাইল স্টোর করার মূল ফোল্ডার
DATA_DIR = os.path.abspath("./server_data")
os.makedirs(DATA_DIR, exist_ok=True)

# রানিং প্রসেস এবং লগ ট্র্যাক করার ডিকশনারি
running_processes = {}
server_logs = {}
server_configs = {}

def get_server_dir(server_id):
    """সার্ভারের জন্য রুট ডিরেক্টরি বের করে এবং তৈরি করে"""
    s_dir = os.path.abspath(os.path.join(DATA_DIR, server_id))
    os.makedirs(s_dir, exist_ok=True)
    return s_dir

def safe_path(server_dir, user_path):
    """Path Traversal আক্রমণ ঠেকানোর জন্য নিরাপদ ফাইল পাথ ভ্যালিডেশন"""
    if not user_path:
        return server_dir
    # ক্লিনিং পাথ
    user_path = user_path.lstrip("/\\")
    full_path = os.path.abspath(os.path.join(server_dir, user_path))
    # পাথ যেন সার্ভার ডিরেক্টরির বাইরে না যায়
    if os.path.commonpath([server_dir, full_path]) != server_dir:
        raise ValueError("অননুমোদিত পাথ অ্যাক্সেস!")
    return full_path

def get_config_file(server_id):
    """সার্ভারের কনফিগ ফাইলের পাথ"""
    return os.path.join(get_server_dir(server_id), ".startup_config.json")

def load_server_config(server_id):
    cfg_file = get_config_file(server_id)
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"main_file": "main.py", "req_file": "requirements.txt"}

def save_server_config(server_id, config_data):
    cfg_file = get_config_file(server_id)
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=4)

def append_log(server_id, text):
    """লগ মেমোরিতে সংরক্ষণ করে (সর্বোচ্চ শেষ ৫০,০০০ ক্যারেক্টার)"""
    if server_id not in server_logs:
        server_logs[server_id] = ""
    server_logs[server_id] += text
    if len(server_logs[server_id]) > 50000:
        server_logs[server_id] = server_logs[server_id][-50000:]

def stream_logs(server_id, process):
    """ব্যাকগ্রাউন্ডে সাব-প্রসেসের আউটপুট রিড করে লগে যুক্ত করে"""
    for line in iter(process.stdout.readline, ''):
        append_log(server_id, line)
    process.stdout.close()
    process.wait()
    append_log(server_id, f"\n[Process exited with code {process.returncode}]\n")


# ==============================================================================
# ১. সার্ভার কন্ট্রোল API (START, STOP, RESTART, LOGS)
# ==============================================================================

@app.route("/api/start/<server_id>", methods=["POST"])
def start_server(server_id):
    s_dir = get_server_dir(server_id)
    cfg = load_server_config(server_id)
    main_file = cfg.get("main_file", "main.py")
    entry_path = os.path.join(s_dir, main_file)

    # অলরেডি রানিং কিনা যাচাই
    proc = running_processes.get(server_id)
    if proc and proc.poll() is None:
        return jsonify({"message": "সার্ভার ইতিমধ্যে রানিং অবস্থায় আছে।"}), 200

    if not os.path.exists(entry_path):
        # যদি মেইন ফাইল না থাকে তবে একটি বেসিক ফাইল বানিয়ে দেওয়া
        with open(entry_path, "w", encoding="utf-8") as f:
            f.write("import time\nprint('Server started successfully!')\nwhile True:\n    time.sleep(5)\n    print('Server ping...')\n")

    # লগ পরিষ্কার ও শুরু মেসেজ
    append_log(server_id, f"\n\x1b[32m[Starting Python application: {main_file}]...\x1b[0m\n")

    try:
        # পাইথন ইন্টারপ্রেটার দিয়ে স্ক্রিপ্ট রান করানো
        proc = subprocess.Popen(
            [sys.executable, "-u", main_file],
            cwd=s_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        running_processes[server_id] = proc

        # আলাদা থ্রেডে লগ রিড করা
        log_thread = threading.Thread(target=stream_logs, args=(server_id, proc), daemon=True)
        log_thread.start()

        return jsonify({"message": f"{main_file} সফলভাবে চালু হয়েছে।"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stop/<server_id>", methods=["POST"])
def stop_server(server_id):
    proc = running_processes.get(server_id)
    if proc and proc.poll() is None:
        proc.terminate()
        append_log(server_id, "\n\x1b[31m[Server manually stopped]\x1b[0m\n")
        return jsonify({"message": "সার্ভার বন্ধ করা হয়েছে।"}), 200
    return jsonify({"message": "সার্ভার আগেই বন্ধ ছিল।"}), 200

@app.route("/api/restart/<server_id>", methods=["POST"])
def restart_server(server_id):
    stop_server(server_id)
    return start_server(server_id)

@app.route("/api/logs/<server_id>", methods=["GET"])
def get_logs(server_id):
    logs = server_logs.get(server_id, "~ $ Ready...\n")
    return jsonify({"logs": logs})


# ==============================================================================
# ২. ফাইল ম্যানেজার API (LIST, READ, WRITE, DELETE, FOLDER, RENAME, ZIP)
# ==============================================================================

@app.route("/api/files/<server_id>", methods=["GET"])
def list_files(server_id):
    s_dir = get_server_dir(server_id)
    rel_path = request.args.get("path", "")
    try:
        target_dir = safe_path(s_dir, rel_path)
        if not os.path.exists(target_dir):
            return jsonify({"error": "ডিরেক্টরি পাওয়া যায়নি।"}), 404

        files_list = []
        for item in os.listdir(target_dir):
            # ইন্টারনাল কনফিগ ফাইল হাইড রাখা
            if item == ".startup_config.json":
                continue
            item_path = os.path.join(target_dir, item)
            files_list.append({
                "name": item,
                "is_dir": os.path.isdir(item_path)
            })

        # ফোল্ডারগুলো আগে এবং নাম অনুযায়ী সর্ট করা
        files_list.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return jsonify({"files": files_list})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/file/<server_id>", methods=["GET"])
def get_file_content(server_id):
    s_dir = get_server_dir(server_id)
    rel_path = request.args.get("path", "")
    try:
        target_file = safe_path(s_dir, rel_path)
        if not os.path.isfile(target_file):
            return jsonify({"error": "ফাইলটি পাওয়া যায়নি।"}), 404
        with open(target_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/file/<server_id>", methods=["POST"])
def save_file_content(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    content = data.get("content", "")

    if not rel_path:
        return jsonify({"error": "ফাইলের পাথ উল্লেখ করা হয়নি।"}), 400

    try:
        target_file = safe_path(s_dir, rel_path)
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"message": "ফাইল সফলভাবে সেভ হয়েছে।"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/file/<server_id>", methods=["DELETE"])
def delete_file_or_folder(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    try:
        target = safe_path(s_dir, rel_path)
        if os.path.isdir(target):
            import shutil
            shutil.rmtree(target)
        elif os.path.isfile(target):
            os.remove(target)
        return jsonify({"message": "সফলভাবে মুছে ফেলা হয়েছে।"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/create_folder/<server_id>", methods=["POST"])
def create_folder(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    try:
        target_dir = safe_path(s_dir, rel_path)
        os.makedirs(target_dir, exist_ok=True)
        return jsonify({"message": "ফোল্ডার তৈরি হয়েছে।"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/rename/<server_id>", methods=["POST"])
def rename_item(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(force=True)
    old_p = data.get("old_path", "")
    new_p = data.get("new_path", "")
    try:
        old_full = safe_path(s_dir, old_p)
        new_full = safe_path(s_dir, new_p)
        os.rename(old_full, new_full)
        return jsonify({"message": "রিনেম সফল হয়েছে।"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/extract/<server_id>", methods=["POST"])
def extract_zip(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(force=True)
    zip_path = data.get("file_path", "")
    target_path = data.get("target_path", "")
    try:
        full_zip = safe_path(s_dir, zip_path)
        full_target = safe_path(s_dir, target_path)

        if not zipfile.is_zipfile(full_zip):
            return jsonify({"error": "এটি ভ্যালিড ZIP ফাইল নয়।"}), 400

        with zipfile.ZipFile(full_zip, 'r') as zip_ref:
            zip_ref.extractall(full_target)
        return jsonify({"message": "ZIP সফলভাবে আনজিপ করা হয়েছে।"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/upload/<server_id>", methods=["POST"])
def upload_file(server_id):
    s_dir = get_server_dir(server_id)
    dest_path = request.form.get("path", "")
    try:
        target_dir = safe_path(s_dir, dest_path)
        os.makedirs(target_dir, exist_ok=True)

        files = request.files.getlist("file")
        if not files:
            return jsonify({"error": "কোনো ফাইল পাওয়া যায়নি।"}), 400

        for file in files:
            if file and file.filename:
                fname = secure_filename(file.filename)
                file.save(os.path.join(target_dir, fname))

        return jsonify({"message": "ফাইল(গুলো) আপলোড সফল হয়েছে।"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ==============================================================================
# ৩. স্টার্টআপ কনফিগারেশন API
# ==============================================================================

@app.route("/api/get_startup/<server_id>", methods=["GET"])
def get_startup(server_id):
    return jsonify(load_server_config(server_id))

@app.route("/api/set_startup/<server_id>", methods=["POST"])
def set_startup(server_id):
    data = request.get_json(force=True)
    config_data = {
        "main_file": data.get("main_file", "main.py"),
        "req_file": data.get("req_file", "requirements.txt")
    }
    save_server_config(server_id, config_data)
    return jsonify({"message": "স্টার্টআপ কনফিগারেশন সফলভাবে সেভ হয়েছে।"})


# ==============================================================================
# মূল রুট (Health Check)
# ==============================================================================
@app.route("/")
def index():
    return jsonify({"status": "running", "service": "Jubayer Hosting API Engine"}), 200


if __name__ == "__main__":
    # Render বা যেকোনো সার্ভারে পোর্ট নির্ধারণ
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
