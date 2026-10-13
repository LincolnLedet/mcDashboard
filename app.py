from flask import Flask, render_template, jsonify, request, Response, abort
from mcstatus import JavaServer
from rcon.source import Client
import base64
import datetime
import sqlite3
import re
import os
import threading
import time
import requests

import logsync

# All secrets and host-specific paths come from the environment (see .env.example).
RCON_HOST = os.environ.get("RCON_HOST", "minecraft")
RCON_PORT = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASS = os.environ["RCON_PASSWORD"]
TURNSTILE_SECRET = os.environ["TURNSTILE_SECRET"]
TURNSTILE_SITEKEY = os.environ["TURNSTILE_SITEKEY"]
MC_HOST = os.environ.get("MC_HOST", "minecraft")
MC_PORT = int(os.environ.get("MC_PORT", "25565"))
MC_ADDRESS = os.environ.get("MC_ADDRESS", "mc.heyim.link")
MAP_URL = os.environ.get("MAP_URL", "https://map.heyim.link")
LOG_DIR = os.environ.get("LOG_DIR", "/mc/logs")
# Written by the minecraft service on every start (seconds since epoch).
STARTED_AT_PATH = os.environ.get("STARTED_AT_PATH", "/mc/run/started_at")
# cgroup v2 memory accounting for the minecraft service, mounted read-only.
MEMORY_CURRENT_PATH = os.environ.get("MEMORY_CURRENT_PATH", "/mc/cgroup/memory.current")
RAM_MAX_MB = float(os.environ.get("RAM_MAX_MB", "10240"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "players.db"))

STATUS_CACHE_SECONDS = 10
LOG_SYNC_SECONDS = 15
WHITELIST_LIMIT = 5            # attempts per client...
WHITELIST_WINDOW_SECONDS = 600  # ...per 10 minutes


def rcon_command(command):
    try:
        with Client(RCON_HOST, RCON_PORT, passwd=RCON_PASS, timeout=5) as client:
            return client.run(command)
    except Exception as e:
        print(f"RCON error: {e}")
        return None


app = Flask(__name__)


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        logsync.init(conn)


def sync_logs():
    """Joins and chat come from the server logs, so nothing is missed or double counted
    no matter how often (or whether) anyone has the dashboard open."""
    try:
        with db() as conn:
            logsync.sync_dir(conn, LOG_DIR)
    except Exception as e:
        print(f"Log sync failed: {e}")


def log_sync_loop():
    while True:
        sync_logs()
        time.sleep(LOG_SYNC_SECONDS)


def get_recent_players(limit=20):
    with db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''
            SELECT name,
                   MIN(CASE WHEN kind = 'join' THEN at END) AS first_seen,
                   MAX(at) AS last_seen,
                   SUM(kind = 'join') AS join_count
            FROM events
            GROUP BY name
            HAVING join_count > 0
            ORDER BY last_seen DESC
            LIMIT ?
        ''', (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_recent_chats(limit=50):
    with db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''
            SELECT player_name, message, sent_at
            FROM chat_messages
            ORDER BY sent_at DESC, id DESC
            LIMIT ?
        ''', (limit,)).fetchall()
    today = datetime.date.today().isoformat()
    chats = []
    for row in rows:
        chat = dict(row)
        date, clock = chat["sent_at"].split(" ")
        # Today's messages show just the time; older ones also show the date.
        chat["timestamp"] = clock if date == today else f"{date[5:]} {clock[:5]}"
        chats.append(chat)
    return chats


def get_server_day():
    # "minecraft:" prefix: EssentialsX replaces the plain /time command.
    resp = rcon_command("minecraft:time query minecraft:day repetition")
    if resp:
        match = re.search(r'(\d+) repetition', resp)
        if match:
            return int(match.group(1))
    return None


def get_server_tps():
    resp = rcon_command("tps")
    if resp:
        # Response looks like "§6TPS from last 1m, 5m, 15m: §a20.0§r, §a20.0§r, §a20.0"
        clean = re.sub(r'§.', '', resp)
        if ':' in clean:
            match = re.search(r'(\d+\.?\d*)', clean.split(':')[1])
            if match:
                return float(match.group(1))
    return None


def get_server_time():
    # 26.3 replaced "daytime" with timelines: "Timeline minecraft:day is at N tick(s)"
    resp = rcon_command("minecraft:time query minecraft:day")
    if resp:
        match = re.search(r'(\d+) tick', resp)
        if match:
            ticks = int(match.group(1)) % 24000
            # Convert ticks to hours/minutes (0 ticks = 6:00 AM)
            total_minutes = ((ticks / 1000) * 60 + 360) % 1440
            hours = int(total_minutes // 60)
            minutes = int(total_minutes % 60)
            period = "AM" if hours < 12 else "PM"
            display_hours = hours % 12 or 12
            return f"{display_hours}:{minutes:02d} {period}"
    return None


def read_number(path):
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


init_db()
threading.Thread(target=log_sync_loop, daemon=True).start()

_status_lock = threading.Lock()
_status_cache = {"at": 0.0, "data": None, "icon": None}


def build_status():
    try:
        mcstatus = JavaServer(MC_HOST, MC_PORT).status()
    except Exception as e:
        print(f"Status ping failed: {e}")
        return {"online": False, "player_history": get_recent_players(10),
                "recent_chats": get_recent_chats(20), "current_players": []}

    current_players = [p.name for p in (mcstatus.players.sample or [])]
    _status_cache["icon"] = mcstatus.icon

    mem_bytes = read_number(MEMORY_CURRENT_PATH)
    started_at = read_number(STARTED_AT_PATH)
    uptime_seconds = int(time.time() - started_at) if started_at else None

    return {
        "online": True,
        "motd": mcstatus.motd.to_plain(),
        "address": MC_ADDRESS,
        "current_players": current_players,
        "player_history": get_recent_players(10),
        "recent_chats": get_recent_chats(20),
        "players_online": mcstatus.players.online,
        "players_max": mcstatus.players.max,
        "version_name": mcstatus.version.name,
        "version_protocol": mcstatus.version.protocol,
        "tps": get_server_tps(),
        "ram_mb": round(mem_bytes / (1024 ** 2), 2) if mem_bytes is not None else None,
        "ram_max_mb": RAM_MAX_MB,
        "uptime_seconds": uptime_seconds,
        "server_day": get_server_day(),
        "server_time": get_server_time(),
    }


def cached_status():
    # The page polls every 10s per visitor; only hit RCON and the log once per window.
    with _status_lock:
        if _status_cache["data"] is None or time.time() - _status_cache["at"] > STATUS_CACHE_SECONDS:
            _status_cache["data"] = build_status()
            _status_cache["at"] = time.time()
        return _status_cache["data"]


_attempts_lock = threading.Lock()
_attempts = {}


def client_ip():
    # Behind Cloudflare Tunnel every request comes from cloudflared; the real client is in this header.
    return request.headers.get("CF-Connecting-IP") or request.remote_addr


def rate_limited(key):
    now = time.time()
    with _attempts_lock:
        recent = [t for t in _attempts.get(key, []) if now - t < WHITELIST_WINDOW_SECONDS]
        limited = len(recent) >= WHITELIST_LIMIT
        if not limited:
            recent.append(now)
        _attempts[key] = recent
        return limited


@app.route('/')
def index():
    return render_template('index.html', map_url=MAP_URL, mc_address=MC_ADDRESS)


@app.route('/server-icon.png')
def server_icon():
    cached_status()
    icon = _status_cache["icon"]
    if not icon or not icon.startswith("data:image/png;base64,"):
        abort(404)
    return Response(base64.b64decode(icon.split(",", 1)[1]), mimetype='image/png',
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route('/api/status')
def status():
    return jsonify(cached_status())


@app.route('/api/players')
def all_players():
    return jsonify(get_recent_players(100))


@app.route('/api/chats')
def all_chats():
    return jsonify(get_recent_chats(100))


@app.route('/whitelist')
def whitelist_page():
    return render_template('whitelist.html', turnstile_sitekey=TURNSTILE_SITEKEY, mc_address=MC_ADDRESS)


@app.route('/api/whitelist', methods=['POST'])
def whitelist_player():
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    captcha_token = str(data.get('captcha_token', ''))

    if rate_limited(client_ip()):
        return jsonify({"success": False, "error": "Too many attempts. Please wait a few minutes and try again."}), 429

    # Validate username (Minecraft usernames: 3-16 chars, alphanumeric + underscore)
    if not re.fullmatch(r'[a-zA-Z0-9_]{3,16}', username):
        return jsonify({"success": False, "error": "Invalid username. Must be 3-16 characters, letters/numbers/underscores only."})

    try:
        verify_resp = requests.post('https://challenges.cloudflare.com/turnstile/v0/siteverify', data={
            'secret': TURNSTILE_SECRET,
            'response': captcha_token,
            'remoteip': client_ip(),
        }, timeout=10)
        if not verify_resp.json().get('success'):
            return jsonify({"success": False, "error": "Verification failed. Please try again."})
    except Exception as e:
        print(f"Turnstile error: {e}")
        return jsonify({"success": False, "error": "Verification error."})

    resp = rcon_command(f"whitelist add {username}")
    if resp is None:
        return jsonify({"success": False, "error": "Failed to connect to server. Try again later."})
    clean = re.sub(r'§.', '', resp)
    if "Added" in clean or "already whitelisted" in clean.lower():
        return jsonify({"success": True, "message": f"{username} has been whitelisted! Join at {MC_ADDRESS}"})
    if "does not exist" in clean.lower():
        return jsonify({"success": False, "error": "That Minecraft account doesn't exist. Check the spelling."})
    print(f"Unexpected whitelist response for {username}: {clean}")
    return jsonify({"success": False, "error": "The server couldn't add that name. Please try again later."})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8081)
