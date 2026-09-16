import os
import sys
import json
import time
import uuid
import shutil
import zipfile
import threading
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename
import psutil

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB Upload Limit

# সমস্ত ক্রস-অরিজিন ডোমেইন থেকে রিকোয়েস্ট অ্যালাউ করা হলো
CORS(app, resources={r"/api/*": {"origins": "*"}})

SERVERS_FILE = 'servers.json'
BOTS_DIR = os.path.abspath('bots')
CPU_HISTORY = {}
RUNNING_PROCESSES = {}

os.makedirs(BOTS_DIR, exist_ok=True)

# ============================================
# ডাটাবেস ও পাথ সিকিউরিটি (Database & Security)
# ============================================

def load_servers():
    if not os.path.exists(SERVERS_FILE):
        save_servers({})
        return {}
    with open(SERVERS_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_servers(data):
    with open(SERVERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_server_dir(server_id):
    server_dir = os.path.abspath(os.path.join(BOTS_DIR, server_id))
    os.makedirs(server_dir, exist_ok=True)
    return server_dir

def safe_path(server_dir, user_path):
    """পাথ ট্রাভার্সাল (Path Traversal) ঠেকানোর জন্য স্যান্ডবক্সিং ভ্যালিডেশন"""
    if not user_path:
        return server_dir
    user_path = user_path.lstrip("/\\")
    full_path = os.path.abspath(os.path.join(server_dir, user_path))
    if os.path.commonpath([server_dir, full_path]) != server_dir:
        raise ValueError("অননুমোদিত পাথ অ্যাক্সেস প্রতিহত করা হয়েছে!")
    return full_path

def create_default_files(server_dir):
    main_py = os.path.join(server_dir, 'main.py')
    if not os.path.exists(main_py):
        with open(main_py, 'w', encoding='utf-8') as f:
            f.write('''# JUBAYER HOSTING - Bot
import time

print("\\033[92m" + "=" * 40)
print("  Bot is running on JUBAYER HOSTING")
print("  Termux / Cloud Console Ready!")
print("=" * 40 + "\\033[0m")

counter = 0
while True:
    counter += 1
    print(f"\\033[94m[{time.strftime('%H:%M:%S')}]\\033[0m \\033[93mHeartbeat #{counter}\\033[0m | \\033[92mActive\\033[0m")
    time.sleep(10)
''')
    
    req_file = os.path.join(server_dir, 'requirements.txt')
    if not os.path.exists(req_file):
        with open(req_file, 'w', encoding='utf-8') as f:
            f.write('# Add your pip packages here\n')

def get_or_create_default_server():
    servers = load_servers()
    if 'default' not in servers:
        server_dir = get_server_dir('default')
        create_default_files(server_dir)
        servers['default'] = {
            'server_id': 'default',
            'type': 'python',
            'ram': '1GB',
            'disk': '1GB',
            'status': 'stopped',
            'pid': None,
            'created': str(datetime.now()),
            'main_file': 'main.py',
            'requirements_file': 'requirements.txt',
            'cpu_limit': 80,
            'rate_limit_exceeded': False,
            'stopped_by_user': False
        }
        save_servers(servers)
    return servers['default']

# ব্যাকএন্ড চালু হওয়ার সময় কোনো অবাস্তব 'running' স্ট্যাটাস থাকলে তা রিসেট করা
def sync_initial_state():
    servers = load_servers()
    changed = False
    for sid, srv in servers.items():
        if srv.get('status') == 'running':
            srv['status'] = 'stopped'
            srv['pid'] = None
            changed = True
    if changed:
        save_servers(servers)

sync_initial_state()

# ============================================
# রেট লিমিট ও প্রসেস মনিটর
# ============================================

class RateLimiter:
    def check_rate(self, server_id, limit_percent):
        if server_id not in CPU_HISTORY:
            CPU_HISTORY[server_id] = []
        servers = load_servers()
        server = servers.get(server_id)
        if not server or server.get('status') != 'running':
            return False, 0
        pid = server.get('pid')
        if not pid: 
            return False, 0
        try:
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=0.2)
            now = time.time()
            CPU_HISTORY[server_id].append({'time': now, 'cpu': cpu})
            CPU_HISTORY[server_id] = [h for h in CPU_HISTORY[server_id] if now - h['time'] < 30]
            recent = [h['cpu'] for h in CPU_HISTORY[server_id] if now - h['time'] < 10]
            if recent:
                avg_cpu = sum(recent) / len(recent)
                if avg_cpu > limit_percent:
                    return True, avg_cpu
        except Exception: 
            pass
        return False, 0

rate_limiter = RateLimiter()

def stop_bot_process(pid):
    """চাইল্ড প্রসেসসহ সাব-প্রসেস সঠিকভাবে কিল করা"""
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
        return True
    except Exception:
        try:
            if sys.platform == 'win32':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
            else:
                os.kill(pid, 9)
            return True
        except Exception:
            return False

def run_bot(server_id, main_file='main.py', requirements_file='requirements.txt'):
    server_dir = get_server_dir(server_id)
    main_path = os.path.join(server_dir, main_file)
    log_file = os.path.join(server_dir, 'output.log')
    python_exe = sys.executable
    
    def log(msg):
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"{msg}\n")
                f.flush()
        except Exception:
            pass
    
    if not os.path.exists(main_path):
        return None, f"ERROR: {main_file} not found!"
    
    # নতুন রান হলে পূর্বের লগ খালি করা
    try: 
        open(log_file, 'w', encoding='utf-8').close()
    except Exception: 
        pass
    
    ts = lambda: datetime.now().strftime('%I:%M:%S %p')
    servers = load_servers()
    server = servers.get(server_id, {})
    cpu_limit = server.get('cpu_limit', 80)
    
    log(f"\033[90m[{ts()}] Starting server process...\033[0m")
    
    # Requirements ইন্সটল করা (যদি প্যাকেজ থাকে)
    if requirements_file and requirements_file.strip():
        req_path = os.path.join(server_dir, requirements_file.strip())
        if os.path.exists(req_path):
            with open(req_path, 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f.read().split('\n') if l.strip() and not l.strip().startswith('#')]
            if lines:
                log(f"\033[93m[{ts()}] Installing requirements...\033[0m")
                try:
                    proc = subprocess.Popen(
                        [python_exe, '-m', 'pip', 'install', '-r', os.path.abspath(req_path), '--disable-pip-version-check'],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    for line in iter(proc.stdout.readline, ''):
                        if line.strip(): 
                            log(line.rstrip())
                    proc.wait()
                except Exception as e:
                    log(f"\033[91m[{ts()}] pip error: {str(e)}\033[0m")
    
    try:
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        env['TERM'] = 'xterm-256color'
        
        proc = subprocess.Popen(
            [python_exe, '-u', os.path.abspath(main_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=server_dir,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        
        RUNNING_PROCESSES[server_id] = proc
        
        # CPU ওভারইউজ মনিটরিং থ্রেড
        def rate_monitor():
            while proc.poll() is None:
                time.sleep(5)
                exceeded, avg_cpu = rate_limiter.check_rate(server_id, cpu_limit)
                if exceeded:
                    log(f"\n\033[91m[{ts()}] CPU Limit exceeded ({avg_cpu:.1f}% > {cpu_limit}%), stopping...\033[0m")
                    stop_bot_process(proc.pid)
                    srvs = load_servers()
                    if server_id in srvs:
                        srvs[server_id]['status'] = 'stopped'
                        srvs[server_id]['pid'] = None
                        srvs[server_id]['rate_limit_exceeded'] = True
                        save_servers(srvs)
                    RUNNING_PROCESSES.pop(server_id, None)
                    break
        
        threading.Thread(target=rate_monitor, daemon=True).start()
        
        # লাইভ টার্মিনাল লগ পাইপিং
        def stream_output():
            try:
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    
                    if any(c in line for c in ['\x1b[2J', '\x1b[H', '[H[2J', '\033[2J', '\x1b[3J']):
                        with open(log_file, 'w', encoding='utf-8') as f:
                            f.write("")
                    
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(line)
                        f.flush()
            except Exception: 
                pass
            finally:
                proc.wait()
                srvs = load_servers()
                if server_id in srvs and srvs[server_id].get('status') == 'running':
                    srvs[server_id]['status'] = 'stopped'
                    srvs[server_id]['pid'] = None
                    save_servers(srvs)
                RUNNING_PROCESSES.pop(server_id, None)
        
        threading.Thread(target=stream_output, daemon=True).start()
        return proc.pid, None
    except Exception as e:
        log(f"\033[91m[{ts()}] Error: {str(e)}\033[0m")
        return None, str(e)

def get_process_stats(pid):
    try:
        proc = psutil.Process(pid)
        cpu = proc.cpu_percent(interval=0.1)
        mem = proc.memory_info()
        ram = mem.rss / (1024 * 1024)
        return {
            'cpu_percent': round(cpu, 1),
            'ram_display': f"{ram:.1f} MB" if ram < 1024 else f"{ram/1024:.1f} GB",
        }
    except Exception:
        return {'cpu_percent': 0, 'ram_display': '0 MB'}

# ============================================
# পেজ রাউটস (Page Routes)
# ============================================

@app.route('/')
def home_redirect():
    default_srv = get_or_create_default_server()
    return redirect(url_for('server_panel', server_id=default_srv['server_id']))

@app.route('/<server_id>')
def server_panel(server_id):
    servers = load_servers()
    if server_id not in servers:
        server_dir = get_server_dir(server_id)
        create_default_files(server_dir)
        servers[server_id] = {
            'server_id': server_id,
            'type': 'python',
            'ram': '1GB',
            'disk': '1GB',
            'status': 'stopped',
            'pid': None,
            'created': str(datetime.now()),
            'main_file': 'main.py',
            'requirements_file': 'requirements.txt',
            'cpu_limit': 80,
            'rate_limit_exceeded': False,
            'stopped_by_user': False
        }
        save_servers(servers)
    
    # templates/home.html না থাকলেও API ক্র্যাশ করবে না
    try:
        return render_template('home.html', current_server=servers[server_id])
    except Exception:
        return jsonify({
            "service": "JUBAYER HOSTING API",
            "server_id": server_id,
            "status": servers[server_id].get('status')
        })

# ============================================
# বট কন্ট্রোল API (Process Control)
# ============================================

@app.route('/api/start/<server_id>', methods=['POST'])
@app.route('/api/run/<server_id>', methods=['POST'])
def api_start_server(server_id):
    servers = load_servers()
    server = servers.get(server_id)
    if not server: 
        return jsonify({'status': 'error', 'message': 'Server not found'}), 404
    
    # অলরেডি প্রসেস চালু থাকলে চেক করা
    if server.get('pid') and psutil.pid_exists(server['pid']):
        return jsonify({'status': 'error', 'message': 'Already running!'})
    
    server['rate_limit_exceeded'] = False
    server['stopped_by_user'] = False
    
    pid, error = run_bot(server_id, server.get('main_file', 'main.py'), server.get('requirements_file', 'requirements.txt'))
    
    if pid:
        server['status'] = 'running'
        server['pid'] = pid
        server['started_at'] = str(datetime.now())
        save_servers(servers)
        return jsonify({'status': 'success', 'message': 'Started!'})
    return jsonify({'status': 'error', 'message': error or 'Failed'}), 500

@app.route('/api/stop/<server_id>', methods=['POST'])
def api_stop(server_id):
    servers = load_servers()
    server = servers.get(server_id)
    if not server: 
        return jsonify({'status': 'error', 'message': 'Server not found'}), 404
    
    if server.get('pid'):
        stop_bot_process(server['pid'])
    
    RUNNING_PROCESSES.pop(server_id, None)
    
    server['status'] = 'stopped'
    server['pid'] = None
    server['stopped_by_user'] = True
    save_servers(servers)
    
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n\033[91m[{datetime.now().strftime('%I:%M:%S %p')}] Server stopped by user\033[0m\n")
    except Exception: 
        pass
    
    return jsonify({'status': 'success', 'message': 'Stopped'})

@app.route('/api/restart/<server_id>', methods=['POST'])
def api_restart(server_id):
    api_stop(server_id)
    time.sleep(1)
    return api_start_server(server_id)

@app.route('/api/logs/<server_id>')
def api_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            logs = f.read()
    else: 
        logs = "~ $ Ready...\n"
    return jsonify({'logs': logs, 'output': logs})

@app.route('/api/clear_logs/<server_id>', methods=['POST'])
def api_clear_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        if os.path.exists(log_file):
            open(log_file, 'w', encoding='utf-8').close()
        return jsonify({'status': 'success', 'message': 'Cleared'})
    except Exception: 
        return jsonify({'status': 'error', 'message': 'Failed to clear logs'}), 500

@app.route('/api/command', methods=['POST'])
def api_command():
    data = request.get_json(silent=True) or {}
    cmd = data.get('cmd', '')
    server_id = data.get('server_id', 'default')
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    
    if server_id in RUNNING_PROCESSES:
        proc = RUNNING_PROCESSES[server_id]
        if proc.poll() is None:
            try:
                proc.stdin.write(cmd + "\n")
                proc.stdin.flush()
                return jsonify({'status': 'success', 'output': f'Sent input: {cmd}\n'})
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=get_server_dir(server_id), timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        output = (result.stdout + result.stderr)[:4000]
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n\033[93m$ {cmd}\033[0m\n{output}\n")
        return jsonify({'status': 'success', 'output': output})
    except Exception as e: 
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/stats/<server_id>')
def api_stats(server_id):
    servers = load_servers()
    server = servers.get(server_id)
    if not server:
        return jsonify({'cpu': '0%', 'ram': '0 MB', 'status': 'stopped'})
    
    cpu, ram = "0%", "0 MB"
    if server.get('status') == 'running' and server.get('pid'):
        if psutil.pid_exists(server['pid']):
            stats = get_process_stats(server['pid'])
            cpu = f"{stats['cpu_percent']}%"
            ram = stats['ram_display']
        else:
            server['status'] = 'stopped'
            server['pid'] = None
            save_servers(servers)
    
    return jsonify({'cpu': cpu, 'ram': ram, 'status': server.get('status', 'stopped')})

# ============================================
# ফাইল ম্যানেজার API (Secure File Operations)
# ============================================

def format_file_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.1f} KB"
    else:
        return f"{size_bytes/(1024*1024):.1f} MB"

@app.route('/api/files/<server_id>')
def api_files(server_id):
    folder = request.args.get('path', '') or request.args.get('folder', '')
    server_dir = get_server_dir(server_id)
    try:
        target_dir = safe_path(server_dir, folder)
    except ValueError as e:
        return jsonify({'error': str(e)}), 403

    if not os.path.exists(target_dir): 
        return jsonify({'files': []})
    
    files = []
    try:
        for item in sorted(os.listdir(target_dir)):
            item_path = os.path.join(target_dir, item)
            is_dir = os.path.isdir(item_path)
            size = os.path.getsize(item_path) if not is_dir else 0
            files.append({
                'name': item,
                'is_dir': is_dir,
                'size': size,
                'size_formatted': format_file_size(size) if not is_dir else '-',
                'modified': datetime.fromtimestamp(os.path.getmtime(item_path)).strftime('%Y-%m-%d %H:%M')
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'files': files})

@app.route('/api/file/<server_id>', methods=['GET'])
def api_get_file(server_id):
    file_rel_path = request.args.get('path', '') or request.args.get('filename', '')
    server_dir = get_server_dir(server_id)
    try:
        filepath = safe_path(server_dir, file_rel_path)
    except ValueError as e:
        return jsonify({'error': str(e)}), 403

    if os.path.exists(filepath) and os.path.isfile(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f: 
                return jsonify({'content': f.read()})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'error': 'File not found'}), 404

@app.route('/api/file/<server_id>', methods=['POST'])
def api_save_file(server_id):
    data = request.get_json(silent=True) or {}
    file_rel_path = data.get('path', '') or data.get('filename', '')
    server_dir = get_server_dir(server_id)
    try:
        filepath = safe_path(server_dir, file_rel_path)
    except ValueError as e:
        return jsonify({'error': str(e)}), 403

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f: 
        f.write(data.get('content', ''))
    return jsonify({'success': True, 'message': 'Saved successfully'})

@app.route('/api/file/<server_id>', methods=['DELETE'])
def api_delete_file(server_id):
    data = request.get_json(silent=True) or {}
    file_rel_path = data.get('path', '') or data.get('filename', '')
    server_dir = get_server_dir(server_id)
    try:
        filepath = safe_path(server_dir, file_rel_path)
    except ValueError as e:
        return jsonify({'error': str(e)}), 403

    if os.path.exists(filepath):
        if os.path.isdir(filepath): 
            shutil.rmtree(filepath)
        else: 
            os.remove(filepath)
        return jsonify({'success': True, 'message': 'Deleted successfully'})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/upload/<server_id>', methods=['POST'])
def api_upload(server_id):
    if 'file' not in request.files: 
        return jsonify({'error': 'No file uploaded'}), 400
    folder = request.form.get('path', '') or request.form.get('folder', '')
    server_dir = get_server_dir(server_id)
    try:
        target_dir = safe_path(server_dir, folder)
    except ValueError as e:
        return jsonify({'error': str(e)}), 403

    os.makedirs(target_dir, exist_ok=True)
    uploaded_files = request.files.getlist('file')
    for file in uploaded_files:
        if file.filename:
            fname = secure_filename(file.filename)
            if fname:
                file.save(os.path.join(target_dir, fname))
    return jsonify({'success': True, 'message': 'Uploaded successfully'})

@app.route('/api/create_folder/<server_id>', methods=['POST'])
def api_create_folder(server_id):
    data = request.get_json(silent=True) or {}
    folder_rel = data.get('path', '') or data.get('folder_name', '') or data.get('foldername', '')
    server_dir = get_server_dir(server_id)
    try:
        target = safe_path(server_dir, folder_rel)
    except ValueError as e:
        return jsonify({'error': str(e)}), 403

    os.makedirs(target, exist_ok=True)
    return jsonify({'success': True, 'message': 'Folder created'})

@app.route('/api/rename/<server_id>', methods=['POST'])
def api_rename(server_id):
    d = request.get_json(silent=True) or {}
    server_dir = get_server_dir(server_id)
    old_rel = d.get('old_path', '') or d.get('old_name', '')
    new_rel = d.get('new_path', '') or d.get('new_name', '')
    try:
        old_path = safe_path(server_dir, old_rel)
        new_path = safe_path(server_dir, new_rel)
    except ValueError as e:
        return jsonify({'error': str(e)}), 403
    
    if os.path.exists(old_path):
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.rename(old_path, new_path)
        return jsonify({'success': True, 'message': 'Renamed successfully'})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/extract/<server_id>', methods=['POST'])
@app.route('/api/unzip/<server_id>', methods=['POST'])
def api_extract(server_id):
    data = request.get_json(silent=True) or {}
    file_rel = data.get('file_path', '') or data.get('path', '') or data.get('filename', '')
    target_rel = data.get('target_path', '') or data.get('target', '')
    server_dir = get_server_dir(server_id)
    
    try:
        zip_path = safe_path(server_dir, file_rel)
        dest_path = safe_path(server_dir, target_rel) if target_rel else os.path.dirname(zip_path)
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 403
    
    if os.path.exists(zip_path) and zip_path.endswith('.zip'):
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # Zip Slip প্রিভেনশন
                for member in zf.namelist():
                    extracted_path = os.path.abspath(os.path.join(dest_path, member))
                    if os.path.commonpath([dest_path, extracted_path]) != dest_path:
                        raise ValueError("ক্ষতিকর ফাইল সনাক্ত হয়েছে (Zip Slip Attack)!")
                zf.extractall(dest_path)
            return jsonify({'status': 'success', 'message': 'Extracted successfully'})
        except Exception as e: 
            return jsonify({'status': 'error', 'message': str(e)}), 500
    return jsonify({'status': 'error', 'message': 'Invalid ZIP file'}), 400

@app.route('/api/get_startup/<server_id>')
def api_get_startup(server_id):
    servers = load_servers()
    server = servers.get(server_id, {})
    return jsonify({
        'main_file': server.get('main_file', 'main.py'), 
        'req_file': server.get('requirements_file', 'requirements.txt'),
        'requirements_file': server.get('requirements_file', 'requirements.txt')
    })

@app.route('/api/set_startup/<server_id>', methods=['POST'])
def api_set_startup(server_id):
    d = request.get_json(silent=True) or {}
    servers = load_servers()
    if server_id in servers:
        servers[server_id]['main_file'] = d.get('main_file', 'main.py')
        servers[server_id]['requirements_file'] = d.get('req_file') or d.get('requirements_file', 'requirements.txt')
        save_servers(servers)
        return jsonify({'success': True, 'message': 'Startup config saved'})
    return jsonify({'error': 'Not found'}), 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
