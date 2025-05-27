from flask import Flask, render_template, jsonify

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def status():
    return jsonify({
        "online": True,
        "motd": "Welcome to the Minecraft Server!",
        "players_online": 3,
        "players_max": 20,
        "latency_ms": 42
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081, debug=True)
