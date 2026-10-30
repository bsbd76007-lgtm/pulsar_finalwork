"""Per-chat community economy, collection game, and moderation persistence.

This subsystem deliberately uses separate tables and a separate chat currency;
it never touches the casino wallet/deposit tables.
"""
import os
import random
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("PULSAR_DB_PATH", str(Path(__file__).resolve().parents[1] / "pulsar_fan.sqlite3"))
_INITIALIZED_PATHS: set[str] = set()

ITEMS = {
    "дерево": {"emoji": "🪵", "sell": 3, "buy": 7, "rarity": "обычный"},
    "камень": {"emoji": "🪨", "sell": 4, "buy": 9, "rarity": "обычный"},
    "рыба": {"emoji": "🐟", "sell": 8, "buy": 16, "rarity": "обычный"},
    "кристалл": {"emoji": "💎", "sell": 30, "buy": 65, "rarity": "редкий"},
    "корона": {"emoji": "👑", "sell": 100, "buy": 220, "rarity": "легендарный"},
}


def connect():
    """Open a tuned SQLite connection without renegotiating WAL per query."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


WORLD_CARDS = {
    "дворовый_кот": {"name": "Дворовый кот", "emoji": "🐈", "rarity": "обычная", "value": 8, "weight": 42},
    "скейтер": {"name": "Скейтер с района", "emoji": "🛹", "rarity": "обычная", "value": 10, "weight": 32},
    "ночной_курьер": {"name": "Ночной курьер", "emoji": "🛵", "rarity": "редкая", "value": 25, "weight": 16},
    "король_крыши": {"name": "Король крыши", "emoji": "👑", "rarity": "эпическая", "value": 70, "weight": 7},
    "призрак_метро": {"name": "Призрак метро", "emoji": "👻", "rarity": "легендарная", "value": 180, "weight": 2},
    "nova_легенда": {"name": "Легенда PULSAR", "emoji": "🌌", "rarity": "мифическая", "value": 500, "weight": 1},
}
WORLD_EVENTS = [
    {"key": "мусорный_рейд", "title": "Мусорный рейд", "emoji": "🗑️", "desc": "Собери хлам района: обычные карты встречаются чаще, легенды всё ещё редки.", "bonus": 4},
    {"key": "ночь_неона", "title": "Ночь неона", "emoji": "🌃", "desc": "Ночной город активен: редкие и эпические карты ценнее.", "bonus": 7},
    {"key": "дождь_монет", "title": "Дождь коинов", "emoji": "🪙", "desc": "Каждый поиск приносит дополнительные коины.", "bonus": 10},
    {"key": "охота_за_легендой", "title": "Охота за легендой", "emoji": "🔎", "desc": "Шанс найти легендарную карту немного повышен.", "bonus": 5},
    {"key": "дворовый_турнир", "title": "Дворовый турнир", "emoji": "🏆", "desc": "Участники собирают карты и получают бонусные коины за поиск.", "bonus": 8},
    {"key": "космический_шторм", "title": "Космический шторм", "emoji": "🌌", "desc": "Поток энергии накрыл район: поиски приносят больше коинов.", "bonus": 9},
    {"key": "охота_за_стикерами", "title": "Охота за артефактами", "emoji": "🧩", "desc": "Редкие находки попадаются чаще обычного — самое время пополнить альбом.", "bonus": 6},
    {"key": "ночь_призраков", "title": "Ночь призраков", "emoji": "👻", "desc": "В переулках замечены странные силуэты. Будь готов к неожиданной находке.", "bonus": 7},
    {"key": "ярмарка_района", "title": "Ярмарка района", "emoji": "🏪", "desc": "Торговцы активны: поиски дают дополнительные коины для покупок на рынке.", "bonus": 5},
    {"key": "кометный_дождь", "title": "Кометный дождь", "emoji": "☄️", "desc": "Кометы осветили небо, а удачные поиски приносят щедрую награду.", "bonus": 11},
]


def init_community_store():
    db_key = str(Path(DB_PATH).resolve())
    if db_key in _INITIALIZED_PATHS and Path(DB_PATH).exists():
        return
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS community_wallets (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT NOT NULL DEFAULT '',
            coins INTEGER NOT NULL DEFAULT 0, xp INTEGER NOT NULL DEFAULT 0,
            last_collect INTEGER NOT NULL DEFAULT 0, last_daily TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS community_inventory (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, item TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,user_id,item));
        CREATE TABLE IF NOT EXISTS community_messages (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS community_activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_community_activity_period
            ON community_activity_events(chat_id, created_at, user_id);
        CREATE TABLE IF NOT EXISTS community_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS community_moderation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS nova_cards (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, card_key TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,user_id,card_key));
        CREATE TABLE IF NOT EXISTS nova_world_events (
            chat_id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS nova_world_claims (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, event_key TEXT NOT NULL,
            last_claim INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,user_id,event_key));
        CREATE TABLE IF NOT EXISTS global_weekly_activity (
            week_start TEXT NOT NULL, user_id INTEGER NOT NULL, username TEXT NOT NULL DEFAULT '',
            messages INTEGER NOT NULL DEFAULT 0, points INTEGER NOT NULL DEFAULT 0,
            chats TEXT NOT NULL DEFAULT '', last_message_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(week_start,user_id));
        CREATE TABLE IF NOT EXISTS global_activity_chats (
            chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', last_seen INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS global_weekly_awards (
            week_start TEXT NOT NULL, place INTEGER NOT NULL, user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '', points INTEGER NOT NULL DEFAULT 0,
            cash_prize TEXT NOT NULL DEFAULT '', nft_prize TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending_manual_payout', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(week_start,place), UNIQUE(week_start,user_id));
        CREATE TABLE IF NOT EXISTS community_settings (
            chat_id INTEGER NOT NULL, setting TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(chat_id, setting));
        CREATE TABLE IF NOT EXISTS community_notes (
            chat_id INTEGER NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
            updated_by INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(chat_id, name));
        CREATE TABLE IF NOT EXISTS community_filters (
            chat_id INTEGER NOT NULL, trigger TEXT NOT NULL, reply TEXT NOT NULL,
            updated_by INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id, trigger));
        CREATE TABLE IF NOT EXISTS community_user_profiles (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, nickname TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '', bio TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS community_moderator_roles (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rank INTEGER NOT NULL,
            permissions TEXT NOT NULL DEFAULT 'warn,mute,ban,unmute,call',
            assigned_by INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(chat_id,user_id));
        CREATE INDEX IF NOT EXISTS idx_global_activity_week_points
            ON global_weekly_activity(week_start, points DESC, messages DESC);
        CREATE INDEX IF NOT EXISTS idx_moderation_log_chat_created
            ON community_moderation_log(chat_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_community_messages_chat_count
            ON community_messages(chat_id, count DESC);
        """)
        # Upgrade databases created by earlier versions without resetting existing data.
        columns = {row[1] for row in c.execute("PRAGMA table_info(community_messages)").fetchall()}
        if "first_seen" not in columns:
            c.execute("ALTER TABLE community_messages ADD COLUMN first_seen TEXT NOT NULL DEFAULT ''")
    _INITIALIZED_PATHS.add(db_key)


def init_social_store():
    init_community_store()
    db_key = str(Path(DB_PATH).resolve())
    social_key = db_key + "::social"
    if social_key in _INITIALIZED_PATHS and Path(DB_PATH).exists():
        return
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS social_marriages (
            chat_id INTEGER NOT NULL, user_a INTEGER NOT NULL, name_a TEXT NOT NULL DEFAULT '',
            user_b INTEGER NOT NULL, name_b TEXT NOT NULL DEFAULT '', married_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(chat_id,user_a), UNIQUE(chat_id,user_b));
        CREATE TABLE IF NOT EXISTS social_family (
            chat_id INTEGER NOT NULL, parent_id INTEGER NOT NULL, parent_name TEXT NOT NULL DEFAULT '',
            child_id INTEGER NOT NULL, child_name TEXT NOT NULL DEFAULT '', relation TEXT NOT NULL DEFAULT 'ребёнок',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(chat_id,parent_id,child_id));
        CREATE TABLE IF NOT EXISTS social_scratch (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, last_play INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id,user_id));
        CREATE INDEX IF NOT EXISTS idx_social_family_child ON social_family(chat_id, child_id);
        """)
    _INITIALIZED_PATHS.add(social_key)


def scratch_card(chat_id, user_id, now=None):
    import time
    now = int(time.time() if now is None else now)
    init_social_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT last_play FROM social_scratch WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        if row and now - int(row[0]) < 300:
            return None, 300 - (now - int(row[0]))
        c.execute("INSERT INTO social_scratch(chat_id,user_id,last_play) VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET last_play=excluded.last_play", (chat_id,user_id,now))
        # Community coins only; intentionally separate from the real-money casino wallet.
        roll = random.randint(1, 100)
        reward = 0 if roll <= 18 else (3 if roll <= 52 else 7 if roll <= 80 else 15 if roll <= 96 else 40)
        _ensure(c, chat_id, user_id, '')
        c.execute("UPDATE community_wallets SET coins=coins+?,xp=xp+? WHERE chat_id=? AND user_id=?", (reward, 2 if reward else 0, chat_id,user_id))
        balance = c.execute("SELECT coins FROM community_wallets WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()[0]
        return {"reward": reward, "balance": int(balance), "roll": roll}, 0


def marry(chat_id, user_a, name_a, user_b, name_b):
    init_social_store()
    if user_a == user_b: return False, "Нельзя заключить брак с самим собой."
    a,b=sorted((int(user_a),int(user_b)))
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT user_a,user_b FROM social_marriages WHERE chat_id=? AND (user_a IN (?,?) OR user_b IN (?,?))",(chat_id,a,b,a,b)).fetchone()
        if row: return False, "Один из вас уже состоит в браке в этой группе. Сначала используйте «развод» с партнёром в ответ на его сообщение."
        names={int(user_a):name_a,int(user_b):name_b}
        c.execute("INSERT INTO social_marriages(chat_id,user_a,name_a,user_b,name_b) VALUES(?,?,?,?,?)",(chat_id,a,names[a],b,names[b]))
    return True, f"💍 {name_a} и {name_b} теперь семья! Поздравляем молодожёнов."


def divorce(chat_id, user_a, user_b):
    init_social_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT user_a,user_b FROM social_marriages WHERE chat_id=? AND ((user_a=? AND user_b=?) OR (user_a=? AND user_b=?))",(chat_id,user_a,user_b,user_b,user_a)).fetchone()
        if not row: return False
        c.execute("DELETE FROM social_marriages WHERE chat_id=? AND user_a=?",(chat_id,row["user_a"]))
        return True


def social_status(chat_id, user_id):
    init_social_store()
    with connect() as c:
        spouse=c.execute("SELECT user_a,name_a,user_b,name_b,married_at FROM social_marriages WHERE chat_id=? AND (user_a=? OR user_b=?)",(chat_id,user_id,user_id)).fetchone()
        kids=c.execute("SELECT child_id,child_name,relation FROM social_family WHERE chat_id=? AND parent_id=?",(chat_id,user_id)).fetchall()
        parents=c.execute("SELECT parent_id,parent_name,relation FROM social_family WHERE chat_id=? AND child_id=?",(chat_id,user_id)).fetchall()
    partner=None
    if spouse: partner={"id":int(spouse["user_b"] if int(spouse["user_a"])==user_id else spouse["user_a"]),"name":spouse["name_b"] if int(spouse["user_a"])==user_id else spouse["name_a"],"since":spouse["married_at"]}
    return {"partner":partner,"children":[dict(x) for x in kids],"parents":[dict(x) for x in parents]}


def add_family_member(chat_id, parent_id, parent_name, child_id, child_name, relation='ребёнок'):
    init_social_store()
    if parent_id == child_id: return False, "Нельзя добавить себя в семью как собственного ребёнка."
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT 1 FROM social_family WHERE chat_id=? AND child_id=? AND parent_id=?",(chat_id,child_id,parent_id)).fetchone()
        if row: return False, "Этот участник уже записан в вашу семью."
        c.execute("INSERT INTO social_family(chat_id,parent_id,parent_name,child_id,child_name,relation) VALUES(?,?,?,?,?,?)",(chat_id,parent_id,parent_name,child_id,child_name,relation))
    return True, f"👨‍👩‍👧 {parent_name} добавил(а) {child_name} в семью: {relation}."


def world_event(chat_id, now=None):
    """One persistent random event per chat, rotated every 24 hours."""
    import time
    now = int(now if now is not None else time.time())
    init_community_store()
    with connect() as c:
        row = c.execute("SELECT event_key,starts_at,ends_at FROM nova_world_events WHERE chat_id=?", (chat_id,)).fetchone()
        if row and int(row["ends_at"]) > now:
            event = next((e for e in WORLD_EVENTS if e["key"] == row["event_key"]), WORLD_EVENTS[0])
            return {**event, "starts_at": int(row["starts_at"]), "ends_at": int(row["ends_at"])}
        previous = row["event_key"] if row else ""
        choices = [e for e in WORLD_EVENTS if e["key"] != previous] or WORLD_EVENTS
        event = random.choice(choices)
        c.execute("INSERT INTO nova_world_events(chat_id,event_key,starts_at,ends_at) VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET event_key=excluded.event_key,starts_at=excluded.starts_at,ends_at=excluded.ends_at", (chat_id,event["key"],now,now+86400))
        return {**event, "starts_at": now, "ends_at": now+86400}


def world_search(chat_id, user_id, username='', now=None):
    import time
    now = int(now if now is not None else time.time())
    event = world_event(chat_id, now)
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _ensure(c, chat_id, user_id, username)
        row = c.execute("SELECT MAX(last_claim) AS last_claim FROM nova_world_claims WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        last_claim = int(row["last_claim"] or 0) if row else 0
        remaining = 2 * 3600 - (now - last_claim) if last_claim else 0
        if remaining > 0:
            return None, remaining, event
        weights = [card["weight"] for card in WORLD_CARDS.values()]
        # Event modifiers now match their descriptions instead of being cosmetic.
        keys = list(WORLD_CARDS)
        if event["key"] == "охота_за_легендой":
            weights[-1] = 4
        elif event["key"] in {"ночь_неона", "охота_за_стикерами"}:
            weights = [max(1, w // 2) if i < 2 else w * (2 if i in (2, 3, 4) else 1) for i, w in enumerate(weights)]
        elif event["key"] == "мусорный_рейд":
            weights = [w * 2 if i < 2 else max(1, w // 2) for i, w in enumerate(weights)]
        card_key = random.choices(keys, weights=weights, k=1)[0]
        coins = random.randint(2, 6) + event["bonus"]
        c.execute("INSERT INTO nova_cards(chat_id,user_id,card_key,quantity) VALUES(?,?,?,1) ON CONFLICT(chat_id,user_id,card_key) DO UPDATE SET quantity=quantity+1", (chat_id,user_id,card_key))
        c.execute("UPDATE community_wallets SET coins=coins+?,xp=xp+12 WHERE chat_id=? AND user_id=?", (coins,chat_id,user_id))
        c.execute("INSERT INTO nova_world_claims(chat_id,user_id,event_key,last_claim) VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id,event_key) DO UPDATE SET last_claim=excluded.last_claim", (chat_id,user_id,event["key"],now))
        return {**WORLD_CARDS[card_key], "key": card_key, "coins": coins}, 0, event


def card_album(chat_id, user_id):
    init_community_store()
    with connect() as c:
        rows = c.execute("SELECT card_key,quantity FROM nova_cards WHERE chat_id=? AND user_id=? AND quantity>0", (chat_id,user_id)).fetchall()
        owned = {r["card_key"]: int(r["quantity"]) for r in rows}
        return [{**card, "key": key, "quantity": owned.get(key, 0)} for key,card in WORLD_CARDS.items()]


def open_card_pack(chat_id, user_id, username=''):
    """Buy a three-card pack for community coins, atomically."""
    cost = 35
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _ensure(c, chat_id, user_id, username)
        row = c.execute("SELECT coins FROM community_wallets WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        if int(row[0]) < cost:
            return False, f"Пак стоит {cost} коинов, у тебя {row[0]}."
        c.execute("UPDATE community_wallets SET coins=coins-? WHERE chat_id=? AND user_id=?", (cost,chat_id,user_id))
        found=[]
        for _ in range(3):
            key=random.choices(list(WORLD_CARDS), weights=[x["weight"] for x in WORLD_CARDS.values()], k=1)[0]
            c.execute("INSERT INTO nova_cards(chat_id,user_id,card_key,quantity) VALUES(?,?,?,1) ON CONFLICT(chat_id,user_id,card_key) DO UPDATE SET quantity=quantity+1", (chat_id,user_id,key))
            found.append(WORLD_CARDS[key])
        return True, {"cost":cost,"cards":found}


def sell_card(chat_id, user_id, card_key, quantity=1):
    card_key = card_key.casefold().strip()
    if card_key not in WORLD_CARDS or quantity < 1 or quantity > 100:
        return False, "Неизвестная карта или количество. Используй ключ из альбома, например продать карту дворовый_кот 1."
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT quantity FROM nova_cards WHERE chat_id=? AND user_id=? AND card_key=?", (chat_id,user_id,card_key)).fetchone()
        if not row or int(row[0]) < quantity:
            return False, "У тебя недостаточно таких карт."
        c.execute("UPDATE nova_cards SET quantity=quantity-? WHERE chat_id=? AND user_id=? AND card_key=?", (quantity,chat_id,user_id,card_key))
        earned = WORLD_CARDS[card_key]["value"] * quantity
        c.execute("UPDATE community_wallets SET coins=coins+? WHERE chat_id=? AND user_id=?", (earned,chat_id,user_id))
        return True, earned



def _ensure(c, chat_id, user_id, username=''):
    c.execute("INSERT INTO community_wallets(chat_id,user_id,username) VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET username=CASE WHEN excluded.username='' THEN community_wallets.username ELSE excluded.username END", (chat_id, user_id, username or ''))


def wallet(chat_id, user_id, username=''):
    init_community_store()
    with connect() as c:
        _ensure(c, chat_id, user_id, username)
        row = c.execute("SELECT * FROM community_wallets WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        return dict(row)


def _week_start(now=None):
    from datetime import datetime, timezone
    import time
    stamp = int(now if now is not None else time.time())
    dt = datetime.fromtimestamp(stamp, timezone.utc)
    monday = dt.date().isoformat() if dt.weekday() == 0 else (dt.date()).isoformat()
    from datetime import timedelta
    return (dt.date() - timedelta(days=dt.weekday())).isoformat()


def add_message(chat_id, user_id, username='', chat_title='', now=None):
    """Track chat-local economy plus one global weekly activity score across ALL groups."""
    import time
    now = int(now if now is not None else time.time())
    week = _week_start(now)
    init_community_store()
    with connect() as c:
        _ensure(c, chat_id, user_id, username)
        c.execute("INSERT INTO community_messages(chat_id,user_id,count) VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1,last_seen=CURRENT_TIMESTAMP,first_seen=CASE WHEN first_seen='' THEN CURRENT_TIMESTAMP ELSE first_seen END", (chat_id,user_id))
        c.execute("INSERT INTO community_activity_events(chat_id,user_id,username,created_at) VALUES(?,?,?,?)", (chat_id, user_id, username or '', now))
        c.execute("INSERT INTO global_activity_chats(chat_id,title,last_seen) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=CASE WHEN excluded.title='' THEN global_activity_chats.title ELSE excluded.title END,last_seen=excluded.last_seen", (chat_id, chat_title or '', now))
        row = c.execute("SELECT count FROM community_messages WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        if row and row[0] % 10 == 0:
            c.execute("UPDATE community_wallets SET coins=coins+1,xp=xp+2 WHERE chat_id=? AND user_id=?", (chat_id,user_id))
        activity = c.execute("SELECT chats,last_message_at FROM global_weekly_activity WHERE week_start=? AND user_id=?", (week,user_id)).fetchone()
        chats = set((activity['chats'] if activity else '').split(','))
        chats.discard('')
        chats.add(str(chat_id))
        # Every real message counts for the leaderboard; points are lightly throttled to
        # one per user per 15 seconds to make flood-spam less profitable.
        points = 1 if not activity or now - int(activity['last_message_at']) >= 15 else 0
        c.execute("INSERT INTO global_weekly_activity(week_start,user_id,username,messages,points,chats,last_message_at) VALUES(?,?,?,1,?,?,?) ON CONFLICT(week_start,user_id) DO UPDATE SET username=CASE WHEN excluded.username='' THEN global_weekly_activity.username ELSE excluded.username END,messages=messages+1,points=points+excluded.points,chats=excluded.chats,last_message_at=CASE WHEN excluded.points>0 THEN excluded.last_message_at ELSE global_weekly_activity.last_message_at END", (week,user_id,username or '',points,','.join(sorted(chats)),now))


def global_weekly_leaderboard(limit=10, now=None):
    week = _week_start(now)
    init_community_store()
    with connect() as c:
        rows = c.execute("SELECT user_id,username,messages,points,chats FROM global_weekly_activity WHERE week_start=? ORDER BY points DESC,messages DESC,user_id ASC LIMIT ?", (week, max(1, min(int(limit), 100)))).fetchall()
        return [dict(r) for r in rows]


def tracked_group_ids():
    init_community_store()
    with connect() as c:
        return [int(r[0]) for r in c.execute("SELECT chat_id FROM global_activity_chats").fetchall()]


def finalize_weekly_awards(now=None, cash_prizes=None, nft_prizes=None):
    """Freeze top 3 globally for the completed UTC week; prize fulfillment is manual."""
    from datetime import datetime, timezone, timedelta
    import time
    stamp = int(now if now is not None else time.time())
    current_start = _week_start(stamp)
    completed_week = (datetime.fromisoformat(current_start).date() - timedelta(days=7)).isoformat()
    cash_prizes = cash_prizes or ('50 USD', '25 USD', '10 USD')
    nft_prizes = nft_prizes or ('NFT-подарок #1', 'NFT-подарок #2', 'NFT-подарок #3')
    init_community_store()
    with connect() as c:
        existing = c.execute("SELECT COUNT(*) FROM global_weekly_awards WHERE week_start=?", (completed_week,)).fetchone()[0]
        if existing:
            return completed_week, [dict(r) for r in c.execute("SELECT * FROM global_weekly_awards WHERE week_start=? ORDER BY place", (completed_week,)).fetchall()], False
        winners = c.execute("SELECT user_id,username,messages,points FROM global_weekly_activity WHERE week_start=? ORDER BY points DESC,messages DESC,user_id ASC LIMIT 3", (completed_week,)).fetchall()
        result=[]
        for place, winner in enumerate(winners, 1):
            cash = str(cash_prizes[place-1]) if len(cash_prizes) >= place else ''
            nft = str(nft_prizes[place-1]) if len(nft_prizes) >= place else ''
            c.execute("INSERT INTO global_weekly_awards(week_start,place,user_id,username,points,cash_prize,nft_prize) VALUES(?,?,?,?,?,?,?)", (completed_week,place,int(winner['user_id']),winner['username'],int(winner['points']),cash,nft))
            result.append(dict(week_start=completed_week,place=place,user_id=int(winner['user_id']),username=winner['username'],points=int(winner['points']),cash_prize=cash,nft_prize=nft,status='pending_manual_payout'))
        return completed_week, result, True


def weekly_awards(week_start=None):
    init_community_store()
    if week_start is None:
        from datetime import datetime, timezone, timedelta
        week_start = (datetime.now(timezone.utc).date() - timedelta(days=datetime.now(timezone.utc).weekday()+7)).isoformat()
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM global_weekly_awards WHERE week_start=? ORDER BY place", (week_start,)).fetchall()]


def collect(chat_id, user_id, username='', now=None):
    import time
    now = int(now if now is not None else time.time())
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _ensure(c, chat_id, user_id, username)
        row = c.execute("SELECT last_collect FROM community_wallets WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        remaining = 4 * 3600 - (now - int(row[0]))
        if remaining > 0:
            return None, remaining
        # Weighted resource table: commons frequent, valuable drops rare.
        item = random.choices(["дерево", "камень", "рыба", "кристалл", "корона"], weights=[35,30,25,9,1], k=1)[0]
        coins = random.randint(2, 7)
        c.execute("UPDATE community_wallets SET coins=coins+?,xp=xp+10,last_collect=? WHERE chat_id=? AND user_id=?", (coins,now,chat_id,user_id))
        c.execute("INSERT INTO community_inventory(chat_id,user_id,item,quantity) VALUES(?,?,?,1) ON CONFLICT(chat_id,user_id,item) DO UPDATE SET quantity=quantity+1", (chat_id,user_id,item))
        return {"item": item, "coins": coins, **ITEMS[item]}, 0


def inventory(chat_id, user_id):
    init_community_store()
    with connect() as c:
        rows = c.execute("SELECT item,quantity FROM community_inventory WHERE chat_id=? AND user_id=? AND quantity>0 ORDER BY item", (chat_id,user_id)).fetchall()
        return [(r["item"], r["quantity"]) for r in rows]


def sell_item(chat_id, user_id, item, quantity=1):
    item = item.casefold().strip()
    if item not in ITEMS or quantity < 1 or quantity > 1000:
        return False, "Неизвестный предмет или количество."
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT quantity FROM community_inventory WHERE chat_id=? AND user_id=? AND item=?", (chat_id,user_id,item)).fetchone()
        if not row or row[0] < quantity:
            return False, "У тебя недостаточно этого предмета в инвентаре."
        c.execute("UPDATE community_inventory SET quantity=quantity-? WHERE chat_id=? AND user_id=? AND item=?", (quantity,chat_id,user_id,item))
        earned = ITEMS[item]["sell"] * quantity
        c.execute("UPDATE community_wallets SET coins=coins+?,xp=xp+? WHERE chat_id=? AND user_id=?", (earned,quantity,chat_id,user_id))
        return True, earned


def buy_item(chat_id, user_id, item, quantity=1, username=''):
    item = item.casefold().strip()
    if item not in ITEMS or quantity < 1 or quantity > 100:
        return False, "Неизвестный предмет или количество."
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _ensure(c,chat_id,user_id,username)
        cost = ITEMS[item]["buy"] * quantity
        row = c.execute("SELECT coins FROM community_wallets WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        if row[0] < cost:
            return False, f"Не хватает коинов: нужно {cost}, у тебя {row[0]}."
        c.execute("UPDATE community_wallets SET coins=coins-? WHERE chat_id=? AND user_id=?", (cost,chat_id,user_id))
        c.execute("INSERT INTO community_inventory(chat_id,user_id,item,quantity) VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id,item) DO UPDATE SET quantity=quantity+excluded.quantity", (chat_id,user_id,item,quantity))
        return True, cost


def daily(chat_id, user_id, username=''):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _ensure(c,chat_id,user_id,username)
        row = c.execute("SELECT last_daily FROM community_wallets WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        if row[0] == today:
            return False, 0
        reward = random.randint(15, 25)
        c.execute("UPDATE community_wallets SET coins=coins+?,xp=xp+5,last_daily=? WHERE chat_id=? AND user_id=?", (reward,today,chat_id,user_id))
        return True, reward


def leaderboard(chat_id, limit=10):
    init_community_store()
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT user_id,username,coins,xp FROM community_wallets WHERE chat_id=? ORDER BY coins DESC,xp DESC LIMIT ?", (chat_id,limit)).fetchall()]


def activity_leaderboard(chat_id, period="week", limit=10, now=None):
    """Rank chat members by messages during a real UTC rolling period.

    Historical rankings are based on events recorded after this feature was installed;
    old messages are not fabricated or backfilled.
    """
    import time
    now = int(time.time() if now is None else now)
    span = {"week": 7 * 86400, "month": 30 * 86400}.get(str(period).casefold())
    if span is None:
        raise ValueError("period must be 'week' or 'month'")
    init_community_store()
    with connect() as c:
        rows = c.execute(
            "SELECT user_id, MAX(username) AS username, COUNT(*) AS messages "
            "FROM community_activity_events WHERE chat_id=? AND created_at>=? AND created_at<=? "
            "GROUP BY user_id ORDER BY messages DESC, user_id ASC LIMIT ?",
            (chat_id, now - span, now, max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(r) for r in rows]


def add_warning(chat_id, user_id, moderator_id, reason):
    init_community_store()
    with connect() as c:
        c.execute("INSERT INTO community_warnings(chat_id,user_id,moderator_id,reason) VALUES(?,?,?,?)", (chat_id,user_id,moderator_id,reason[:300]))
        count = c.execute("SELECT COUNT(*) FROM community_warnings WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()[0]
        c.execute("INSERT INTO community_moderation_log(chat_id,moderator_id,target_id,action,reason) VALUES(?,?,?,?,?)", (chat_id,moderator_id,user_id,"warn",reason[:300]))
        return count


def log_moderation(chat_id, moderator_id, target_id, action, reason=''):
    init_community_store()
    with connect() as c:
        c.execute("INSERT INTO community_moderation_log(chat_id,moderator_id,target_id,action,reason) VALUES(?,?,?,?,?)", (chat_id,moderator_id,target_id,action,reason[:300]))


def clear_warnings(chat_id, user_id, moderator_id):
    """Clear a member's warnings and preserve an audit record."""
    init_community_store()
    with connect() as c:
        count = c.execute("SELECT COUNT(*) FROM community_warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()[0]
        c.execute("DELETE FROM community_warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        c.execute("INSERT INTO community_moderation_log(chat_id,moderator_id,target_id,action,reason) VALUES(?,?,?,?,?)", (chat_id, moderator_id, user_id, "clear_warnings", f"Удалено предупреждений: {count}"))
        return count


def user_warnings(chat_id, user_id, limit=10):
    init_community_store()
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT moderator_id,reason,created_at FROM community_warnings WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?", (chat_id,user_id,max(1,min(int(limit),50)))).fetchall()]


def moderation_log(chat_id, limit=10):
    init_community_store()
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT moderator_id,target_id,action,reason,created_at FROM community_moderation_log WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id,max(1,min(int(limit),50)))).fetchall()]


def stats(chat_id):
    init_community_store()
    with connect() as c:
        users = c.execute("SELECT COUNT(*) FROM community_wallets WHERE chat_id=?", (chat_id,)).fetchone()[0]
        messages = c.execute("SELECT COALESCE(SUM(count),0) FROM community_messages WHERE chat_id=?", (chat_id,)).fetchone()[0]
        warnings = c.execute("SELECT COUNT(*) FROM community_warnings WHERE chat_id=?", (chat_id,)).fetchone()[0]
        return users, messages, warnings


def change_coins(chat_id, user_id, delta, username=''):
    """Atomically adjust community coins; negative deltas never overdraw a wallet."""
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _ensure(c, chat_id, user_id, username)
        row = c.execute("SELECT coins FROM community_wallets WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        current = int(row[0])
        if delta < 0 and current < -delta:
            return False, current
        c.execute("UPDATE community_wallets SET coins=coins+?, xp=xp+? WHERE chat_id=? AND user_id=?", (delta, max(0, abs(delta)//2), chat_id, user_id))
        return True, current + delta



# Free community-helper features: per-chat settings, notes, and keyword replies.
def set_chat_setting(chat_id, setting, value):
    init_community_store()
    with connect() as c:
        c.execute("INSERT INTO community_settings(chat_id,setting,value) VALUES(?,?,?) ON CONFLICT(chat_id,setting) DO UPDATE SET value=excluded.value", (chat_id, setting, str(value)))


def get_chat_setting(chat_id, setting, default=''):
    init_community_store()
    with connect() as c:
        row = c.execute("SELECT value FROM community_settings WHERE chat_id=? AND setting=?", (chat_id, setting)).fetchone()
        return str(row[0]) if row else default


def set_chat_note(chat_id, name, body, updated_by=0):
    name = name.casefold().strip().replace(' ', '_')[:40]
    if not name or not body.strip() or len(body) > 3500:
        return False
    init_community_store()
    with connect() as c:
        c.execute("INSERT INTO community_notes(chat_id,name,body,updated_by) VALUES(?,?,?,?) ON CONFLICT(chat_id,name) DO UPDATE SET body=excluded.body,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP", (chat_id,name,body.strip(),updated_by))
    return True


def get_chat_note(chat_id, name):
    name = name.casefold().strip().replace(' ', '_')[:40]
    init_community_store()
    with connect() as c:
        row = c.execute("SELECT body FROM community_notes WHERE chat_id=? AND name=?", (chat_id,name)).fetchone()
        return str(row[0]) if row else None


def list_chat_notes(chat_id):
    init_community_store()
    with connect() as c:
        return [str(r[0]) for r in c.execute("SELECT name FROM community_notes WHERE chat_id=? ORDER BY name", (chat_id,)).fetchall()]


def set_chat_filter(chat_id, trigger, reply, updated_by=0):
    trigger = trigger.casefold().strip()[:80]
    reply = reply.strip()
    if len(trigger) < 2 or not reply or len(reply) > 1500:
        return False
    init_community_store()
    with connect() as c:
        c.execute("INSERT INTO community_filters(chat_id,trigger,reply,updated_by) VALUES(?,?,?,?) ON CONFLICT(chat_id,trigger) DO UPDATE SET reply=excluded.reply,updated_by=excluded.updated_by", (chat_id,trigger,reply,updated_by))
    return True


def delete_chat_filter(chat_id, trigger):
    init_community_store()
    with connect() as c:
        cur = c.execute("DELETE FROM community_filters WHERE chat_id=? AND trigger=?", (chat_id,trigger.casefold().strip()))
        return cur.rowcount > 0


def list_chat_filters(chat_id):
    init_community_store()
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT trigger,reply FROM community_filters WHERE chat_id=? ORDER BY trigger", (chat_id,)).fetchall()]


def match_chat_filter(chat_id, text):
    folded = text.casefold()
    for item in list_chat_filters(chat_id):
        if item['trigger'] in folded:
            return item['reply']
    return None


# Free per-user profile customization and portable chat configuration.
def set_user_profile(chat_id, user_id, field, value):
    if field not in {"nickname", "title", "bio"}:
        return False
    limits = {"nickname": 32, "title": 48, "bio": 240}
    value = str(value).strip()[:limits[field]]
    init_community_store()
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO community_user_profiles(chat_id,user_id) VALUES(?,?)", (chat_id,user_id))
        c.execute(f"UPDATE community_user_profiles SET {field}=? WHERE chat_id=? AND user_id=?", (value,chat_id,user_id))
    return True


def get_user_profile(chat_id, user_id):
    init_community_store()
    with connect() as c:
        row = c.execute("SELECT nickname,title,bio FROM community_user_profiles WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
    return dict(row) if row else {"nickname":"", "title":"", "bio":""}


def export_chat_settings(chat_id):
    init_community_store()
    with connect() as c:
        settings = {r[0]: r[1] for r in c.execute("SELECT setting,value FROM community_settings WHERE chat_id=?", (chat_id,))}
        notes = [dict(r) for r in c.execute("SELECT name,body,updated_by FROM community_notes WHERE chat_id=?", (chat_id,))]
        filters = [dict(r) for r in c.execute("SELECT trigger,reply,updated_by FROM community_filters WHERE chat_id=?", (chat_id,))]
    return {"version":1,"settings":settings,"notes":notes,"filters":filters}


def import_chat_settings(chat_id, payload):
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return False
    settings = payload.get("settings", {})
    notes = payload.get("notes", [])
    filters = payload.get("filters", [])
    if not isinstance(settings, dict) or not isinstance(notes, list) or not isinstance(filters, list):
        return False
    init_community_store()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for key, value in settings.items():
            if isinstance(key,str) and len(key) <= 80 and isinstance(value,(str,int,float,bool)):
                c.execute("INSERT INTO community_settings(chat_id,setting,value) VALUES(?,?,?) ON CONFLICT(chat_id,setting) DO UPDATE SET value=excluded.value", (chat_id,key,str(value)[:4000]))
        for item in notes[:200]:
            if isinstance(item,dict) and item.get("name") and item.get("body"):
                c.execute("INSERT INTO community_notes(chat_id,name,body,updated_by) VALUES(?,?,?,?) ON CONFLICT(chat_id,name) DO UPDATE SET body=excluded.body,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP", (chat_id,str(item['name'])[:40],str(item['body'])[:3500],int(item.get('updated_by',0))))
        for item in filters[:200]:
            if isinstance(item,dict) and item.get("trigger") and item.get("reply"):
                c.execute("INSERT INTO community_filters(chat_id,trigger,reply,updated_by) VALUES(?,?,?,?) ON CONFLICT(chat_id,trigger) DO UPDATE SET reply=excluded.reply,updated_by=excluded.updated_by", (chat_id,str(item['trigger'])[:100],str(item['reply'])[:1000],int(item.get('updated_by',0))))
    return True


def get_community_profile(chat_id, user_id):
    """Return community-only progression stats; never reads casino balances."""
    init_community_store()
    with connect() as c:
        wallet = c.execute("SELECT coins,xp FROM community_wallets WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        messages = c.execute("SELECT count, first_seen FROM community_messages WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
    xp = int(wallet["xp"]) if wallet else 0
    # Progression gets steadily slower; level 1 starts at zero XP.
    level = 1
    while xp >= level * 100:
        xp -= level * 100
        level += 1
    total_xp = int(wallet["xp"]) if wallet else 0
    return {"coins": int(wallet["coins"]) if wallet else 0, "xp": total_xp,
            "level": level, "level_xp": xp, "next_level_xp": level * 100,
            "messages": int(messages["count"]) if messages else 0,
            "first_seen": str(messages["first_seen"]) if messages else ""}


def set_moderator_role(chat_id, user_id, rank, permissions, assigned_by):
    """Persist a per-chat moderator rank and explicit permission list."""
    with connect() as c:
        c.execute("""INSERT INTO community_moderator_roles(chat_id,user_id,rank,permissions,assigned_by,updated_at)
            VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(chat_id,user_id) DO UPDATE SET
            rank=excluded.rank, permissions=excluded.permissions, assigned_by=excluded.assigned_by,
            updated_at=CURRENT_TIMESTAMP""", (chat_id, user_id, int(rank), ','.join(sorted(set(permissions))), assigned_by))


def get_moderator_role(chat_id, user_id):
    with connect() as c:
        row = c.execute("SELECT rank,permissions,assigned_by FROM community_moderator_roles WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        if not row:
            return None
        return {"rank": int(row["rank"]), "permissions": set(filter(None, row["permissions"].split(','))), "assigned_by": int(row["assigned_by"])}


def remove_moderator_role(chat_id, user_id):
    with connect() as c:
        c.execute("DELETE FROM community_moderator_roles WHERE chat_id=? AND user_id=?", (chat_id,user_id))


def list_moderator_roles(chat_id):
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT user_id,rank,permissions,assigned_by FROM community_moderator_roles WHERE chat_id=? ORDER BY rank DESC,user_id", (chat_id,)).fetchall()]
