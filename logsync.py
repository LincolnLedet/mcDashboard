"""Build join and chat history from the Minecraft server logs.

Logs are the only reliable record: they capture every join and message whether or not
anyone has the dashboard open. Rotated logs (YYYY-MM-DD-N.log.gz) are immutable, so each
is read once and remembered; latest.log is re-read every sync and de-duplicated by the
unique keys below.
"""
import datetime
import gzip
import os
import re
import sqlite3
import sys

LINE = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\] \[([^\]]+)\]: (.*)$')
# "Linkabink[/10.89.0.2:35700] logged in with entity id ..." is vanilla's own login line,
# unaffected by join-message plugins.
JOIN = re.compile(r'^(\w{2,16})\[[^\]]*\] logged in with entity id')
LEAVE = re.compile(r'^(\w{2,16}) lost connection:')
# Optional "[Not Secure]" and rank tags such as "[Admin]" come before <Name>.
CHAT = re.compile(r'^(?:\[[^\]]*\] )*<(\w{2,16})> (.*)$')
EMOTE = re.compile(r'^\* (\w{2,16}) (.*)$')
# Game messages are logged as "System chat: <text>". Joins, leaves, deaths and advancements show up in the
# website chat next to normal messages; the stored text is what follows the player's name.
JOIN_MSG = re.compile(r'^(\w{2,16}) (joined the game)$')
LEAVE_MSG = re.compile(r'^(\w{2,16}) (left the game)$')
ADVANCEMENT = re.compile(r'^(\w{2,16}) (has (?:made the advancement|completed the challenge|reached the goal) \[.*\])$')
DEATH = re.compile(r"^(\w{2,16}) ((?:was |fell |drowned|burned|tried |suffocated|starved|hit the ground|walked into|experienced|blew up|went |withered|froze|died|discovered|didn't want).*)$")
# Announcements from the console or RCON (/say), e.g. restart warnings: "[Not Secure] [Rcon] Restarting in 10 seconds".
SERVER_SAY = re.compile(r'^(?:\[Not Secure\] )?\[(?:Rcon|Server)\] (.+)$')
EVENT_KINDS = (('join', JOIN_MSG), ('leave', LEAVE_MSG), ('advancement', ADVANCEMENT), ('death', DEATH))
ROTATED = re.compile(r'^(\d{4}-\d{2}-\d{2})-\d+\.log\.gz$')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS events (
    name TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'join' or 'leave'
    at TEXT NOT NULL,            -- 'YYYY-MM-DD HH:MM:SS', server local time
    UNIQUE(name, kind, at)
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name TEXT NOT NULL,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'chat',   -- chat, server, join, leave, death, advancement
    UNIQUE(player_name, message, sent_at)
);
CREATE INDEX IF NOT EXISTS chat_messages_sent_at ON chat_messages(sent_at);
CREATE TABLE IF NOT EXISTS processed_logs (file TEXT PRIMARY KEY);
'''


def init(conn):
    conn.executescript(SCHEMA)
    # databases made before activity lines existed have no "kind" column yet
    if 'kind' not in [r[1] for r in conn.execute('PRAGMA table_info(chat_messages)')]:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
    conn.commit()


def read_lines(path):
    opener = gzip.open if path.endswith('.gz') else open
    try:
        with opener(path, 'rt', encoding='utf-8', errors='replace') as f:
            for line in f:
                yield line.rstrip('\n')
    except (EOFError, OSError) as e:  # truncated archives still yield what was readable
        print(f"logsync: stopped early reading {path}: {e}")


def read_new_lines(path):
    """Lines appended to a growing log since the last call. Only whole lines are consumed; a new file
    (rotation) or a shorter one starts over from the beginning."""
    st = os.stat(path)
    inode, offset = _tail.get(path, (None, 0))
    if inode != st.st_ino or st.st_size < offset:
        offset = 0
    with open(path, 'rb') as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b'\n') + 1
    _tail[path] = (st.st_ino, offset + end)
    return data[:end].decode('utf-8', errors='replace').splitlines()


# path -> (inode, bytes already read) for latest.log, so each sync only reads what is new
_tail = {}


def parse(lines, date, joins=True):
    """Yield ('event', name, kind, at) and ('chat', name, message, at, kind) records from log lines."""
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        clock, thread, body = m.groups()
        at = f"{date} {clock}"
        if thread.startswith('Async Chat Thread'):
            c = CHAT.match(body) or EMOTE.match(body)
            if c:
                name, message = c.groups()
                if c.re is EMOTE:
                    message = f"* {message}"
                yield ('chat', name, message, at, 'chat')
        elif thread == 'Server thread/INFO' and (say := SERVER_SAY.match(body)):
            yield ('chat', 'Server', say.group(1), at, 'server')
        elif thread == 'Server thread/INFO' and body.startswith('System chat: '):
            text = body[len('System chat: '):]
            if 'kicked' not in text:
                for kind, rx in EVENT_KINDS:
                    m = rx.match(text)
                    if m:
                        yield ('chat', m.group(1), m.group(2), at, kind)
                        break
        elif joins and thread == 'Server thread/INFO':
            j = JOIN.match(body)
            if j:
                yield ('event', j.group(1), 'join', at)
                continue
            l = LEAVE.match(body)
            if l:
                yield ('event', l.group(1), 'leave', at)


def ingest(conn, lines, date, joins=True):
    events, chats = [], []
    for record in parse(lines, date, joins):
        (events if record[0] == 'event' else chats).append(record[1:])
    conn.executemany('INSERT OR IGNORE INTO events (name, kind, at) VALUES (?, ?, ?)', events)
    # An advancement is shown once per player (some are re-granted over and over in the logs).
    seen, kept = set(), []
    for name, message, at, kind in chats:
        if kind == 'advancement':
            key = (name, message)
            if key in seen or conn.execute("SELECT 1 FROM chat_messages WHERE kind = 'advancement' AND player_name = ? AND message = ? AND sent_at < ? LIMIT 1", (name, message, at)).fetchone():
                continue
            seen.add(key)
        kept.append((name, message, at, kind))
    conn.executemany('INSERT OR IGNORE INTO chat_messages (player_name, message, sent_at, kind) VALUES (?, ?, ?, ?)', kept)
    return len(events), len(kept)


def sync_dir(conn, log_dir, joins=True):
    """Ingest new rotated logs once each, then whatever was added to latest.log since last time."""
    for file in sorted(os.listdir(log_dir)):
        m = ROTATED.match(file)
        if not m:
            continue
        key = os.path.join(os.path.abspath(log_dir), file)
        if conn.execute('SELECT 1 FROM processed_logs WHERE file = ?', (key,)).fetchone():
            continue
        ingest(conn, read_lines(os.path.join(log_dir, file)), m.group(1), joins)
        conn.execute('INSERT INTO processed_logs (file) VALUES (?)', (key,))
    latest = os.path.join(log_dir, 'latest.log')
    if os.path.exists(latest):
        # latest.log rolls over at the first write after midnight, so all of it is from the day it was last written.
        date = datetime.date.fromtimestamp(os.path.getmtime(latest)).isoformat()
        ingest(conn, read_new_lines(latest), date, joins)
    conn.commit()


if __name__ == '__main__':
    # Usage: python3 logsync.py DB_PATH LOG_DIR [--chats-only]
    db_path, log_dir = sys.argv[1], sys.argv[2]
    conn = sqlite3.connect(db_path)
    init(conn)
    sync_dir(conn, log_dir, joins='--chats-only' not in sys.argv)
    print(conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0], 'chats,',
          conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'join'").fetchone()[0], 'joins')
