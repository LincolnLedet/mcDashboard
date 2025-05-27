from flask import Flask, render_template, jsonify
from mcstatus import JavaServer
import docker
import datetime

app = Flask(__name__)
server = JavaServer("theycallme.link", 25565)
status = server.status()


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')

def status():
    server = JavaServer("theycallme.link", 25565)
    mcstatus = server.status()

    client = docker.from_env()
    container = client.containers.get('minecraft-server')
    stats = container.stats(stream=False)
    mem_usage = stats["memory_stats"]["usage"] / (1024 ** 2)
    started_at = container.attrs["State"]["StartedAt"]
    status = container.status
    # Calculate uptime
    started_time = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    uptime_seconds = int((datetime.datetime.now(datetime.timezone.utc) - started_time).total_seconds())


    return jsonify({
        "online": True,
        "motd": mcstatus.description,
        "players_online": mcstatus.players.online,
        "players_max": mcstatus.players.max,
        "player_names": mcstatus.players,
        "version_name": mcstatus.version.name,
        "version_protocol": mcstatus.version.protocol,
        "latency_ms": mcstatus.latency,
        "status": status,
        "ram_mb": round(mem_usage, 2),
        "uptime_seconds": uptime_seconds
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081, debug=True)
