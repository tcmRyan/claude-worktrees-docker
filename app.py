from flask import Flask, jsonify
import os, redis as _redis, psycopg2

app = Flask(__name__)

_r = _redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"))
_db_url = os.environ.get("DATABASE_URL", "")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/count")
def count():
    n = _r.incr("visits") or 0
    return jsonify({"visits": n})


@app.route("/db")
def db_check():
    conn = psycopg2.connect(_db_url)
    cur = conn.cursor()
    cur.execute("SELECT version()")
    row = cur.fetchone()
    version = row[0] if row else "unknown"
    conn.close()
    return jsonify({"postgres": version})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
