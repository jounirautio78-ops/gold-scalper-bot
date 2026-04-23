import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Scalper Railway Backend v2")

DB_PATH = os.getenv("DB_PATH", "scalper.db")
BOT_TZ = os.getenv("BOT_TZ", "Europe/Madrid")

try:
    TZ = ZoneInfo(BOT_TZ)
except Exception:
    TZ = ZoneInfo("UTC")


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=30000;")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scalp_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_signal_id TEXT,
            received_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            timeframe TEXT,
            entry_reason TEXT,
            sl_distance REAL,
            tp_distance REAL,
            payload_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            claimed_at TEXT,
            processed_at TEXT
        )
        """
    )

    # Lightweight migration path for older deployed DBs:
    # if the table already exists without these columns, add them.
    cur.execute("PRAGMA table_info(scalp_signals)")
    existing_cols = {row["name"] for row in cur.fetchall()}

    required_cols = {
        "external_signal_id": "TEXT",
        "claimed_at": "TEXT",
        "processed_at": "TEXT",
    }

    for col_name, col_type in required_cols.items():
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE scalp_signals ADD COLUMN {col_name} {col_type}")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_type TEXT,
            accepted INTEGER,
            reason TEXT,
            payload_json TEXT,
            created_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_scalp_signals_external_id
        ON scalp_signals(external_signal_id)
        WHERE external_signal_id IS NOT NULL
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scalp_signals_status_id
        ON scalp_signals(status, id)
        """
    )

    conn.commit()
    conn.close()


def log_webhook(message_type: str, payload: dict, accepted: bool, reason: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO webhook_log (message_type, accepted, reason, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            message_type,
            1 if accepted else 0,
            reason,
            json.dumps(payload, ensure_ascii=False),
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()


def validate_scalp_payload(data: dict):
    message_type = str(data.get("message_type", "")).strip()
    if message_type != "scalp_signal":
        return False, "ignored_not_scalp"

    direction = str(data.get("direction", "")).strip().lower()
    if direction not in {"buy", "sell"}:
        return False, "invalid_direction"

    symbol = str(data.get("symbol", "")).strip()
    if not symbol:
        return False, "missing_symbol"

    try:
        sl_distance = float(data.get("sl_distance"))
        tp_distance = float(data.get("tp_distance"))
    except Exception:
        return False, "invalid_distance_fields"

    if sl_distance <= 0 or tp_distance <= 0:
        return False, "non_positive_distance"

    return True, "ok"


@app.on_event("startup")
def startup_event():
    init_db()


@app.get("/")
def root():
    return {
        "status": "scalper_api_running",
        "db_path": DB_PATH,
        "timezone": str(TZ),
    }


@app.get("/health")
def health():
    return {"status": "ok", "time": now_iso()}


@app.post("/scalper")
async def scalper_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "reason": "invalid_json"}, status_code=400)

    ok, reason = validate_scalp_payload(data)
    if not ok:
        log_webhook(str(data.get("message_type", "")), data, False, reason)
        return {"status": reason}

    external_signal_id = str(data.get("signal_id", "")).strip() or None

    direction = str(data["direction"]).strip().lower()
    symbol = str(data["symbol"]).strip()
    timeframe = str(data.get("timeframe", "")).strip() or None
    entry_reason = str(data.get("entry_reason", "")).strip() or None
    sl_distance = float(data["sl_distance"])
    tp_distance = float(data["tp_distance"])

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO scalp_signals (
                external_signal_id, received_at, symbol, direction, timeframe,
                entry_reason, sl_distance, tp_distance, payload_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                external_signal_id,
                now_iso(),
                symbol,
                direction,
                timeframe,
                entry_reason,
                sl_distance,
                tp_distance,
                json.dumps(data, ensure_ascii=False),
            ),
        )
        signal_id = int(cur.lastrowid)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        log_webhook("scalp_signal", data, False, "duplicate_external_signal_id")
        return {"status": "duplicate_signal", "signal_id": external_signal_id}

    conn.close()
    log_webhook("scalp_signal", data, True, "queued")
    return {"status": "scalp_signal_queued", "signal_id": signal_id}


@app.get("/next_signal")
def next_signal():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM scalp_signals
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"status": "empty"}

    signal = dict(row)
    return {"status": "ok", "signal": signal}


@app.post("/ack_signal/{signal_id}")
def ack_signal(signal_id: int):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE scalp_signals
        SET status = 'processed', processed_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (now_iso(), signal_id),
    )
    conn.commit()
    updated = cur.rowcount

    if updated == 1:
        conn.close()
        return {"status": "processed", "signal_id": signal_id}

    cur.execute("SELECT status FROM scalp_signals WHERE id = ?", (signal_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"status": "not_found", "signal_id": signal_id}

    return {"status": "already_processed", "signal_id": signal_id}


@app.get("/signals_report")
def signals_report():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS cnt FROM scalp_signals")
    total = int(cur.fetchone()["cnt"])

    cur.execute(
        """
        SELECT status, COUNT(*) AS cnt
        FROM scalp_signals
        GROUP BY status
        ORDER BY status
        """
    )
    by_status = [dict(r) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT id, external_signal_id, symbol, direction, timeframe, status, received_at, processed_at
        FROM scalp_signals
        ORDER BY id DESC
        LIMIT 50
        """
    )
    latest = [dict(r) for r in cur.fetchall()]

    conn.close()

    return {
        "ok": True,
        "db_path": DB_PATH,
        "timezone": str(TZ),
        "total_signals": total,
        "by_status": by_status,
        "latest": latest,
    }
