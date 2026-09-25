import sqlite3
import threading
import time

import config as C

_lock = threading.Lock()
_conn = sqlite3.connect(C.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, ts INTEGER, score REAL,
    entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
    pcnt24 REAL, funding REAL,
    status TEXT DEFAULT 'open',      -- open|tp1|tp2|tp3|sl|expired
    best REAL DEFAULT 0,             -- MFE, % в нашу сторону
    worst REAL DEFAULT 0,            -- MAE, % против нас
    closed_ts INTEGER DEFAULT 0,
    taken INTEGER DEFAULT 0          -- 0 нет / 1 взял / -1 пропустил
);
CREATE TABLE IF NOT EXISTS mutes (symbol TEXT PRIMARY KEY, until INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL);
"""
with _lock:
    _conn.executescript(SCHEMA)
    _conn.commit()


def _q(sql, args=(), fetch=None):
    with _lock:
        cur = _conn.execute(sql, args)
        if fetch == "one":
            row = cur.fetchone()
        elif fetch == "all":
            row = cur.fetchall()
        else:
            row = cur.lastrowid
        _conn.commit()
        return row


# ---- настройки ----
def load_settings():
    for r in _q("SELECT key, value FROM settings", fetch="all"):
        if hasattr(C, r["key"]):
            cur = getattr(C, r["key"])
            setattr(C, r["key"], int(r["value"]) if isinstance(cur, int) else r["value"])


def save_setting(key, value):
    _q("INSERT INTO settings(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, float(value)))
    cur = getattr(C, key)
    setattr(C, key, int(value) if isinstance(cur, int) else float(value))


# ---- муты и кулдаун ----
def mute(symbol, hours):
    _q("INSERT INTO mutes(symbol,until) VALUES(?,?) "
       "ON CONFLICT(symbol) DO UPDATE SET until=excluded.until",
       (symbol, int(time.time()) + hours * 3600))


def is_muted(symbol) -> bool:
    r = _q("SELECT until FROM mutes WHERE symbol=?", (symbol,), fetch="one")
    return bool(r and r["until"] > time.time())


def recent_signal(symbol, hours) -> bool:
    r = _q("SELECT ts FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
           (symbol,), fetch="one")
    return bool(r and time.time() - r["ts"] < hours * 3600)


# ---- сигналы ----
def add_signal(s) -> int:
    return _q(
        "INSERT INTO signals(symbol,ts,score,entry,sl,tp1,tp2,tp3,pcnt24,funding) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (s.symbol, int(time.time()), s.score, s.entry, s.sl, s.tp1, s.tp2, s.tp3,
         s.pcnt24, s.funding),
    )


def open_signals():
    return _q("SELECT * FROM signals WHERE status='open'", fetch="all")


def update_signal(sid, **kw):
    if not kw:
        return
    sets = ",".join(f"{k}=?" for k in kw)
    _q(f"UPDATE signals SET {sets} WHERE id=?", (*kw.values(), sid))


def get_signal(sid):
    return _q("SELECT * FROM signals WHERE id=?", (sid,), fetch="one")


def stats(days=30):
    since = int(time.time()) - days * 86400
    rows = _q("SELECT * FROM signals WHERE ts>=?", (since,), fetch="all")
    closed = [r for r in rows if r["status"] != "open"]
    wins = [r for r in closed if r["status"].startswith("tp")]
    losses = [r for r in closed if r["status"] == "sl"]
    exp = [r for r in closed if r["status"] == "expired"]
    wr = len(wins) / len(closed) * 100 if closed else 0.0
    avg_best = sum(r["best"] for r in closed) / len(closed) if closed else 0.0
    avg_worst = sum(r["worst"] for r in closed) / len(closed) if closed else 0.0
    return {
        "total": len(rows), "open": len(rows) - len(closed), "closed": len(closed),
        "wins": len(wins), "losses": len(losses), "expired": len(exp),
        "winrate": wr, "avg_best": avg_best, "avg_worst": avg_worst,
    }
