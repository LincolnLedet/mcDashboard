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
    UNIQUE(player_name, message, sent_at)
);
CREATE INDEX IF NOT EXISTS chat_messages_sent_at ON chat_messages(sent_at);
CREATE TABLE IF NOT EXISTS processed_logs (file TEXT PRIMARY KEY);
'''


def init(conn):
    conn.executescript(SCHEMA)


def read_lines(path):
    opener = gzip.open if path.endswith('.gz') else open
    try:
        with opener(path, 'rt', encoding='utf-8', errors='replace') as f:
            for line in f:
                yield line.rstrip('\n')
    except (EOFError, OSError) as e:  # truncated archives still yield what was readable
        print(f"logsync: stopped early reading {path}: {e}")


def parse(path, date, joins=True):
    """Yield ('event', name, kind, at) and ('chat', name, message, at) records from one log file."""
    for line in read_lines(path):
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
                yield ('chat', name, message, at)
        elif joins and thread == 'Server thread/INFO':
            j = JOIN.match(body)
            if j:
                yield ('event', j.group(1), 'join', at)
                continue
            l = LEAVE.match(body)
            if l:
                yield ('event', l.group(1), 'leave', at)


def ingest(conn, path, date, joins=True):
    events, chats = [], []
    for record in parse(path, date, joins):
        (events if record[0] == 'event' else chats).append(record[1:])
    conn.executemany('INSERT OR IGNORE INTO events (name, kind, at) VALUES (?, ?, ?)', events)
    conn.executemany('INSERT OR IGNORE INTO chat_messages (player_name, message, sent_at) VALUES (?, ?, ?)', chats)
    return len(events), len(chats)


def sync_dir(conn, log_dir, joins=True):
    """Ingest new rotated logs once each, then re-read latest.log."""
    for file in sorted(os.listdir(log_dir)):
        m = ROTATED.match(file)
        if not m:
            continue
        key = os.path.join(os.path.abspath(log_dir), file)
        if conn.execute('SELECT 1 FROM processed_logs WHERE file = ?', (key,)).fetchone():
            continue
        ingest(conn, os.path.join(log_dir, file), m.group(1), joins)
        conn.execute('INSERT INTO processed_logs (file) VALUES (?)', (key,))
    latest = os.path.join(log_dir, 'latest.log')
    if os.path.exists(latest):
        # latest.log rolls over at the first write after midnight, so all of it is from the day it was last written.
        date = datetime.date.fromtimestamp(os.path.getmtime(latest)).isoformat()
        ingest(conn, latest, date, joins)
    conn.commit()


if __name__ == '__main__':
    # Usage: python3 logsync.py DB_PATH LOG_DIR [--chats-only]
    db_path, log_dir = sys.argv[1], sys.argv[2]
    conn = sqlite3.connect(db_path)
    init(conn)
    sync_dir(conn, log_dir, joins='--chats-only' not in sys.argv)
    print(conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0], 'chats,',
          conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'join'").fetchone()[0], 'joins')
