diff --git a/main.py b/main.py
index 2d850bd1b9e62aea9dbe1f8a9d75bc9ba99ec86a..45fb4279864079eda51b0246b6dfa1e8c1b2c15f 100644
--- a/main.py
+++ b/main.py
@@ -1,362 +1,315 @@
 import json
+import os
 import sqlite3
 from datetime import datetime
+from zoneinfo import ZoneInfo
+
 from fastapi import FastAPI, Request
 from fastapi.responses import JSONResponse
 
-app = FastAPI(title="Paul Railway Backend v3")
+app = FastAPI(title="Scalper Railway Backend v2")
 
-DB_PATH = "paul_railway_v3.db"
+DB_PATH = os.getenv("DB_PATH", "scalper.db")
+BOT_TZ = os.getenv("BOT_TZ", "Europe/Madrid")
 
-DEFAULT_BIAS = {
-    "h4": "bearish",
-    "h1": "bearish",
-    "daily_bias": "strong_bearish",
-    "direction": "sell_only"
-}
+try:
+    TZ = ZoneInfo(BOT_TZ)
+except Exception:
+    TZ = ZoneInfo("UTC")
 
 
-def now_iso():
-    return datetime.now().isoformat()
+def now_iso() -> str:
+    return datetime.now(TZ).isoformat()
 
 
-def get_conn():
-    conn = sqlite3.connect(DB_PATH)
+def get_conn() -> sqlite3.Connection:
+    conn = sqlite3.connect(DB_PATH, timeout=30)
     conn.row_factory = sqlite3.Row
     return conn
 
 
 def init_db():
     conn = get_conn()
     cur = conn.cursor()
 
-    cur.execute("""
-    CREATE TABLE IF NOT EXISTS planner_queue (
-        id INTEGER PRIMARY KEY AUTOINCREMENT,
-        zone_id TEXT,
-        direction TEXT,
-        grade TEXT,
-        entry_low REAL,
-        entry_high REAL,
-        invalidation REAL,
-        payload_json TEXT,
-        status TEXT DEFAULT 'pending',
-        created_at TEXT,
-        processed_at TEXT
-    )
-    """)
-
-    cur.execute("""
-    CREATE TABLE IF NOT EXISTS webhook_log (
-        id INTEGER PRIMARY KEY AUTOINCREMENT,
-        message_type TEXT,
-        payload_json TEXT,
-        accepted INTEGER,
-        reason TEXT,
-        created_at TEXT
+    cur.execute("PRAGMA journal_mode=WAL;")
+    cur.execute("PRAGMA busy_timeout=30000;")
+
+    cur.execute(
+        """
+        CREATE TABLE IF NOT EXISTS scalp_signals (
+            id INTEGER PRIMARY KEY AUTOINCREMENT,
+            external_signal_id TEXT,
+            received_at TEXT NOT NULL,
+            symbol TEXT NOT NULL,
+            direction TEXT NOT NULL,
+            timeframe TEXT,
+            entry_reason TEXT,
+            sl_distance REAL,
+            tp_distance REAL,
+            payload_json TEXT,
+            status TEXT NOT NULL DEFAULT 'pending',
+            claimed_at TEXT,
+            processed_at TEXT
+        )
+        """
     )
-    """)
-
-    cur.execute("""
-    CREATE TABLE IF NOT EXISTS paul_bias (
-        id INTEGER PRIMARY KEY CHECK (id = 1),
-        h4 TEXT,
-        h1 TEXT,
-        daily_bias TEXT,
-        direction TEXT,
-        updated_at TEXT
+
+    # Lightweight migration path for older deployed DBs:
+    # if the table already exists without these columns, add them.
+    cur.execute("PRAGMA table_info(scalp_signals)")
+    existing_cols = {row["name"] for row in cur.fetchall()}
+
+    required_cols = {
+        "external_signal_id": "TEXT",
+        "claimed_at": "TEXT",
+        "processed_at": "TEXT",
+    }
+
+    for col_name, col_type in required_cols.items():
+        if col_name not in existing_cols:
+            cur.execute(f"ALTER TABLE scalp_signals ADD COLUMN {col_name} {col_type}")
+
+    cur.execute(
+        """
+        CREATE TABLE IF NOT EXISTS webhook_log (
+            id INTEGER PRIMARY KEY AUTOINCREMENT,
+            message_type TEXT,
+            accepted INTEGER,
+            reason TEXT,
+            payload_json TEXT,
+            created_at TEXT
+        )
+        """
     )
-    """)
-
-    cur.execute("""
-        INSERT INTO paul_bias (id, h4, h1, daily_bias, direction, updated_at)
-        VALUES (1, ?, ?, ?, ?, ?)
-        ON CONFLICT(id) DO NOTHING
-    """, (
-        DEFAULT_BIAS["h4"],
-        DEFAULT_BIAS["h1"],
-        DEFAULT_BIAS["daily_bias"],
-        DEFAULT_BIAS["direction"],
-        now_iso(),
-    ))
 
-    conn.commit()
-    conn.close()
+    cur.execute(
+        """
+        CREATE UNIQUE INDEX IF NOT EXISTS idx_scalp_signals_external_id
+        ON scalp_signals(external_signal_id)
+        WHERE external_signal_id IS NOT NULL
+        """
+    )
 
+    cur.execute(
+        """
+        CREATE INDEX IF NOT EXISTS idx_scalp_signals_status_id
+        ON scalp_signals(status, id)
+        """
+    )
 
-def log_webhook(message_type: str, payload: dict, accepted: bool, reason: str = ""):
-    conn = get_conn()
-    cur = conn.cursor()
-    cur.execute("""
-        INSERT INTO webhook_log (
-            message_type, payload_json, accepted, reason, created_at
-        ) VALUES (?, ?, ?, ?, ?)
-    """, (
-        message_type,
-        json.dumps(payload, ensure_ascii=False),
-        1 if accepted else 0,
-        reason,
-        now_iso(),
-    ))
     conn.commit()
     conn.close()
 
 
-def get_bias_row():
+def log_webhook(message_type: str, payload: dict, accepted: bool, reason: str):
     conn = get_conn()
     cur = conn.cursor()
-    cur.execute("SELECT h4, h1, daily_bias, direction, updated_at FROM paul_bias WHERE id = 1")
-    row = cur.fetchone()
+    cur.execute(
+        """
+        INSERT INTO webhook_log (message_type, accepted, reason, payload_json, created_at)
+        VALUES (?, ?, ?, ?, ?)
+        """,
+        (
+            message_type,
+            1 if accepted else 0,
+            reason,
+            json.dumps(payload, ensure_ascii=False),
+            now_iso(),
+        ),
+    )
+    conn.commit()
     conn.close()
-    if not row:
-        return dict(DEFAULT_BIAS)
-    return dict(row)
 
 
-@app.get("/")
-def root():
-    return {"ok": True, "service": "paul_railway_backend_v3"}
-
+def validate_scalp_payload(data: dict):
+    message_type = str(data.get("message_type", "")).strip()
+    if message_type != "scalp_signal":
+        return False, "ignored_not_scalp"
 
-@app.get("/paul_bias")
-def paul_bias():
-    row = get_bias_row()
-    return {"ok": True, "bias": row}
+    direction = str(data.get("direction", "")).strip().lower()
+    if direction not in {"buy", "sell"}:
+        return False, "invalid_direction"
 
+    symbol = str(data.get("symbol", "")).strip()
+    if not symbol:
+        return False, "missing_symbol"
 
-@app.post("/set_paul_bias")
-async def set_paul_bias(req: Request):
     try:
-        data = await req.json()
+        sl_distance = float(data.get("sl_distance"))
+        tp_distance = float(data.get("tp_distance"))
     except Exception:
-        return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)
+        return False, "invalid_distance_fields"
 
-    h4 = str(data.get("h4", "")).strip().lower()
-    h1 = str(data.get("h1", "")).strip().lower()
-    daily_bias = str(data.get("daily_bias", "")).strip().lower()
-    direction = str(data.get("direction", "")).strip().lower()
-
-    if h4 not in {"bullish", "bearish"}:
-        return JSONResponse({"ok": False, "reason": "bad_h4"}, status_code=400)
-    if h1 not in {"bullish", "bearish"}:
-        return JSONResponse({"ok": False, "reason": "bad_h1"}, status_code=400)
-    if daily_bias not in {"bullish", "bearish", "strong_bullish", "strong_bearish"}:
-        return JSONResponse({"ok": False, "reason": "bad_daily_bias"}, status_code=400)
-    if direction not in {"buy_only", "sell_only", "both"}:
-        return JSONResponse({"ok": False, "reason": "bad_direction"}, status_code=400)
-
-    conn = get_conn()
-    cur = conn.cursor()
-    cur.execute("""
-        INSERT INTO paul_bias (id, h4, h1, daily_bias, direction, updated_at)
-        VALUES (1, ?, ?, ?, ?, ?)
-        ON CONFLICT(id) DO UPDATE SET
-            h4 = excluded.h4,
-            h1 = excluded.h1,
-            daily_bias = excluded.daily_bias,
-            direction = excluded.direction,
-            updated_at = excluded.updated_at
-    """, (h4, h1, daily_bias, direction, now_iso()))
-    conn.commit()
-    conn.close()
+    if sl_distance <= 0 or tp_distance <= 0:
+        return False, "non_positive_distance"
 
-    return {"ok": True, "bias": get_bias_row()}
+    return True, "ok"
 
 
-@app.post("/webhook")
-async def webhook(req: Request):
-    try:
-        data = await req.json()
-    except Exception:
-        return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)
+@app.on_event("startup")
+def startup_event():
+    init_db()
 
-    message_type = str(data.get("message_type", "")).strip()
 
-    if message_type == "bias_update":
-        h4 = str(data.get("h4", "")).strip().lower()
-        h1 = str(data.get("h1", "")).strip().lower()
-        daily_bias = str(data.get("daily_bias", "")).strip().lower()
-        direction = str(data.get("direction", "")).strip().lower()
-
-        if h4 not in {"bullish", "bearish"} or h1 not in {"bullish", "bearish"} or daily_bias not in {"bullish", "bearish", "strong_bullish", "strong_bearish"} or direction not in {"buy_only", "sell_only", "both"}:
-            log_webhook(message_type, data, False, "bad_bias_payload")
-            return {"ok": True, "updated": False, "reason": "bad_bias_payload"}
-
-        conn = get_conn()
-        cur = conn.cursor()
-        cur.execute("""
-            INSERT INTO paul_bias (id, h4, h1, daily_bias, direction, updated_at)
-            VALUES (1, ?, ?, ?, ?, ?)
-            ON CONFLICT(id) DO UPDATE SET
-                h4 = excluded.h4,
-                h1 = excluded.h1,
-                daily_bias = excluded.daily_bias,
-                direction = excluded.direction,
-                updated_at = excluded.updated_at
-        """, (h4, h1, daily_bias, direction, now_iso()))
-        conn.commit()
-        conn.close()
+@app.get("/")
+def root():
+    return {
+        "status": "scalper_api_running",
+        "db_path": DB_PATH,
+        "timezone": str(TZ),
+    }
 
-        log_webhook(message_type, data, True, "bias_updated")
-        return {"ok": True, "updated": True, "bias": get_bias_row()}
 
-    if message_type != "new_zone":
-        log_webhook(message_type, data, False, "ignored_message_type")
-        return {"ok": True, "zones": 0, "daily_plan_sent": False, "reason": "ignored_message_type"}
+@app.get("/health")
+def health():
+    return {"status": "ok", "time": now_iso()}
 
-    zone_id = str(data.get("zone_id", "")).strip()
-    direction = str(data.get("direction", "")).strip().lower()
-    grade = str(data.get("grade", "")).strip().upper()
 
+@app.post("/scalper")
+async def scalper_webhook(request: Request):
     try:
-        entry_low = float(data.get("entry_low"))
-        entry_high = float(data.get("entry_high"))
-        invalidation = float(data.get("invalidation"))
+        data = await request.json()
     except Exception:
-        log_webhook(message_type, data, False, "bad_price_fields")
-        return {"ok": True, "zones": 0, "daily_plan_sent": False, "reason": "bad_price_fields"}
+        return JSONResponse({"status": "error", "reason": "invalid_json"}, status_code=400)
 
-    if not zone_id:
-        log_webhook(message_type, data, False, "missing_zone_id")
-        return {"ok": True, "zones": 0, "daily_plan_sent": False, "reason": "missing_zone_id"}
+    ok, reason = validate_scalp_payload(data)
+    if not ok:
+        log_webhook(str(data.get("message_type", "")), data, False, reason)
+        return {"status": reason}
 
-    if direction not in ("buy", "sell"):
-        log_webhook(message_type, data, False, "bad_direction")
-        return {"ok": True, "zones": 0, "daily_plan_sent": False, "reason": "bad_direction"}
+    external_signal_id = str(data.get("signal_id", "")).strip() or None
 
-    if grade not in ("SNIPER", "OK", "RISKY"):
-        log_webhook(message_type, data, False, "bad_grade")
-        return {"ok": True, "zones": 0, "daily_plan_sent": False, "reason": "bad_grade"}
+    direction = str(data["direction"]).strip().lower()
+    symbol = str(data["symbol"]).strip()
+    timeframe = str(data.get("timeframe", "")).strip() or None
+    entry_reason = str(data.get("entry_reason", "")).strip() or None
+    sl_distance = float(data["sl_distance"])
+    tp_distance = float(data["tp_distance"])
 
     conn = get_conn()
     cur = conn.cursor()
-    cur.execute("""
-        SELECT id
-        FROM planner_queue
-        WHERE zone_id = ?
-          AND status IN ('pending', 'processed')
-        ORDER BY id DESC
-        LIMIT 1
-    """, (zone_id,))
-    existing = cur.fetchone()
 
-    if existing:
+    try:
+        cur.execute(
+            """
+            INSERT INTO scalp_signals (
+                external_signal_id, received_at, symbol, direction, timeframe,
+                entry_reason, sl_distance, tp_distance, payload_json, status
+            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
+            """,
+            (
+                external_signal_id,
+                now_iso(),
+                symbol,
+                direction,
+                timeframe,
+                entry_reason,
+                sl_distance,
+                tp_distance,
+                json.dumps(data, ensure_ascii=False),
+            ),
+        )
+        signal_id = int(cur.lastrowid)
+        conn.commit()
+    except sqlite3.IntegrityError:
         conn.close()
-        log_webhook(message_type, data, False, "duplicate_zone_id")
-        return {"ok": True, "zones": 0, "daily_plan_sent": False, "reason": "duplicate_zone_id"}
-
-    cur.execute("""
-        INSERT INTO planner_queue (
-            zone_id, direction, grade, entry_low, entry_high, invalidation,
-            payload_json, status, created_at
-        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
-    """, (
-        zone_id,
-        direction,
-        grade,
-        entry_low,
-        entry_high,
-        invalidation,
-        json.dumps(data, ensure_ascii=False),
-        now_iso(),
-    ))
+        log_webhook("scalp_signal", data, False, "duplicate_external_signal_id")
+        return {"status": "duplicate_signal", "signal_id": external_signal_id}
 
-    conn.commit()
     conn.close()
-
-    log_webhook(message_type, data, True, "queued")
-    return {"ok": True, "zones": 1, "daily_plan_sent": True, "zone_id": zone_id}
+    log_webhook("scalp_signal", data, True, "queued")
+    return {"status": "scalp_signal_queued", "signal_id": signal_id}
 
 
-@app.get("/next_planner_signal")
-def next_planner_signal():
+@app.get("/next_signal")
+def next_signal():
     conn = get_conn()
     cur = conn.cursor()
-
-    cur.execute("""
+    cur.execute(
+        """
         SELECT *
-        FROM planner_queue
+        FROM scalp_signals
         WHERE status = 'pending'
         ORDER BY id ASC
         LIMIT 1
-    """)
+        """
+    )
     row = cur.fetchone()
     conn.close()
 
     if not row:
         return {"status": "empty"}
 
-    signal = {
-        "id": row["id"],
-        "zone_id": row["zone_id"],
-        "direction": row["direction"],
-        "grade": row["grade"],
-        "entry_low": row["entry_low"],
-        "entry_high": row["entry_high"],
-        "invalidation": row["invalidation"],
-    }
-
+    signal = dict(row)
     return {"status": "ok", "signal": signal}
 
 
-@app.post("/ack_planner_signal/{signal_id}")
-def ack_planner_signal(signal_id: int):
+@app.post("/ack_signal/{signal_id}")
+def ack_signal(signal_id: int):
     conn = get_conn()
     cur = conn.cursor()
-    cur.execute("""
-        UPDATE planner_queue
-        SET status = 'processed',
-            processed_at = ?
-        WHERE id = ?
-    """, (now_iso(), signal_id))
+
+    cur.execute(
+        """
+        UPDATE scalp_signals
+        SET status = 'processed', processed_at = ?
+        WHERE id = ? AND status = 'pending'
+        """,
+        (now_iso(), signal_id),
+    )
     conn.commit()
+    updated = cur.rowcount
+
+    if updated == 1:
+        conn.close()
+        return {"status": "processed", "signal_id": signal_id}
+
+    cur.execute("SELECT status FROM scalp_signals WHERE id = ?", (signal_id,))
+    row = cur.fetchone()
     conn.close()
-    return {"status": "processed", "signal_id": signal_id}
 
+    if not row:
+        return {"status": "not_found", "signal_id": signal_id}
+
+    return {"status": "already_processed", "signal_id": signal_id}
 
-@app.get("/report")
-def report():
+
+@app.get("/signals_report")
+def signals_report():
     conn = get_conn()
     cur = conn.cursor()
 
-    cur.execute("SELECT COUNT(*) AS cnt FROM planner_queue")
-    total = cur.fetchone()["cnt"]
+    cur.execute("SELECT COUNT(*) AS cnt FROM scalp_signals")
+    total = int(cur.fetchone()["cnt"])
 
-    cur.execute("""
+    cur.execute(
+        """
         SELECT status, COUNT(*) AS cnt
-        FROM planner_queue
+        FROM scalp_signals
         GROUP BY status
         ORDER BY status
-    """)
+        """
+    )
     by_status = [dict(r) for r in cur.fetchall()]
 
-    cur.execute("""
-        SELECT zone_id, direction, grade, status, created_at, processed_at
-        FROM planner_queue
+    cur.execute(
+        """
+        SELECT id, external_signal_id, symbol, direction, timeframe, status, received_at, processed_at
+        FROM scalp_signals
         ORDER BY id DESC
         LIMIT 50
-    """)
+        """
+    )
     latest = [dict(r) for r in cur.fetchall()]
 
-    cur.execute("""
-        SELECT message_type, accepted, reason, created_at
-        FROM webhook_log
-        ORDER BY id DESC
-        LIMIT 50
-    """)
-    webhook_log = [dict(r) for r in cur.fetchall()]
-
     conn.close()
 
     return {
         "ok": True,
         "db_path": DB_PATH,
-        "planner_total": total,
+        "timezone": str(TZ),
+        "total_signals": total,
         "by_status": by_status,
-        "latest_signals": latest,
-        "webhook_log": webhook_log,
-        "bias": get_bias_row(),
+        "latest": latest,
     }
-
-
-init_db()
