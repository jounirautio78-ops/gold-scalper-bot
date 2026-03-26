from fastapi import FastAPI, Request
import sqlite3
import json
from datetime import datetime
from zoneinfo import ZoneInfo

app = FastAPI()

TZ = ZoneInfo("Europe/Madrid")
DB_PATH = "scalper.db"


def now_iso():
    return datetime.now(TZ).isoformat()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS scalp_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        timeframe TEXT,
        entry_reason TEXT,
        sl_distance REAL,
        tp_distance REAL,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
    )
    """)

    conn.commit()
    conn.close()


@app.on_event("startup")
def startup_event():
    init_db()


@app.get("/")
def root():
    return {"status": "gold_scalper_api_running"}


@app.get("/health")
def health():
    return {"status": "ok", "time": now_iso()}


@app.post("/scalper")
async def scalper_webhook(request: Request):
    try:
        data = await request.json()
        print("INCOMING SCALP:", data)

        if data.get("message_type") != "scalp_signal":
            return {"status": "ignored_not_scalp"}

        direction = data.get("direction")
        symbol = data.get("symbol")

        if direction not in ["buy", "sell"]:
            return {"status": "invalid_direction"}

        if not symbol:
            return {"status": "missing_symbol"}

        received_at = now_iso()
        timeframe = data.get("timeframe")
        entry_reason = data.get("entry_reason")
        sl_distance = data.get("sl_distance")
        tp_distance = data.get("tp_distance")
        payload_str = json.dumps(data)

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scalp_signals (
                received_at, symbol, direction, timeframe,
                entry_reason, sl_distance, tp_distance,
                payload_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            received_at,
            symbol,
            direction,
            timeframe,
            entry_reason,
            sl_distance,
            tp_distance,
            payload_str
        ))
        signal_id = cur.lastrowid
        conn.commit()
        conn.close()

        print("SCALP SIGNAL QUEUED:", signal_id)

        return {"status": "scalp_signal_queued", "signal_id": signal_id}

    except Exception as e:
        print("SCALPER ERROR:", e)
        return {"status": "error", "message": str(e)}


@app.get("/next_signal")
def next_signal():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM scalp_signals
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT 1
    """)
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"status": "empty"}

    return {
        "status": "ok",
        "signal": dict(row)
    }


@app.post("/ack_signal/{signal_id}")
def ack_signal(signal_id: int):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE scalp_signals
        SET status = 'processed'
        WHERE id = ? AND status = 'pending'
    """, (signal_id,))

    conn.commit()
    updated = cur.rowcount
    conn.close()

    if updated == 0:
        return {"status": "not_found_or_already_processed"}

    return {"status": "processed", "signal_id": signal_id}


@app.get("/signals_report")
def signals_report():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS cnt FROM scalp_signals")
    total = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) AS cnt FROM scalp_signals WHERE status = 'pending'")
    pending = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) AS cnt FROM scalp_signals WHERE status = 'processed'")
    processed = cur.fetchone()["cnt"]

    conn.close()

    return {
        "total_signals": total,
        "pending_signals": pending,
        "processed_signals": processed
    }
