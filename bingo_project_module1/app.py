from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template

from src.main import fetch_latest_draws, init_db, load_recent, module1_analyze, save_draws

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
STATE = {
    "last_update": None,
    "last_added": 0,
    "last_error": None,
}


def update_once() -> dict:
    init_db()
    draws = fetch_latest_draws()
    added = save_draws(draws)
    recent = load_recent(120)
    analysis = module1_analyze(recent)
    STATE["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    STATE["last_added"] = added
    STATE["last_error"] = None
    return {"added": added, "analysis": analysis, "last_update": STATE["last_update"]}


def auto_loop() -> None:
    # 啟動後先抓一次，之後每 5 分鐘更新一次。
    while True:
        try:
            update_once()
        except Exception as exc:
            STATE["last_error"] = str(exc)
        time.sleep(300)


@app.route("/")
def index():
    init_db()
    recent = load_recent(120)
    analysis = module1_analyze(recent)
    return render_template("index.html", analysis=analysis, state=STATE)


@app.route("/api/analysis")
def api_analysis():
    init_db()
    recent = load_recent(120)
    return jsonify({"analysis": module1_analyze(recent), "state": STATE})


@app.route("/api/update", methods=["POST", "GET"])
def api_update():
    try:
        return jsonify(update_once())
    except Exception as exc:
        STATE["last_error"] = str(exc)
        return jsonify({"error": str(exc), "state": STATE}), 500


if __name__ == "__main__":
    t = threading.Thread(target=auto_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
