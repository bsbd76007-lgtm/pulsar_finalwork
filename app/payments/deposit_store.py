"""Small SQLite ledger for Crypto Pay deposits; credits are idempotent."""
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

DB_PATH = os.getenv("PULSAR_DB_PATH", str(Path(__file__).resolve().parents[2] / "pulsar_fan.sqlite3"))


def _connect():
    # Keep per-connection setup cheap; WAL is enabled once in init_store().
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_store():
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            balance_usd TEXT NOT NULL DEFAULT '0.00'
        );
        CREATE TABLE IF NOT EXISTS deposits (
            invoice_id TEXT PRIMARY KEY,
            telegram_id INTEGER NOT NULL,
            amount_usd TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            credited INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(telegram_id, created_at);
        """)


def claim_task_reward(telegram_id: int, task_key: str, amount: Decimal) -> bool:
    """Credit a one-time reward atomically; independent from deposit idempotency."""
    init_store()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS task_rewards (telegram_id INTEGER NOT NULL, task_key TEXT NOT NULL, amount_usd TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (telegram_id, task_key))")
        conn.execute("INSERT INTO users(telegram_id) VALUES (?) ON CONFLICT(telegram_id) DO NOTHING", (telegram_id,))
        cur = conn.execute("INSERT OR IGNORE INTO task_rewards(telegram_id, task_key, amount_usd) VALUES (?, ?, ?)", (telegram_id, task_key, f"{amount:.2f}"))
        if cur.rowcount != 1:
            return False
        current = conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()[0]
        updated = Decimal(current) + amount
        conn.execute("UPDATE users SET balance_usd=? WHERE telegram_id=?", (f"{updated:.2f}", telegram_id))
        return True


def save_invoice(invoice_id: str | int, telegram_id: int, amount_usd: Decimal):
    with _connect() as conn:
        conn.execute("INSERT INTO users(telegram_id) VALUES (?) ON CONFLICT(telegram_id) DO NOTHING", (telegram_id,))
        conn.execute(
            "INSERT INTO deposits(invoice_id, telegram_id, amount_usd, status, credited) VALUES (?, ?, ?, 'pending', 0)",
            (str(invoice_id), telegram_id, f"{amount_usd:.2f}"),
        )


def get_balance(telegram_id: int) -> Decimal:
    init_store()
    with _connect() as conn:
        row = conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        return Decimal(row[0]) if row else Decimal("0.00")


def get_deposit(invoice_id: str | int):
    init_store()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM deposits WHERE invoice_id=?", (str(invoice_id),)).fetchone()
        return dict(row) if row else None


def mark_paid_and_credit(invoice_id: str | int) -> bool:
    """Atomically credit the recorded invoice once. Returns True only on first credit."""
    init_store()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT telegram_id, amount_usd, credited FROM deposits WHERE invoice_id=?", (str(invoice_id),)).fetchone()
        if not row:
            return False
        if row["credited"]:
            conn.execute("UPDATE deposits SET status='paid' WHERE invoice_id=?", (str(invoice_id),))
            return False
        conn.execute("INSERT INTO users(telegram_id) VALUES (?) ON CONFLICT(telegram_id) DO NOTHING", (row["telegram_id"],))
        current = conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (row["telegram_id"],)).fetchone()[0]
        updated = Decimal(current) + Decimal(row["amount_usd"])
        conn.execute("UPDATE users SET balance_usd=? WHERE telegram_id=?", (f"{updated:.2f}", row["telegram_id"]))
        conn.execute("UPDATE deposits SET status='paid', credited=1 WHERE invoice_id=?", (str(invoice_id),))
        # Track casino profile progression only after a confirmed, idempotent deposit.
        conn.execute("""CREATE TABLE IF NOT EXISTS profile_stats (
            telegram_id INTEGER PRIMARY KEY, games_played INTEGER NOT NULL DEFAULT 0,
            turnover_usd TEXT NOT NULL DEFAULT '0.00', cashback_usd TEXT NOT NULL DEFAULT '0.00',
            pax_balance TEXT NOT NULL DEFAULT '0', turnover_abx INTEGER NOT NULL DEFAULT 0,
            level_override TEXT NOT NULL DEFAULT '', xp INTEGER NOT NULL DEFAULT 0)""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(profile_stats)").fetchall()}
        if "xp" not in cols:
            conn.execute("ALTER TABLE profile_stats ADD COLUMN xp INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO profile_stats(telegram_id) VALUES (?) ON CONFLICT(telegram_id) DO NOTHING", (row["telegram_id"],))
        conn.execute("UPDATE profile_stats SET xp=xp+? WHERE telegram_id=?", (max(1, int(Decimal(row["amount_usd"]) * 100)), row["telegram_id"]))
        return True



def record_stars_donation(telegram_id: int, charge_id: str, amount: int, payload: str) -> bool:
    """Persist a Telegram Stars donation once per Telegram charge ID."""
    if not charge_id or amount not in {25, 50, 100, 250}:
        return False
    init_store()
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS stars_donations (
            charge_id TEXT PRIMARY KEY,
            telegram_id INTEGER NOT NULL,
            amount_stars INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        cur = conn.execute(
            "INSERT OR IGNORE INTO stars_donations(charge_id,telegram_id,amount_stars,payload) VALUES (?,?,?,?)",
            (charge_id, int(telegram_id), int(amount), payload),
        )
        return cur.rowcount == 1


def update_deposit_status(invoice_id: str | int, status: str):
    with _connect() as conn:
        conn.execute("UPDATE deposits SET status=? WHERE invoice_id=? AND credited=0", (status, str(invoice_id)))


def get_pending_deposits(limit: int = 100):
    """Return uncredited invoices for the background payment poller."""
    init_store()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT invoice_id, telegram_id, amount_usd, status FROM deposits "
            "WHERE credited=0 AND status NOT IN ('expired', 'cancelled', 'failed') "
            "ORDER BY created_at LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

# Profile progression. This project version has no stake settlement, so paid
# turnover and cashback remain zero until a supported wagering flow exists.
LEVELS = [("Рекрут", Decimal("0")), ("Бронза", Decimal("100")), ("Серебро", Decimal("500")), ("Золото", Decimal("2000")), ("Платина", Decimal("5000")), ("Алмаз", Decimal("10000"))]

def init_profile_store():
    init_store()
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS profile_stats (
            telegram_id INTEGER PRIMARY KEY, games_played INTEGER NOT NULL DEFAULT 0,
            turnover_usd TEXT NOT NULL DEFAULT '0.00', cashback_usd TEXT NOT NULL DEFAULT '0.00',
            pax_balance TEXT NOT NULL DEFAULT '0', turnover_abx INTEGER NOT NULL DEFAULT 0,
            level_override TEXT NOT NULL DEFAULT '', xp INTEGER NOT NULL DEFAULT 0)""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(profile_stats)").fetchall()}
        if "turnover_abx" not in columns:
            conn.execute("ALTER TABLE profile_stats ADD COLUMN turnover_abx INTEGER NOT NULL DEFAULT 0")
        if "level_override" not in columns:
            conn.execute("ALTER TABLE profile_stats ADD COLUMN level_override TEXT NOT NULL DEFAULT ''")
        if "xp" not in columns:
            conn.execute("ALTER TABLE profile_stats ADD COLUMN xp INTEGER NOT NULL DEFAULT 0")
        # Keep the configured owner turnover, but never derive XP from money turnover.
        owner_id = 844181709
        conn.execute("INSERT INTO profile_stats(telegram_id, turnover_usd, level_override, xp) VALUES (?, '70075.00', 'Грандмастер', 0) ON CONFLICT(telegram_id) DO UPDATE SET turnover_usd=MAX(CAST(profile_stats.turnover_usd AS REAL), 70075.00), level_override='Грандмастер'", (owner_id,))
        # Migrate the previous version's erroneous XP mirror once; XP thereafter changes only with gameplay.
        conn.execute("UPDATE profile_stats SET xp=games_played * 10 WHERE telegram_id=? AND xp=70075 AND CAST(turnover_usd AS REAL)>=70075.00 AND level_override='Грандмастер'", (owner_id,))

def record_game(telegram_id: int):
    init_profile_store()
    with _connect() as conn:
        conn.execute("INSERT INTO profile_stats(telegram_id,games_played,xp) VALUES (?,1,10) ON CONFLICT(telegram_id) DO UPDATE SET games_played=games_played+1, xp=xp+10", (telegram_id,))

def get_profile_stats(telegram_id: int) -> dict:
    init_profile_store()
    # Read profile, balance and confirmed deposits in one connection to reduce
    # repeated SQLite setup/locking when the user opens their profile frequently.
    with _connect() as conn:
        row = conn.execute("SELECT games_played,turnover_usd,cashback_usd,pax_balance,turnover_abx,level_override,xp FROM profile_stats WHERE telegram_id=?", (telegram_id,)).fetchone()
        balance_row = conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        deposited_rows = conn.execute("SELECT amount_usd FROM deposits WHERE telegram_id=? AND credited=1", (telegram_id,)).fetchall()
    data = dict(row) if row else {"games_played": 0, "turnover_usd": "0.00", "cashback_usd": "0.00", "pax_balance": "0", "turnover_abx": 0, "xp": 0, "level_override": ""}
    turnover = Decimal(data["turnover_usd"])
    idx = max(i for i, (_, threshold) in enumerate(LEVELS) if turnover >= threshold)
    data["level"] = data.get("level_override") or LEVELS[idx][0]
    data["next_threshold"] = LEVELS[idx + 1][1] if idx + 1 < len(LEVELS) else None
    data["progress"] = 100 if data["next_threshold"] is None else int(min(100, (turnover - LEVELS[idx][1]) / (data["next_threshold"] - LEVELS[idx][1]) * 100))
    data["turnover"] = turnover
    data["balance"] = Decimal(balance_row["balance_usd"]) if balance_row else Decimal("0.00")
    data["total_deposits"] = sum((Decimal(r["amount_usd"]) for r in deposited_rows), Decimal("0.00")).quantize(Decimal("0.01"))
    return data


BET_OPTIONS = [Decimal(x) for x in ("0.10", "0.50", "1.00", "2.00", "5.00", "10.00")]

def init_betting_store():
    init_profile_store()
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS wagers (
            wager_id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, game_key TEXT NOT NULL,
            stake_usd TEXT NOT NULL, status TEXT NOT NULL, dice_value INTEGER,
            payout_usd TEXT NOT NULL DEFAULT '0.00', cashback_usd TEXT NOT NULL DEFAULT '0.00',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_wagers_user ON wagers(telegram_id, created_at);
        """)

def place_bet(wager_id: str, telegram_id: int, game_key: str, stake: Decimal) -> bool:
    init_betting_store()
    stake = Decimal(stake).quantize(Decimal("0.01"))
    if stake not in BET_OPTIONS: raise ValueError("Unsupported stake")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM wagers WHERE wager_id=?", (wager_id,)).fetchone(): return False
        conn.execute("INSERT INTO users(telegram_id) VALUES (?) ON CONFLICT(telegram_id) DO NOTHING", (telegram_id,))
        bal = Decimal(conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()[0])
        if bal < stake: return False
        conn.execute("UPDATE users SET balance_usd=? WHERE telegram_id=?", (f"{bal-stake:.2f}", telegram_id))
        conn.execute("INSERT INTO wagers(wager_id,telegram_id,game_key,stake_usd,status) VALUES (?,?,?,?, 'pending')", (wager_id, telegram_id, game_key, f"{stake:.2f}"))
        return True

def settle_bet(wager_id: str, dice_value: int, multiplier: Decimal) -> dict | None:
    init_betting_store()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM wagers WHERE wager_id=?", (wager_id,)).fetchone()
        if not row or row["status"] != "pending": return None
        stake = Decimal(row["stake_usd"]); payout = (stake * Decimal(multiplier)).quantize(Decimal("0.01"))
        cashback = (stake * Decimal("0.01")).quantize(Decimal("0.01")); uid = row["telegram_id"]
        conn.execute("INSERT INTO profile_stats(telegram_id) VALUES (?) ON CONFLICT(telegram_id) DO NOTHING", (uid,))
        bal = Decimal(conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (uid,)).fetchone()[0])
        conn.execute("UPDATE users SET balance_usd=? WHERE telegram_id=?", (f"{bal+payout+cashback:.2f}", uid))
        stats = conn.execute("SELECT turnover_usd,cashback_usd FROM profile_stats WHERE telegram_id=?", (uid,)).fetchone()
        conn.execute("UPDATE profile_stats SET turnover_usd=?, cashback_usd=?, games_played=games_played+1, xp=xp+? WHERE telegram_id=?", (f"{Decimal(stats['turnover_usd'])+stake:.2f}", f"{Decimal(stats['cashback_usd'])+cashback:.2f}", max(1, int(stake * 10)), uid))
        conn.execute("UPDATE wagers SET status='settled', dice_value=?, payout_usd=?, cashback_usd=? WHERE wager_id=? AND status='pending'", (dice_value, f"{payout:.2f}", f"{cashback:.2f}", wager_id))
        return {"stake":stake,"payout":payout,"cashback":cashback,"net":payout-stake,"balance":bal+payout+cashback}

def get_game_history(telegram_id: int, limit: int = 10) -> list[dict]:
    """Return recent settled games for the user's history screen."""
    init_betting_store()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT game_key, stake_usd, dice_value, payout_usd, cashback_usd, created_at "
            "FROM wagers WHERE telegram_id=? AND status='settled' ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (int(telegram_id), max(1, min(int(limit), 50))),
        ).fetchall()
        return [dict(row) for row in rows]


def refund_bet(wager_id: str) -> bool:
    init_betting_store()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT telegram_id,stake_usd FROM wagers WHERE wager_id=? AND status='pending'", (wager_id,)).fetchone()
        if not row: return False
        bal=Decimal(conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (row['telegram_id'],)).fetchone()[0])
        conn.execute("UPDATE users SET balance_usd=? WHERE telegram_id=?", (f"{bal+Decimal(row['stake_usd']):.2f}",row['telegram_id']))
        conn.execute("UPDATE wagers SET status='refunded' WHERE wager_id=? AND status='pending'", (wager_id,))
        return True


WITHDRAWAL_OPTIONS = [Decimal(x) for x in ("1.00", "5.00", "10.00", "25.00", "50.00")]

def init_withdrawal_store():
    init_store()
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
            withdrawal_id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, recipient_user_id INTEGER NOT NULL,
            amount_usd TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', crypto_transfer_id TEXT,
            error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(telegram_id, created_at)")

def reserve_withdrawal(withdrawal_id: str, telegram_id: int, recipient_user_id: int, amount: Decimal) -> bool:
    init_withdrawal_store()
    amount = Decimal(amount).quantize(Decimal("0.01"))
    if amount not in WITHDRAWAL_OPTIONS or recipient_user_id <= 0:
        raise ValueError("Unsupported withdrawal amount or recipient")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM withdrawals WHERE withdrawal_id=?", (withdrawal_id,)).fetchone():
            return False
        row = conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if not row or Decimal(row[0]) < amount:
            return False
        conn.execute("UPDATE users SET balance_usd=? WHERE telegram_id=?", (f"{Decimal(row[0])-amount:.2f}", telegram_id))
        conn.execute("INSERT INTO withdrawals(withdrawal_id,telegram_id,recipient_user_id,amount_usd,status) VALUES (?,?,?,?, 'processing')", (withdrawal_id, telegram_id, recipient_user_id, f"{amount:.2f}"))
        return True

def finish_withdrawal(withdrawal_id: str, status: str, transfer_id: str | None = None, error: str | None = None, refund: bool = False) -> bool:
    """Finalize once. Refund only when the provider definitively rejected the transfer."""
    init_withdrawal_store()
    if status not in ("paid", "failed", "review"):
        raise ValueError("Invalid withdrawal status")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT telegram_id, amount_usd, status FROM withdrawals WHERE withdrawal_id=?", (withdrawal_id,)).fetchone()
        if not row or row["status"] != "processing":
            return False
        if refund:
            balrow = conn.execute("SELECT balance_usd FROM users WHERE telegram_id=?", (row["telegram_id"],)).fetchone()
            bal = Decimal(balrow[0]) if balrow else Decimal("0.00")
            conn.execute("INSERT INTO users(telegram_id,balance_usd) VALUES (?,?) ON CONFLICT(telegram_id) DO UPDATE SET balance_usd=excluded.balance_usd", (row["telegram_id"], f"{bal+Decimal(row['amount_usd']):.2f}"))
        conn.execute("UPDATE withdrawals SET status=?, crypto_transfer_id=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE withdrawal_id=? AND status='processing'", (status, transfer_id, (error or "")[:500], withdrawal_id))
        return True
