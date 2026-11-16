from flask import Flask, render_template, jsonify, request, Response, abort, send_file
from mcstatus import JavaServer
from rcon.source import Client
import base64
import datetime
import json
import sqlite3
import re
import os
import threading
import time
import zlib
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
# BlueMap's own web server (inside the minecraft container); its live player data is what the public map shows.
BLUEMAP_URL = os.environ.get("BLUEMAP_URL", "http://minecraft:8100")
BLUEMAP_MAPS = os.environ.get("BLUEMAP_MAPS", "world,world_the_nether,world_the_end").split(",")
LOG_DIR = os.environ.get("LOG_DIR", "/mc/logs")
# Written by the minecraft service on every start (seconds since epoch).
STARTED_AT_PATH = os.environ.get("STARTED_AT_PATH", "/mc/run/started_at")
# cgroup v2 memory accounting for the minecraft service, mounted read-only.
MEMORY_CURRENT_PATH = os.environ.get("MEMORY_CURRENT_PATH", "/mc/cgroup/memory.current")
CPU_STAT_PATH = os.environ.get("CPU_STAT_PATH", "/mc/cgroup/cpu.stat")
RAM_MAX_MB = float(os.environ.get("RAM_MAX_MB", "10240"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "players.db"))
# {"title": ..., "text": ..., "until": "2026-10-27T20:30:00-04:00"}; also announced in game by broadcast.py
ANNOUNCEMENT_PATH = os.path.join(os.path.dirname(DB_PATH), "announcement.json")

STATUS_CACHE_SECONDS = 10
LOG_SYNC_SECONDS = 5   # only new lines are read, so this is cheap
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


def get_recent_chats(limit=50, before=None):
    """Newest first. `before` is a message id: only messages older than it (for scrolling back)."""
    with db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''
            SELECT id, player_name, message, sent_at, kind
            FROM chat_messages
            WHERE ? IS NULL OR (sent_at, id) < (SELECT sent_at, id FROM chat_messages WHERE id = ?)
            ORDER BY sent_at DESC, id DESC
            LIMIT ?
        ''', (before, before, limit)).fetchall()
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


def get_view_distances():
    # Overworld line of "/vdt status" (ViewDistanceTweaks): "world | Sim: 16 | View: 32 | ..."
    resp = rcon_command("vdt status")
    if resp:
        match = re.search(r'^world \| Sim: (\d+) \| View: (\d+)', re.sub(r'§.', '', resp), re.M)
        if match:
            return int(match.group(2)), int(match.group(1))
    return None, None


def get_day_ticks():
    # 26.3 replaced "daytime" with timelines: "Timeline minecraft:day is at N tick(s)"
    resp = rcon_command("minecraft:time query minecraft:day")
    if resp:
        match = re.search(r'(\d+) tick', resp)
        if match:
            return int(match.group(1)) % 24000
    return None


def format_server_time(ticks):
    if ticks is None:
        return None
    # Convert ticks to hours/minutes (0 ticks = 6:00 AM)
    total_minutes = ((ticks / 1000) * 60 + 360) % 1440
    hours = int(total_minutes // 60)
    minutes = int(total_minutes % 60)
    period = "AM" if hours < 12 else "PM"
    display_hours = hours % 12 or 12
    return f"{display_hours}:{minutes:02d} {period}"


def get_player_locations():
    """Where each online player is, from BlueMap's live data: {name: {map, x, y, z}}.
    Every map lists every player; the one where `foreign` is false is the world they are in."""
    found = {}
    for map_id in BLUEMAP_MAPS:
        try:
            r = requests.get(f"{BLUEMAP_URL}/maps/{map_id}/live/players.json", timeout=3)
            r.raise_for_status()
            for p in r.json().get("players", []):
                pos = p.get("position") or {}
                if not p.get("foreign") and p.get("name") and all(k in pos for k in ("x", "y", "z")):
                    found[p["name"]] = {"map": map_id, "x": round(pos["x"]), "y": round(pos["y"]), "z": round(pos["z"])}
        except Exception as e:
            print(f"BlueMap player lookup failed for {map_id}: {e}")
    return found


# ---- System numbers, measured like the terminal dashboard (mcmon): RAM used = total - available,
# CPU from /proc/stat, the server's CPU from its cgroup, heap from EssentialsX's /gc.
# The container sees the host's /proc, so these are whole-machine figures.
_sys = {"cpu_pct": None, "server_cpu_pct": None}


def read_meminfo():
    """(total_bytes, used_bytes) where used = total - MemAvailable, like psutil."""
    try:
        with open("/proc/meminfo") as f:
            values = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in f if line.split()[0].rstrip(":") in ("MemTotal", "MemAvailable")}
        return values["MemTotal"], values["MemTotal"] - values["MemAvailable"]
    except (OSError, KeyError, ValueError, IndexError):
        return None, None


def _read_cpu():
    """(busy+idle jiffies, idle jiffies, number of cpus) from /proc/stat."""
    with open("/proc/stat") as f:
        lines = f.read().splitlines()
    v = [int(x) for x in lines[0].split()[1:9]]
    ncpu = sum(1 for l in lines if l.startswith("cpu") and l[3:4].isdigit())
    return sum(v), v[3] + v[4], max(ncpu, 1)


def _read_cgroup_cpu_usec():
    try:
        with open(CPU_STAT_PATH) as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def system_sampler_loop():
    """Every 2 s: whole-machine CPU % and the server's share of it (of all cores), smoothed over ~10 s."""
    recent_cpu, recent_srv = [], []
    prev = None
    while True:
        try:
            total, idle, ncpu = _read_cpu()
            usage = _read_cgroup_cpu_usec()
            now = time.time()
            if prev:
                dt, di = total - prev[0], idle - prev[1]
                if dt > 0:
                    recent_cpu.append((1 - di / dt) * 100)
                if usage is not None and prev[3] is not None and now > prev[2]:
                    recent_srv.append((usage - prev[3]) / 1e6 / ((now - prev[2]) * ncpu) * 100)
                del recent_cpu[:-5], recent_srv[:-5]
                _sys["cpu_pct"] = sum(recent_cpu) / len(recent_cpu) if recent_cpu else None
                _sys["server_cpu_pct"] = sum(recent_srv) / len(recent_srv) if recent_srv else None
            prev = (total, idle, now, usage)
        except Exception as e:
            print(f"System sampler error: {e}")
        time.sleep(2)


def read_loadavg():
    try:
        with open("/proc/loadavg") as f:
            return [float(x) for x in f.read().split()[:3]]
    except (OSError, ValueError):
        return None


def get_heap_mb():
    """(used, max) of the server's Java heap in MB, from EssentialsX's /gc."""
    resp = rcon_command("gc")
    if not resp:
        return None, None
    clean = re.sub(r'§.', '', resp)

    def grab(label):
        m = re.search(label + r':\s*([\d,]+)\s*MB', clean)
        return int(m.group(1).replace(',', '')) if m else None
    mx, free, alloc = grab("Maximum memory"), grab("Free memory"), grab("Allocated memory")
    if None in (mx, free, alloc):
        return None, None
    return max(alloc - free, 0), mx


def read_number(path):
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


init_db()
threading.Thread(target=log_sync_loop, daemon=True).start()
threading.Thread(target=system_sampler_loop, daemon=True).start()

_status_lock = threading.Lock()
_status_cache = {"at": 0.0, "data": None, "icon": None}


def build_status():
    try:
        mcstatus = JavaServer(MC_HOST, MC_PORT).status()
    except Exception as e:
        print(f"Status ping failed: {e}")
        return {"online": False, "player_history": get_recent_players(10),
                "recent_chats": get_recent_chats(40), "current_players": []}

    current_players = [p.name for p in (mcstatus.players.sample or [])]
    _status_cache["icon"] = mcstatus.icon

    mem_bytes = read_number(MEMORY_CURRENT_PATH)
    started_at = read_number(STARTED_AT_PATH)
    uptime_seconds = int(time.time() - started_at) if started_at else None

    day_ticks = get_day_ticks()
    sys_total, sys_used = read_meminfo()
    heap_used, heap_max = get_heap_mb()
    view_distance, sim_distance = get_view_distances()
    return {
        "online": True,
        "motd": mcstatus.motd.to_plain(),
        "address": MC_ADDRESS,
        "current_players": current_players,
        "player_history": get_recent_players(10),
        "recent_chats": get_recent_chats(40),
        "players_online": mcstatus.players.online,
        "players_max": mcstatus.players.max,
        "version_name": mcstatus.version.name,
        "version_protocol": mcstatus.version.protocol,
        "tps": get_server_tps(),
        "view_distance": view_distance,
        "sim_distance": sim_distance,
        "ram_mb": round(mem_bytes / (1024 ** 2), 2) if mem_bytes is not None else None,
        "ram_max_mb": RAM_MAX_MB,
        "uptime_seconds": uptime_seconds,
        "server_day": get_server_day(),
        "server_time": format_server_time(day_ticks),
        # 0 = 6:00 AM; the page turns this into a sun/moon that keeps moving between refreshes
        "server_ticks": day_ticks,
        "player_locations": get_player_locations(),
        # whole-machine RAM (total - available) and CPU, the server's CPU share and Java heap: same as mcmon
        "sys_ram_used_mb": round(sys_used / 1024 ** 2) if sys_used else None,
        "sys_ram_total_mb": round(sys_total / 1024 ** 2) if sys_total else None,
        "heap_used_mb": heap_used,
        "heap_max_mb": heap_max,
        "cpu_pct": round(_sys["cpu_pct"], 1) if _sys["cpu_pct"] is not None else None,
        "server_cpu_pct": round(_sys["server_cpu_pct"], 1) if _sys["server_cpu_pct"] is not None else None,
        "load": read_loadavg(),
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


def current_announcement():
    """The banner from ANNOUNCEMENT_PATH, or None when there is none or its time has passed."""
    try:
        with open(ANNOUNCEMENT_PATH, encoding="utf-8") as f:
            a = json.load(f)
        until = datetime.datetime.fromisoformat(a["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if datetime.datetime.now(datetime.timezone.utc) >= until:
        return None
    return {"title": a.get("title", ""), "text": a["text"], "until_ms": int(until.timestamp() * 1000)}


@app.route('/')
def index():
    return render_template('index.html', map_url=MAP_URL, mc_address=MC_ADDRESS, announcement=current_announcement())


# ---- Realms map (beta): files written by territory.py on a timer (mc-realms.timer) into ~/minecraft/run/realms.
REALMS_DIR = os.environ.get("REALMS_DIR", "/mc/run/realms")
REALMS_FILES = {"data.json": "realms.json"}


@app.route('/realms')
def realms_page():
    return render_template('realms.html', map_url=MAP_URL)


@app.route('/realms/<name>')
def realms_file(name):
    if name not in REALMS_FILES:
        abort(404)
    path = os.path.join(REALMS_DIR, REALMS_FILES[name])
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, max_age=300, conditional=True)
    return resp


@app.context_processor
def favicon_version():
    """Pages link the favicon as /server-icon.png?v=<checksum of the current icon>, so browsers pick up the
    seasonal icon as soon as the server's icon changes instead of keeping an old cached one."""
    icon = _status_cache.get("icon")
    if not icon:
        cached_status()
        icon = _status_cache.get("icon") or ""
    return {"icon_v": format(zlib.crc32(icon.encode()), "x") if icon else "0"}


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


@app.route('/api/chat')
def chat_history():
    """The chat box's feed: the newest page, or with ?before=<id> the page before that message."""
    before = request.args.get('before', type=int)
    limit = min(max(request.args.get('limit', 100, type=int), 1), 200)
    chats = get_recent_chats(limit, before)
    with db() as conn:
        total = conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0]
    return jsonify({"chats": chats, "total": total, "more": len(chats) == limit})


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
