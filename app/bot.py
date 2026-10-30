import asyncio
import logging
import html
import json
import os
import uuid
import random
import secrets
import time
from datetime import datetime, timezone
from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path
from aiogram.exceptions import TelegramNetworkError
from aiogram.dispatcher.event.bases import SkipHandler

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BotCommand, FSInputFile, ChatPermissions, ChatMemberUpdated, LabeledPrice, PreCheckoutQuery, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings
from app import community_store as community
from app import group_call
from app import ai_memory
from app.profile_card import render_profile_card
from app.chat_rank_card import render_chat_rank_card
from app.chat_utils import _parse_duration_minutes
from app.payments.crypto_pay_client import CryptoPayClient
from app.payments.deposit_store import init_store, save_invoice, get_balance, get_deposit, get_pending_deposits, mark_paid_and_credit, update_deposit_status, record_game, get_profile_stats, BET_OPTIONS, place_bet, settle_bet, refund_bet, WITHDRAWAL_OPTIONS, reserve_withdrawal, finish_withdrawal, init_withdrawal_store, claim_task_reward

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
dp = Dispatcher()
pending_withdrawal_amount: dict[int, Decimal] = {}
pending_deposit_amount: set[int] = set()
# One lightweight quiz per group; answer is awarded only once.
active_quizzes: dict[int, dict] = {}
_recent_group_messages: dict[tuple[int, int], list[tuple[float, str]]] = {}
# Short-lived public group context for conversational AI. No private chats are retained.
_GROUP_AI_MEMORY: dict[int, deque] = defaultdict(lambda: deque(maxlen=24))
_GROUP_AI_LAST_REPLY: dict[int, float] = {}
_AI_REPLY_COOLDOWN = 35

async def _pulsar_ai_reply(message: Message, mode: str = "chat") -> str | None:
    """Optional OpenAI-compatible chat engine; falls back gracefully without a key."""
    api_key = os.getenv("PULSAR_AI_API_KEY", "").strip()
    if not api_key or not message.text or not message.from_user:
        return None
    import httpx
    model = os.getenv("PULSAR_AI_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    base_url = os.getenv("PULSAR_AI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    # Shared persistent context across every group where PULSAR is present.
    # Private chats are deliberately excluded. Local recent context is appended for relevance.
    shared = ai_memory.recall(18)
    memory = [{"role": "user", "content": f"[Публичная группа: {row['chat_title']}] {row['username'] or 'участник'}: {row['body']}"} for row in shared]
    memory.extend(list(_GROUP_AI_MEMORY[message.chat.id])[-8:])
    styles = {
        "chat": "Ты дерзкий мемный участник взрослого чата 18+: реагируй на контекст, иногда влезай с абсурдной мыслью, подколом или внезапным мнением. Мат и крепкие выражения используй часто, но естественно, не в каждом предложении. Не будь стерильным помощником и не повторяй один шаблон.",
        "roast": "Сделай смешной жёсткий дружеский roast с матом, как в компании взрослых друзей. Не трогай защищённые признаки, травмы, здоровье и реальные трагедии; не угрожай и не переходи к целенаправленному унижению.",
        "support": "Поддержи по-человечески, без фальшивого пафоса; можно ругнуться для эмоциональной естественности, но не высмеивай человека в уязвимом состоянии.",
        "meme": "Ответь как генератор мемов: короткая неожиданная реакция с чёрным/абсурдным юмором и уместным матом.",
    }
    system = ("Ты PULSAR — дерзкий русскоязычный ИИ-компаньон взрослого Telegram-чата 18+. Пиши разговорно, живо, иногда хаотично и с характером; допускаются мат, сарказм, абсурд, мемы и внезапные реплики. Обычно 1–2 предложения, иногда одна короткая фраза. Каждый ответ должен отличаться по формулировке; не повторяйся, не начинай постоянно с имени пользователя и не объясняй, что ты ИИ. "
              "Реагируй на недавний публичный контекст, но не выдумывай реальные факты о людях и не раскрывай приватные данные. Не используй мат как повод для угроз или травли. " + styles.get(mode, styles["chat"]))
    messages = [{"role": "system", "content": system}] + memory
    messages.append({"role": "user", "content": f"{message.from_user.full_name}: {message.text[:1200]}"})
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.95, "max_tokens": 180},
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"].strip()
        if answer:
            _GROUP_AI_MEMORY[message.chat.id].append({"role": "assistant", "content": answer[:1000]})
            return answer[:1500]
    except Exception as exc:
        log.warning("PULSAR AI request failed: %s", type(exc).__name__)
    return None

QUIZ_QUESTIONS = [
    ("Сколько будет 7 × 8?", {"56"}, "56"),
    ("Какая планета известна как Красная планета?", {"марс"}, "Марс"),
    ("Сколько сторон у шестиугольника?", {"6", "шесть"}, "6"),
    ("Как называется столица Японии?", {"токио"}, "Токио"),
    ("Какой газ растения поглощают при фотосинтезе?", {"углекислый газ", "co2", "углекислота"}, "Углекислый газ"),
    ("Сколько минут в двух часах?", {"120", "сто двадцать"}, "120"),
    ("Как называется спутник Земли?", {"луна"}, "Луна"),
]


# Original, local entertainment snippets inspired by chat-friendly bot formats.
JOKES = [
    "Мой план на сегодня был идеальным. Потом я проснулся и открыл Telegram.",
    "Почему программист не спорит с календарём? У него и так слишком много дедлайнов.",
    "Оптимист видит свет в конце тоннеля. Админ чата проверяет, кто опять выключил свет.",
    "У меня настолько космический уровень прокрастинации, что дела уже вышли на орбиту.",
    "Хотел стать легендой чата, но пока я только закреплённое сообщение.",
    "Мой внутренний будильник сработал. Жаль, что это был будильник соседа.",
]
PREDICTIONS = [
    "Сегодня звёзды советуют не отправлять сообщение, пока не перечитал его дважды.",
    "В ближайшие сутки тебя ждёт важный выбор: ответить сразу или написать «ща» и пропасть.",
    "Космос намекает: удача рядом. Проверь карманы и не забудь зарядку.",
    "Твой сегодняшний бафф — спокойствие. Дебафф — групповой чат на 247 непрочитанных.",
    "Скоро случится маленькая победа. Возможно, ты наконец найдёшь нужный стикер.",
]
MEME_CAPTIONS = [
    "Когда написал «я на минутку» и вернулся через три часа.",
    "Когда админ сказал «последнее предупреждение», но это уже пятое последнее.",
    "Когда выпал редкий предмет, а места в инвентаре нет.",
    "Когда бот работает идеально, пока разработчик не смотрит.",
    "Когда решил экономить коины, но увидел новый пак карт.",
]

# Telegram Premium custom emoji from the user's supplied message.
CUSTOM_EMOJI = {
    "smile": '<tg-emoji emoji-id="5451788978804001506">😄</tg-emoji>',
    "money": '<tg-emoji emoji-id="5451754391432366821">💰</tg-emoji>',
    "gem": '<tg-emoji emoji-id="5451985838630014131">💎</tg-emoji>',
    "cash": '<tg-emoji emoji-id="5452157517062770940">💸</tg-emoji>',
    "money_bag": '<tg-emoji emoji-id="5451845260055450038">💰</tg-emoji>',
    "banknote": '<tg-emoji emoji-id="5449772843845853012">💵</tg-emoji>',
    "greeting": '<tg-emoji emoji-id="5451912789826243272">😃</tg-emoji>',
    "games": '<tg-emoji emoji-id="5452033976623467883">🎮</tg-emoji>',
    "card": '<tg-emoji emoji-id="5449624985301717991">💳</tg-emoji>',
    "celebration": '<tg-emoji emoji-id="5449465422971711717">🎉</tg-emoji>',
    "bomb": '<tg-emoji emoji-id="5452018153963948977">💣</tg-emoji>',
    "castle": '<tg-emoji emoji-id="5449397725697187601">🏰</tg-emoji>',
    "back": '<tg-emoji emoji-id="5451689352742605373">◀️</tg-emoji>',
    "stop": '<tg-emoji emoji-id="5449502419819994491">⏹</tg-emoji>',
    "play": '<tg-emoji emoji-id="5449477951391305638">▶️</tg-emoji>',
    "skip": '<tg-emoji emoji-id="5449486352347339763">⏭️</tg-emoji>',
    "dice": '<tg-emoji emoji-id="5449654410122663850">🎲</tg-emoji>',
    "multiplier": '<tg-emoji emoji-id="5449472625631860042">✖️</tg-emoji>',
    "arrow": '<tg-emoji emoji-id="5449828850219386951">➡️</tg-emoji>',
    "stats": '<tg-emoji emoji-id="5452042536493288421">📊</tg-emoji>',
    "bowling": '<tg-emoji emoji-id="5451684138652306936">🎳</tg-emoji>',
    "slots": '<tg-emoji emoji-id="5451811677706167149">🎰</tg-emoji>',
    "wallet": '<tg-emoji emoji-id="5451790988848697409">👛</tg-emoji>',
    "rocket": '<tg-emoji emoji-id="5452124248246096392">🚀</tg-emoji>',
    "heart": '<tg-emoji emoji-id="5452145448204668230">❤️</tg-emoji>',
    "fire": '<tg-emoji emoji-id="5449527712882399620">🔥</tg-emoji>',
    "trend_up": '<tg-emoji emoji-id="5451778692357327880">📈</tg-emoji>',
    "gold": '<tg-emoji emoji-id="5451916157080604249">🥇</tg-emoji>',
    "silver": '<tg-emoji emoji-id="5452114627519352575">🥈</tg-emoji>',
    "bronze": '<tg-emoji emoji-id="5449450661169106391">🥉</tg-emoji>',
    "trend_down": '<tg-emoji emoji-id="5451776987255313063">📉</tg-emoji>',
    "basketball": '<tg-emoji emoji-id="5296290950572707978">🏀</tg-emoji>',
    "timer": '<tg-emoji emoji-id="5296460644730574615">⏳</tg-emoji>',
    "announcement": '<tg-emoji emoji-id="5300901232892747080">📢</tg-emoji>',
    "swords": '<tg-emoji emoji-id="5303306457528176082">⚔️</tg-emoji>',
    "note": '<tg-emoji emoji-id="5296410453742753454">📝</tg-emoji>',
    "crown": '<tg-emoji emoji-id="5312462735097764089">👑</tg-emoji>',
    "shield": '<tg-emoji emoji-id="5312281637801729565">🛡️</tg-emoji>',
    "pause_play": '<tg-emoji emoji-id="5452035905063786856">⏯️</tg-emoji>',
    "six": '<tg-emoji emoji-id="5451789399710798534">6️⃣</tg-emoji>',
    "gun": '<tg-emoji emoji-id="5449706211723221696">🔫</tg-emoji>',
    "gift": '<tg-emoji emoji-id="5452032666658444888">🎁</tg-emoji>',
    "joker": '<tg-emoji emoji-id="5451826078731503931">🃏</tg-emoji>',
    "football": '<tg-emoji emoji-id="5449880441366548359">⚽</tg-emoji>',
    "down": '<tg-emoji emoji-id="5451807640436903198">🔽</tg-emoji>',
    "sad": '<tg-emoji emoji-id="5452147973645439296">☹️</tg-emoji>',
    "gem_alt": '<tg-emoji emoji-id="5452031425412895584">💎</tg-emoji>',
    "two": '<tg-emoji emoji-id="5296332478611493390">2️⃣</tg-emoji>',
    "three": '<tg-emoji emoji-id="5296337654047086802">3️⃣</tg-emoji>',
    "four": '<tg-emoji emoji-id="5296741956498528812">4️⃣</tg-emoji>',
    "five": '<tg-emoji emoji-id="5296442640227668224">5️⃣</tg-emoji>',
    "six_alt": '<tg-emoji emoji-id="5296497830557425173">6️⃣</tg-emoji>',
}


# Premium emoji IDs are sourced from assets/emoji_catalog.json, not duplicated
# as an independent catalogue. This keeps bot messages and chat menus consistent.
def _load_custom_emoji_catalog() -> dict:
    catalog_path = Path(__file__).resolve().parent.parent / "assets" / "emoji_catalog.json"
    try:
        return json.loads(catalog_path.read_text(encoding="utf-8")).get("custom_emoji", {})
    except (OSError, json.JSONDecodeError):
        log.warning("Unable to load custom emoji catalog: %s", catalog_path)
        return {}

_EMOJI_CATALOG = _load_custom_emoji_catalog()


def _premiumize_html(value: str) -> tuple[str, bool]:
    """Replace catalogued Unicode emoji with Telegram Premium emoji tags outside HTML tags."""
    import re
    if not isinstance(value, str) or not _EMOJI_CATALOG:
        return value, False
    # Prefer the canonical catalog ID for a glyph; suffixed entries are alternate designs.
    glyph_ids = {}
    for glyph, emoji_id in _EMOJI_CATALOG.items():
        if "_alt" in glyph or "_bag" in glyph:
            continue
        glyph_ids.setdefault(glyph, str(emoji_id))
    if not glyph_ids:
        return value, False
    pattern = re.compile("|".join(re.escape(g) for g in sorted(glyph_ids, key=len, reverse=True)))
    pieces = re.split(r"(<tg-emoji\b.*?</tg-emoji>|<[^>]+>)", value, flags=re.DOTALL)
    changed = False
    for i in range(0, len(pieces), 2):
        def replace(match):
            nonlocal changed
            glyph = match.group(0)
            emoji_id = glyph_ids.get(glyph)
            if not emoji_id:
                return glyph
            changed = True
            return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'
        pieces[i] = pattern.sub(replace, pieces[i])
    return "".join(pieces), changed


def _install_premium_emoji_sender():
    """Apply catalogued Telegram Premium emoji to supported outgoing text/captions.

    Inline-keyboard labels and media-group InputMedia objects are deliberately left
    alone: Telegram does not consistently support HTML custom-emoji entities there.
    """
    import functools

    def wrap_text_method(method_name: str, field_name: str, positional_index: int | None = None):
        original = getattr(Bot, method_name, None)
        if original is None or getattr(original, "_pulsar_premium_wrapped", False):
            return

        @functools.wraps(original)
        async def wrapped(self, *args, **kwargs):
            value = kwargs.get(field_name)
            args_list = list(args)
            if value is None and positional_index is not None and len(args_list) > positional_index:
                value = args_list[positional_index]
            # Don't mix HTML tags into explicitly Markdown-formatted messages.
            parse_mode = kwargs.get("parse_mode")
            if parse_mode is None or str(parse_mode).upper() in {"HTML", "PARSEMODE.HTML"}:
                if isinstance(value, str):
                    converted, changed = _premiumize_html(value)
                    if changed:
                        if field_name in kwargs:
                            kwargs[field_name] = converted
                        elif positional_index is not None and len(args_list) > positional_index:
                            args_list[positional_index] = converted
                        else:
                            kwargs[field_name] = converted
                        if parse_mode is None:
                            kwargs["parse_mode"] = "HTML"
            return await original(self, *args_list, **kwargs)

        wrapped._pulsar_premium_wrapped = True
        setattr(Bot, method_name, wrapped)

    # Text-only responses, edits and the common media caption methods. In aiogram
    # method argument order, text/caption follows the chat/media arguments noted here.
    wrap_text_method("send_message", "text", 1)
    wrap_text_method("edit_message_text", "text", 0)
    wrap_text_method("edit_message_caption", "caption", 3)
    for _method in ("send_photo", "send_video", "send_animation", "send_document",
                    "send_audio", "send_voice"):
        wrap_text_method(_method, "caption", 2)



_install_premium_emoji_sender()
_EMOJI_GLYPHS = {
    "smile": "😄", "money": "💰", "gem": "💎", "cash": "💸",
    "money_bag": "💰_bag", "banknote": "💵", "greeting": "😃", "games": "🎮",
    "card": "💳", "celebration": "🎉", "bomb": "💣", "castle": "🏰",
    "back": "◀️", "stop": "⏹️", "play": "▶️", "skip": "⏭️", "dice": "🎲",
    "multiplier": "✖️", "arrow": "➡️", "stats": "📊", "bowling": "🎳",
    "slots": "🎰", "wallet": "👛", "rocket": "🚀", "heart": "❤️", "fire": "🔥",
    "trend_up": "📈", "gold": "🥇", "silver": "🥈", "bronze": "🥉",
    "trend_down": "📉", "basketball": "🏀", "timer": "⏳", "announcement": "📢",
    "swords": "⚔️", "note": "📝", "crown": "👑", "shield": "🛡️",
    "pause_play": "⏯️", "six": "6️⃣", "gun": "🔫", "gift": "🎁", "joker": "🃏",
    "football": "⚽", "down": "🔽", "sad": "☹️", "gem_alt": "💎_alt",
    "two": "2️⃣", "three": "3️⃣", "four": "4️⃣", "five": "5️⃣", "six_alt": "6️⃣_alt",
    "eyes": "👀", "slight_smile": "🙂", "lightning": "⚡️", "comet": "☄️", "shopping": "🛍", "no_entry": "⛔️", "prohibited": "🚫", "exclamation": "❗️",
    "double_exclamation": "‼️", "interrobang": "⁉️", "question": "❓", "warning": "⚠️", "warning_alt": "⚠️_alt", "globe": "🌐", "chat": "💬", "thought": "💭",
    "question_alt": "❓_alt", "chart_alt": "📊_alt", "up": "🔼", "down_alt": "🔽_alt", "candle": "🕯", "trend_up_alt": "📈_alt", "trend_down_alt": "📉_alt", "check": "✔️",
    "cross": "❌", "sos": "🆘", "bell": "🔔", "ninja": "🥷", "party": "🥳", "foot": "🦶", "pin": "📌", "banknote_alt": "💵_alt",
    "cash_alt1": "💸_alt1", "cash_alt2": "💸_alt2", "cash_alt3": "💸_alt3", "cash_alt4": "💸_alt4", "cash_alt5": "💸_alt5", "pound": "💷", "play_alt": "▶️_alt", "red_circle": "🔴",
    "green_circle": "🟢", "arrow_alt": "➡️_alt", "fire_alt": "🔥_alt", "collision": "💥", "studio_microphone": "🎙", "microphone": "🎤", "megaphone": "📣", "shush": "🤫",
    "thumb_down": "👎", "speaking": "🗣️", "search": "🔍", "shield_alt": "🛡️_alt", "link": "🔗", "computer": "🖥", "copyright": "©", "info": "ℹ️",
    "thumb_up": "👍", "pause": "⏸", "hundred": "💯", "refresh": "🔄", "top": "🔝", "new": "🆕", "soon": "🔜",
}
for _name, _glyph in _EMOJI_GLYPHS.items():
    _emoji_id = _EMOJI_CATALOG.get(_glyph)
    if _emoji_id:
        _current = CUSTOM_EMOJI.get(_name, "")
        _visible = _glyph.split("_alt", 1)[0].split("_alt1", 1)[0].split("_alt2", 1)[0].split("_alt3", 1)[0].split("_alt4", 1)[0].split("_alt5", 1)[0] if _glyph.startswith(("💸_", "💵_", "📊_", "🔽_", "📈_", "📉_", "❓_", "🛡️_", "▶️_", "➡️_", "🔥_", "⚠️_")) else (_current.split(">", 1)[-1].split("<", 1)[0] if _current else _glyph.replace("_bag", ""))
        CUSTOM_EMOJI[_name] = f'<tg-emoji emoji-id="{_emoji_id}">{_visible}</tg-emoji>'

WELCOME = (
    f'{CUSTOM_EMOJI["comet"]} <b>PULSAR — игры и помощник сообществ</b>\n\n'
    f'{CUSTOM_EMOJI["games"]} Казино: кубик, футбол, дартс, боулинг, рулетка, блэкджек и другие игры. Профиль и история — в меню.\n'
    f'{CUSTOM_EMOJI["card"]} Мир: карты, альбом, паки, рынок и события. Коины чата не являются деньгами и не выводятся.\n'
    f'{CUSTOM_EMOJI["shield"]} Группы: модерация, правила, заметки, антифлуд, мини-игры, рейтинг и живые реакции PULSAR на разговор.\n'
    f'{CUSTOM_EMOJI["crown"]} <b>Еженедельный конкурс сообществ</b>\n'
    'Участие: от 301 участника и реальная активность. Оцениваются активность, функции PULSAR, игровые события и стабильность. Спам и накрутка запрещены.\n'
    f'{CUSTOM_EMOJI["gold"]} $50 + NFT   {CUSTOM_EMOJI["silver"]} $25   {CUSTOM_EMOJI["bronze"]} $10\n'
    'Период: понедельник–воскресенье (UTC). Победителей проверяет организатор, призы выдаются вручную.\n\n'
    f'{CUSTOM_EMOJI["down"]} <b>Выбери раздел кнопками ниже</b>'
)

GAME_INFO = {
    "dice": {"emoji": "🎲", "name": "Кубик", "command": "/dice", "desc": "Испытай удачу одним броском."},
    "football": {"emoji": "⚽", "name": "Футбол", "command": "/football", "desc": "Пробей пенальти и узнай результат!"},
    "basketball": {"emoji": "🏀", "name": "Баскетбол", "command": "/basketball", "desc": "Попади в кольцо — пусть бросок будет точным!"},
    "darts": {"emoji": "🎯", "name": "Дартс", "command": "/darts", "desc": "Прицелься и проверь меткость."},
    "bowling": {"emoji": "🎳", "name": "Боулинг", "command": "/bowling", "desc": "Сбей как можно больше кеглей!"},
    "billiards": {"emoji": "🎱", "name": "Бильярд", "command": "/billiards", "desc": "Точный удар: цель — забить шар в лузу."},
    "roulette": {"emoji": "🎡", "name": "Рулетка", "command": "/roulette", "desc": "Красное/чёрное: угадай цвет сектора."},
    "blackjack": {"emoji": "🃏", "name": "Блэкджек", "command": "/blackjack", "desc": "Симуляция раздачи против дилера."},
    "baccarat": {"emoji": "♠️", "name": "Баккара", "command": "/baccarat", "desc": "Сравни очки игрока и банкира."},
    "crash": {"emoji": "🚀", "name": "Crash", "command": "/crash", "desc": "Множитель раунда генерируется до старта; цель — забрать выплату до краша."},
    "mines": {"emoji": "💣", "name": "Мины", "command": "/mines", "desc": "Один рискованный выбор: безопасная клетка или мина."},
}
GAME_EMOJIS = {key: val["emoji"] for key, val in GAME_INFO.items()}


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 Играть", callback_data="games")
    kb.button(text="💰 Кошелёк", callback_data="wallet")
    kb.button(text="🏆 Мой профиль", callback_data="profile")
    kb.button(text="📜 История", callback_data="history")
    kb.button(text="🎁 Бонусы", callback_data="bonuses")
    kb.button(text="🌍 Мир и коллекция", callback_data="community_hub")
    kb.button(text="💜 Поддержать бота", callback_data="donate")
    kb.button(text="📖 Правила", callback_data="rules")
    kb.button(text="🆘 Поддержка", callback_data="support")
    kb.button(text="👥 Играть с друзьями", callback_data="group_info")
    kb.button(text="➕ Добавить PULSAR в группу", url="https://t.me/pullsargame?startgroup=true")
    kb.adjust(1, 2, 2, 2, 1, 1, 1, 1)
    return kb.as_markup()


def community_menu():
    """Private-chat hub for social features, not only the casino."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🗺️ Мир и событие", callback_data="world_event")
    kb.button(text="🔎 Собрать карту", callback_data="world_search")
    kb.button(text="🗂️ Моя коллекция", callback_data="card_album")
    kb.button(text="🎴 Открыть пак · 35 коинов", callback_data="card_pack")
    kb.button(text="🏪 Рынок карт", callback_data="card_market")
    kb.button(text="🏆 Глобальный топ недели", callback_data="global_top")
    kb.button(text="👥 Помощник группы", callback_data="group_info")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(1, 2, 2, 1, 1, 1)
    return kb.as_markup()


def _community_wallet_text(chat_id: int, user_id: int, username: str = "") -> str:
    wallet = community.wallet(chat_id, user_id, username)
    album = community.card_album(chat_id, user_id)
    owned = sum(int(card["quantity"]) for card in album)
    kinds = sum(1 for card in album if int(card["quantity"]) > 0)
    return (f"{CUSTOM_EMOJI['money']} <b>Баланс сообщества:</b> {wallet['coins']} коинов\n"
            f"{CUSTOM_EMOJI['gem']} <b>Опыт:</b> {wallet['xp']} XP\n"
            f"{CUSTOM_EMOJI['card']} <b>Карты:</b> {owned} шт. · {kinds}/{len(album)} видов")


def _album_text(chat_id: int, user_id: int) -> str:
    cards = community.card_album(chat_id, user_id)
    lines = [f"{c['emoji']} <b>{html.escape(c['name'])}</b> — ×{c['quantity']} · {html.escape(c['rarity'])} · {c['value']} коинов" for c in cards]
    count = sum(int(c['quantity']) for c in cards)
    return f"{CUSTOM_EMOJI['card']} <b>КОЛЛЕКЦИЯ PULSAR</b>\n\n" + "\n".join(lines) + f"\n\nВсего: <b>{count}</b> / {len(cards)} видов\nПродажа: <code>продать карту дворовый_кот 1</code>."


async def send_community_game_photo(message: Message, caption: str, reply_markup=None):
    """Attach the matching PULSAR collection artwork to collection-game messages."""
    art = Path(__file__).resolve().parent.parent / "assets" / "community_cards_interface.png"
    if art.is_file():
        await message.answer_photo(FSInputFile(art), caption=caption, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=reply_markup)


FEATURE_CARD_FILES = {
    "welcome": "welcome.png", "profile": "profile.png", "profile_detail": "profile_detail.png",
    "games": "games.png", "wallet": "wallet.png", "deposit": "deposit.png", "withdraw": "withdraw.png",
    "top_chat": "top_chat.png", "top_week": "top_week.png", "top_month": "top_month.png",
    "top_all": "top_all.png", "leaderboard": "leaderboard.png", "community": "community.png",
    "commands": "commands.png", "moderation": "moderation.png", "rules": "rules.png",
    "support": "support.png", "statistics": "statistics.png", "history": "history.png",
    "cards": "cards.png", "collections": "collections.png", "inventory": "inventory.png",
    "play_with_friends": "play_with_friends.png", "transfer": "transfer.png", "game_modes": "game_modes.png",
    "bonuses": "bonuses.png", "achievements": "achievements.png", "social": "social.png",
    "marriage": "marriage.png", "daily_bonus": "daily_bonus.png", "help": "help.png",
}

async def send_feature_card(message: Message, feature: str) -> bool:
    """Send the semantically matching PULSAR artwork as a separate photo message."""
    return await send_feature_card_for_chat(message.bot, message.chat.id, feature)


async def answer_with_feature_card(message: Message, feature: str, text: str, reply_markup=None, parse_mode: str | None = "HTML") -> bool:
    """Attach the matching themed image to a private-chat reply; fall back to text."""
    if message.chat.type != "private":
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return False
    sent = await send_feature_card_for_chat(message.bot, message.chat.id, feature, caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
    if not sent:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    return sent

async def send_feature_card_for_chat(bot, chat_id: int, feature: str, caption: str | None = None,
                                     reply_markup=None, parse_mode: str | None = "HTML") -> bool:
    """Send feature artwork; when text is supplied, attach it as the photo caption."""
    filename = FEATURE_CARD_FILES.get(feature)
    if not filename:
        return False
    art = Path(__file__).resolve().parent.parent / "assets" / "chat_cards" / filename
    if not art.is_file():
        log.warning("Feature card missing: %s", art)
        return False
    try:
        kwargs = {}
        if caption is not None:
            kwargs["caption"] = caption
            kwargs["reply_markup"] = reply_markup
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
        await bot.send_photo(chat_id, FSInputFile(art), **kwargs)
        return True
    except Exception:
        log.exception("Could not send feature card %s", feature)
        return False


async def send_live_chat_rank_card(message: Message, *, title: str, rows: list[dict],
                                  metric_label: str, metric_key: str, period_label: str = "ВСЁ ВРЕМЯ",
                                  xp_key: str | None = "xp") -> bool:
    """Send a group ranking card with live values and an accent matching the caller's rank."""
    if not message.from_user or message.chat.type not in {"group", "supergroup"}:
        return False
    normalized = []
    for row in rows:
        item = dict(row)
        item["value"] = int(item.get(metric_key, item.get("value", 0)) or 0)
        normalized.append(item)
    viewer_id = int(message.from_user.id)
    viewer_rank = next((i for i, row in enumerate(normalized, 1) if int(row.get("user_id", 0)) == viewer_id), None)
    viewer_row = next((row for row in normalized if int(row.get("user_id", 0)) == viewer_id), {})
    # If the member is outside the visible top list, query the complete coin ranking when possible.
    if viewer_rank is None and metric_key == "coins":
        try:
            with community.connect() as db:
                all_rows = db.execute(
                    "SELECT user_id, username, coins, xp FROM community_wallets WHERE chat_id=? ORDER BY coins DESC, xp DESC, user_id ASC",
                    (message.chat.id,),
                ).fetchall()
            viewer_rank = next((i for i, row in enumerate(all_rows, 1) if int(row["user_id"]) == viewer_id), None)
            viewer_row = next((dict(row) for row in all_rows if int(row["user_id"]) == viewer_id), viewer_row)
        except Exception:
            log.exception("Could not calculate live coin rank")
    elif viewer_rank is None and metric_key == "messages":
        try:
            with community.connect() as db:
                rows_all = db.execute(
                    "SELECT user_id, MAX(username) AS username, COUNT(*) AS messages FROM community_activity_events WHERE chat_id=? GROUP BY user_id ORDER BY messages DESC, user_id ASC",
                    (message.chat.id,),
                ).fetchall()
            viewer_rank = next((i for i, row in enumerate(rows_all, 1) if int(row["user_id"]) == viewer_id), None)
            viewer_row = next((dict(row) for row in rows_all if int(row["user_id"]) == viewer_id), viewer_row)
        except Exception:
            log.exception("Could not calculate live activity rank")
    viewer_value = int(viewer_row.get(metric_key, 0) or 0)
    viewer_xp = int(viewer_row.get(xp_key, 0) or 0) if xp_key else 0
    try:
        path = render_chat_rank_card(
            chat_title=message.chat.title or "PULLSAR", ranking_title=title,
            ranked_rows=normalized[:3], viewer_name=message.from_user.full_name,
            viewer_rank=viewer_rank, viewer_value=viewer_value, metric_label=metric_label,
            viewer_xp=viewer_xp, period_label=period_label,
        )
        caption_lines = [f"🏆 <b>{html.escape(title)}</b>"]
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(normalized[:10], 1):
            name = html.escape(str(row.get("username") or f"Участник {row.get('user_id', '?')}"))
            val = int(row.get("value", 0) or 0)
            label = medals[i-1] if i <= 3 else f"{i}."
            caption_lines.append(f"{label} {name} — <b>{val:,}</b>".replace(",", " "))
        if viewer_rank is None:
            caption_lines.append("\nТвоё место появится после первых сообщений или получения коинов.")
        else:
            caption_lines.append(f"\nТвоё место: <b>#{viewer_rank}</b> · {metric_label.lower()}: <b>{viewer_value:,}</b>".replace(",", " "))
        caption = "\n".join(caption_lines)
        await message.reply_photo(FSInputFile(path), caption=caption[:1000], parse_mode="HTML")
        return True
    except Exception:
        log.exception("Could not render/send live ranking card")
        return False
    finally:
        try:
            if 'path' in locals() and os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


def donate_menu():
    kb = InlineKeyboardBuilder()
    for amount, label in ((25, "25 ⭐"), (50, "50 ⭐"), (100, "100 ⭐"), (250, "250 ⭐")):
        kb.button(text=label, callback_data=f"donate_{amount}")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def games_menu():
    kb = InlineKeyboardBuilder()
    for key, info in GAME_INFO.items():
        kb.button(text=f"{info['emoji']} {info['name']}", callback_data=f"play_{key}")
    kb.button(text="🎉 Ивент казино дня", callback_data="casino_event")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(2, 2, 2, 2, 2, 2, 1)
    return kb.as_markup()


DEPOSIT_OPTIONS = {
    "050": Decimal("0.50"),
    "100": Decimal("1.00"),
    "500": Decimal("5.00"),
    "1000": Decimal("10.00"),
    "2500": Decimal("25.00"),
    "5000": Decimal("50.00"),
}


def wallet_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ $0.50", callback_data="topup_050")
    kb.button(text="➕ $1", callback_data="topup_100")
    kb.button(text="➕ $5", callback_data="topup_500")
    kb.button(text="➕ $10", callback_data="topup_1000")
    kb.button(text="➕ $25", callback_data="topup_2500")
    kb.button(text="➕ $50", callback_data="topup_5000")
    kb.button(text="💸 Вывести средства", callback_data="withdraw")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def invoice_menu(invoice_id: str):
    # Automatic polling handles payment confirmation; no manual check button.
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Кошелёк", callback_data="wallet")
    kb.adjust(1)
    return kb.as_markup()


def withdrawal_menu():
    kb = InlineKeyboardBuilder()
    for amount in WITHDRAWAL_OPTIONS:
        cents = int(amount * 100)
        kb.button(text=f"Вывести ${amount:.2f} в USDT", callback_data=f"withdraw_amount_{cents}")
    kb.button(text="Отмена", callback_data="wallet")
    kb.adjust(1)
    return kb.as_markup()


def payment_client() -> CryptoPayClient:
    return CryptoPayClient(
        token=settings.crypto_pay_api_token,
        base_url=settings.crypto_pay_api_base_url,
        enabled=settings.payments_enabled,
    )


BONUS_CHAT_ID = -1004371502806
BONUS_CHAT_URL = "https://t.me/+8wE2ouxyV6VhMTk1"


def bonus_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Перейти в чат", url=BONUS_CHAT_URL)
    kb.button(text="✅ Проверить подписку", callback_data="bonus_check_chat")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def back_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 Выбрать игру", callback_data="games")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(1, 1)
    return kb.as_markup()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


async def send_welcome(message: Message):
    """Send the dedicated detailed PULLSAR welcome card with its menu attached."""
    sent = await send_feature_card_for_chat(
        message.bot, message.chat.id, "welcome", caption=WELCOME,
        reply_markup=main_menu(), parse_mode="HTML",
    )
    if not sent:
        await message.answer(WELCOME, reply_markup=main_menu(), parse_mode="HTML")


@dp.my_chat_member()
async def bot_added_or_promoted(event: ChatMemberUpdated):
    """Send a setup guide when the bot becomes an administrator in a group."""
    chat = event.chat
    if chat.type not in {"group", "supergroup"}:
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if new_status != "administrator" or old_status == "administrator":
        return
    title = html.escape(chat.title or "этот чат")
    await send_feature_card_for_chat(event.bot, chat.id, "moderation")
    guide = (
        f'{CUSTOM_EMOJI["party"]} <b>PULSAR подключён к чату «{title}»!</b>\n'
        '\n'
        f'{CUSTOM_EMOJI["eyes"]} <b>Я — помощник сообщества.</b> Могу помогать с модерацией, статистикой, экономикой, коллекционированием и играми.\n\n'
        f'{CUSTOM_EMOJI["shield"]} <b>1. Настрой права</b>\n'
        'Для обычных ответов достаточно права отправлять сообщения. Для мута и бана нужны права ограничивать участников; для удаления сообщений — право удалять сообщения. Выдавай только необходимые разрешения.\n\n'
        f'{CUSTOM_EMOJI["chat"]} <b>2. Модерация обычным текстом</b>\n'
        'Администратор отвечает на сообщение участника и пишет одно слово:\n'
        '• бан — заблокировать; разбан — снять блокировку\n'
        '• мут 10 причина — ограничить сообщения на 10 минут\n'
        '• размут — снять ограничения\n'
        '• варн причина — предупреждение; 3 предупреждения запускают автобан\n'
        '• статистика чата — сводка активности (администраторам)\n\n'
        f'{CUSTOM_EMOJI["shopping"]} <b>3. Экономика и коллекция</b>\n'
        'Пиши обычными сообщениями: «собрать», «инвентарь», «магазин», «купить дерево 2», «продать рыба 3», «коины», «ежедневный бонус», «топ чата». Валюта чата отдельная от баланса казино.\n\n'
        f'{CUSTOM_EMOJI["games"]} <b>4. Игры и личный кабинет</b>\n'
        'В личке открой меню и выбери «Профиль» или «Играть». В группе пользуйся игровым меню и доступными текстовыми функциями; отдельная игра за коины — «скретч». Социальные действия выполняются ответом на сообщение: «жениться», «развод», «поцелуй», «обнять», «сюрприз», «усыновить». «семья» показывает семейный статус. Валюта чата не связана с игровым балансом.\n\n'
        f'{CUSTOM_EMOJI["warning"]} <b>5. Важно</b>\n'
        '• Не выдавай боту права на управление администраторами — они не нужны.\n'
        '• Проверь модерацию в тестовой группе.\n'
        '• Не отправляй в чат BOT_TOKEN, платёжные токены, пароли и приватные ключи.\n'
        '• Для получения событий о других участниках и расширенной автомодерации могут потребоваться дополнительные настройки Telegram и отдельная конфигурация.\n\n'
        f'{CUSTOM_EMOJI["check"]} <b>Быстрый старт:</b> напиши «экономика чата» для списка игровых функций. Настройте правила чата и закрепите это сообщение при необходимости.\n\n'
        f'{CUSTOM_EMOJI["heart"]} Спасибо, что выбрали нашего бота!'
    )
    try:
        await event.bot.send_message(chat.id, guide, parse_mode="HTML", disable_web_page_preview=True)
        guide_file = Path(__file__).resolve().parent.parent / "docs" / "PULSAR_GROUP_GUIDE.md"
        if guide_file.is_file():
            await event.bot.send_document(
                chat.id,
                FSInputFile(guide_file, filename="PULSAR_Инструкция_для_группы.md"),
                caption=f"{CUSTOM_EMOJI['note']} Полная инструкция PULSAR для администраторов и модераторов. Сохрани файл или закрепи его в чате.",
            )
    except Exception:
        log.exception("Could not send onboarding guide to chat %s", chat.id)


DONATION_AMOUNTS = {25, 50, 100, 250}


def parse_donation_payload(payload: str, payer_id: int) -> int | None:
    """Validate invoice payload against the payer and the allowed fixed prices."""
    try:
        prefix, owner, amount = payload.split(":", 2)
        parsed_amount = int(amount)
        if prefix != "nova_donation" or int(owner) != int(payer_id) or parsed_amount not in DONATION_AMOUNTS:
            return None
        return parsed_amount
    except (ValueError, AttributeError):
        return None


@dp.pre_checkout_query()
async def donation_pre_checkout(query: PreCheckoutQuery):
    # Telegram Stars invoices must use XTR and fixed, validated prices.
    amount = parse_donation_payload(query.invoice_payload or "", query.from_user.id)
    if amount is None or query.currency != "XTR" or query.total_amount != amount:
        await query.answer(ok=False, error_message="Счёт недействителен. Открой меню поддержки и создай новый.")
        return
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def donation_success(message: Message):
    payment = message.successful_payment
    if not payment:
        return
    amount = parse_donation_payload(payment.invoice_payload or "", message.from_user.id)
    if amount is None or payment.currency != "XTR" or payment.total_amount != amount:
        log.warning("Rejected mismatched Stars payment from user %s", message.from_user.id)
        await message.answer("Платёж получен, но данные счёта не совпали. Напиши в поддержку и сохрани Telegram charge ID.")
        return
    # Telegram sends successful_payment once, but a unique charge ledger also makes
    # duplicate update delivery safe and leaves an auditable donation record.
    from app.payments.deposit_store import record_stars_donation
    first_receipt = record_stars_donation(
        telegram_id=message.from_user.id,
        charge_id=payment.telegram_payment_charge_id,
        amount=amount,
        payload=payment.invoice_payload,
    )
    if not first_receipt:
        await message.answer("Этот платёж уже учтён. Спасибо за поддержку!", reply_markup=back_menu())
        return
    await message.answer(
        f"{CUSTOM_EMOJI['heart']} <b>Спасибо за поддержку бота!</b>\n\n"
        f"Получено: <b>{amount} ⭐ Telegram Stars</b>.\n"
        "Твой вклад помогает развивать игры и сообщество. Донат добровольный, "
        "не пополняет игровой баланс и не влияет на шансы выигрыша.",
        reply_markup=back_menu(), parse_mode="HTML")


@dp.message(CommandStart())
async def start(message: Message):
    await send_welcome(message)


@dp.message(Command("help"))
async def help_cmd(message: Message):
    text = (
        f"{CUSTOM_EMOJI['eyes']} <b>PULSAR — ПОМОЩЬ</b>\n"
        "🎮 Играть — выбери игру и ставку.\n"
        "👤 Профиль — баланс, депозит, уровень и ID.\n"
        "💰 Кошелёк — пополнение и вывод.\n"
        "🌍 Мир — карточки, события и рейтинг.\n"
        "😂 В чатах PULSAR реагирует на контекст автоматически.\n"
        "🛡️ Модерация: ответь на сообщение и напиши «мут 10 причина», «бан причина» или «варн причина».\n"
        "✨ Не спамь и не публикуй личные данные. Баланс казино и коины сообщества — разные вещи."
    )
    sent = await send_feature_card_for_chat(message.bot, message.chat.id, "help", caption=text, reply_markup=main_menu(), parse_mode="HTML")
    if not sent:
        await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")

@dp.message(Command("profile"))
async def profile_command(message: Message):
    if message.from_user:
        text = profile_text(message.from_user.id, message.from_user.full_name)
        sent = await send_feature_card_for_chat(message.bot, message.chat.id, "profile", caption=text, reply_markup=back_menu(), parse_mode="HTML")
        if not sent:
            await message.answer(text, reply_markup=back_menu(), parse_mode="HTML")


CHAT_PROFILE_WORDS = {"профиль", "мой профиль", "profile", "мой аккаунт", "аккаунт", "мой профайл"}
CHAT_GAME_WORDS = {"играть", "игра", "игры", "игру", "начать игру", "games", "play"}
CHAT_DEPOSIT_WORDS = {"пополнить", "пополнение", "пополнить баланс", "депозит", "деп", "deposit", "top up"}
CHAT_WITHDRAW_WORDS = {"вывести", "вывод", "вывести средства", "снять", "withdraw", "cash out"}
CHAT_WALLET_WORDS = {"кошелек", "кошелёк", "баланс", "wallet", "б"}


async def _chat_admin(message: Message) -> bool:
    if not message.from_user or message.chat.type not in {"group", "supergroup"}:
        return False
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in {"administrator", "creator"}
    except Exception:
        log.exception("Could not verify group admin status")
        return False


async def _target_from_reply(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    return None


async def community_warn(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        return await message.answer("Эта команда работает в группе.")
    if not await _chat_admin(message):
        return await message.answer("⛔ Команда доступна администраторам чата.")
    target = await _target_from_reply(message)
    if not target:
        return await message.answer("Ответь на сообщение участника: /warn причина")
    reason = (message.text or "").partition(" ")[2].strip() or "Причина не указана"
    count = community.add_warning(message.chat.id, target.id, message.from_user.id, reason)
    await message.answer(f"⚠️ Участник {html.escape(target.full_name)} получил предупреждение ({count}). Причина: {html.escape(reason)}")
    try:
        warn_limit = max(2, min(10, int(community.get_chat_setting(message.chat.id, "warn_limit", "3") or "3")))
    except (TypeError, ValueError):
        warn_limit = 3
    if count >= warn_limit:
        action = community.get_chat_setting(message.chat.id, "warn_action", "ban")
        try:
            if action == "mute":
                from datetime import datetime, timedelta, timezone
                minutes = max(1, min(10080, int(community.get_chat_setting(message.chat.id, "warn_mute_minutes", "60") or "60")))
                await message.bot.restrict_chat_member(message.chat.id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc)+timedelta(minutes=minutes))
                label = f"мут на {minutes} мин."
                log_action = "auto_mute_warn_limit"
            else:
                await message.bot.ban_chat_member(message.chat.id, target.id, revoke_messages=False)
                label = "бан"
                log_action = "auto_ban_warn_limit"
            community.log_moderation(message.chat.id, message.from_user.id, target.id, log_action, reason)
            await message.answer(f"🚫 Автоматическое наказание ({label}): накоплено {count} предупреждений из {warn_limit}.")
        except Exception:
            log.exception("Auto action after warnings failed")


@dp.message(Command("warnlimit"))
async def warn_limit_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы может менять наказания.")
    args = (message.text or "").split()
    if len(args) < 2:
        limit = community.get_chat_setting(message.chat.id, "warn_limit", "3")
        action = community.get_chat_setting(message.chat.id, "warn_action", "ban")
        minutes = community.get_chat_setting(message.chat.id, "warn_mute_minutes", "60")
        return await message.answer(f"Текущая настройка: {limit} предупреждения; действие {action}; мут {minutes} мин.\n/warnlimit 3 ban\n/warnlimit 4 mute 30")
    try:
        limit = max(2, min(10, int(args[1])))
    except ValueError:
        return await message.answer("Порог должен быть числом от 2 до 10.")
    action = args[2].casefold() if len(args) > 2 else "ban"
    if action not in {"ban", "mute"}:
        return await message.answer("Действие: ban или mute.")
    minutes = 60
    if action == "mute" and len(args) > 3:
        try:
            minutes = max(1, min(10080, int(args[3])))
        except ValueError:
            return await message.answer("Длительность мута — число минут от 1 до 10080.")
    community.set_chat_setting(message.chat.id, "warn_limit", str(limit))
    community.set_chat_setting(message.chat.id, "warn_action", action)
    community.set_chat_setting(message.chat.id, "warn_mute_minutes", str(minutes))
    await message.answer(f"✅ Настроено: {limit} предупреждения → {'бан' if action == 'ban' else f'мут на {minutes} мин.'}.")


async def community_mute(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        return await message.answer("Эта команда работает в группе.")
    if not await _chat_admin(message):
        return await message.answer("⛔ Команда доступна администраторам чата.")
    target = await _target_from_reply(message)
    if not target:
        return await message.answer("Ответь на сообщение участника: /mute [минуты] [причина]")
    args = (message.text or "").split(maxsplit=2)
    duration = 10
    reason = "Нарушение правил"
    if len(args) > 1:
        try:
            duration = max(1, min(int(args[1]), 10080))
            if len(args) > 2: reason = args[2]
        except ValueError:
            reason = " ".join(args[1:])
    from datetime import datetime, timedelta, timezone
    try:
        await message.bot.restrict_chat_member(message.chat.id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc)+timedelta(minutes=duration))
        community.log_moderation(message.chat.id, message.from_user.id, target.id, "mute", reason)
        await message.answer(f"🔇 {html.escape(target.full_name)} ограничен на {duration} мин. Причина: {html.escape(reason)}")
    except Exception:
        log.exception("Mute failed")
        await message.answer("Не удалось выдать мут. Проверь права администратора бота.")


async def community_ban(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        return await message.answer("Эта команда работает в группе.")
    if not await _chat_admin(message):
        return await message.answer("⛔ Команда доступна администраторам чата.")
    target = await _target_from_reply(message)
    if not target:
        return await message.answer("Ответь на сообщение участника: /ban причина")
    reason = (message.text or "").partition(" ")[2].strip() or "Причина не указана"
    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        community.log_moderation(message.chat.id, message.from_user.id, target.id, "ban", reason)
        await message.answer(f"🚫 {html.escape(target.full_name)} заблокирован. Причина: {html.escape(reason)}")
    except Exception:
        log.exception("Ban failed")
        await message.answer("Не удалось заблокировать участника. Проверь права бота.")


@dp.message(Command("unmute"))
async def community_unmute(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Команда доступна администраторам группы.")
    target = await _target_from_reply(message)
    if not target:
        return await message.answer("Ответь на сообщение участника: /unmute")
    try:
        from aiogram.types import ChatPermissions
        await message.bot.restrict_chat_member(message.chat.id, target.id, permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
        community.log_moderation(message.chat.id, message.from_user.id, target.id, "unmute")
        await message.answer(f"🔊 Ограничения сняты с {html.escape(target.full_name)}.")
    except Exception:
        log.exception("Unmute failed")
        await message.answer("Не удалось снять ограничения. Проверь права бота.")


@dp.message(Command("warns"))
async def community_warns_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Команда доступна администраторам группы.")
    target = await _target_from_reply(message)
    if not target:
        return await message.answer("Ответь на сообщение участника и напиши /warns.")
    rows = community.user_warnings(message.chat.id, target.id)
    if not rows:
        return await message.answer(f"✅ У {html.escape(target.full_name)} нет предупреждений.")
    lines = [f"{i}. {html.escape(row['reason'])} — {html.escape(row['created_at'])}" for i, row in enumerate(rows, 1)]
    await message.answer(f"⚠️ <b>Предупреждения: {html.escape(target.full_name)}</b>\n" + "\n".join(lines), parse_mode="HTML")


@dp.message(Command("clearwarns"))
async def community_clear_warns_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Команда доступна администраторам группы.")
    target = await _target_from_reply(message)
    if not target:
        return await message.answer("Ответь на сообщение участника и напиши /clearwarns.")
    removed = community.clear_warnings(message.chat.id, target.id, message.from_user.id)
    await message.answer(f"✅ Предупреждения {html.escape(target.full_name)} очищены. Удалено: {removed}.")


@dp.message(Command("modlog"))
async def community_modlog_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Команда доступна администраторам группы.")
    rows = community.moderation_log(message.chat.id, 10)
    if not rows:
        return await message.answer("Журнал модерации пока пуст.")
    lines = [f"• {html.escape(row['action'])}: ID <code>{row['target_id']}</code> — {html.escape(row['reason'] or 'без причины')} ({html.escape(row['created_at'])})" for row in rows]
    await message.answer("🛡️ <b>Последние действия модерации</b>\n" + "\n".join(lines), parse_mode="HTML")


@dp.message(Command("setrules"))
async def set_rules_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы может менять правила.")
    body = (message.text or "").partition(" ")[2].strip()
    if not body:
        return await message.answer("Использование: /setrules текст правил")
    community.set_chat_setting(message.chat.id, "rules", body[:3500])
    await message.answer("✅ Правила сохранены. Участники могут открыть их командой /rules.")


@dp.message(Command("rules"))
async def rules_command(message: Message):
    body = community.get_chat_setting(message.chat.id, "rules", "")
    await message.answer("📜 <b>Правила группы</b>\n\n" + html.escape(body) if body else "📜 Правила пока не настроены. Администратор может задать их командой /setrules текст.", parse_mode="HTML")


@dp.message(Command("setwelcome"))
async def set_welcome_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы может менять приветствие.")
    body = (message.text or "").partition(" ")[2].strip()
    if not body:
        return await message.answer("Использование: /setwelcome текст приветствия\nПеременные: {name}, {title}")
    community.set_chat_setting(message.chat.id, "welcome", body[:1000])
    community.set_chat_setting(message.chat.id, "welcome_enabled", "1")
    await message.answer("✅ Приветствие сохранено. Отключить: /welcomeoff")


@dp.message(Command("welcomeoff"))
async def welcome_off_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    community.set_chat_setting(message.chat.id, "welcome_enabled", "0")
    await message.answer("Приветствия отключены. Включить: /welcomeon")


@dp.message(Command("welcomeon"))
async def welcome_on_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    community.set_chat_setting(message.chat.id, "welcome_enabled", "1")
    await message.answer("Приветствия включены.")


@dp.message(Command("setnote"))
async def set_note_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы может сохранять заметки.")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not community.set_chat_note(message.chat.id, parts[1], parts[2], message.from_user.id):
        return await message.answer("Использование: /setnote название текст заметки")
    await message.answer(f"✅ Заметка <code>{html.escape(parts[1])}</code> сохранена. Вызов: /getnote {html.escape(parts[1])}", parse_mode="HTML")


@dp.message(Command("getnote"))
async def get_note_command(message: Message):
    name = (message.text or "").partition(" ")[2].strip()
    if not name:
        names = community.list_chat_notes(message.chat.id)
        return await message.answer("Заметки: " + (", ".join(names) if names else "пока нет"))
    body = community.get_chat_note(message.chat.id, name)
    await message.answer(f"📝 <b>{html.escape(name)}</b>\n\n{html.escape(body)}" if body else "Заметка не найдена.", parse_mode="HTML")


@dp.message(Command("setfilter"))
async def set_filter_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы может настраивать фильтры.")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not community.set_chat_filter(message.chat.id, parts[1], parts[2], message.from_user.id):
        return await message.answer("Использование: /setfilter слово ответ бота")
    await message.answer("✅ Фильтр сохранён.")


@dp.message(Command("delfilter"))
async def delete_filter_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    trigger = (message.text or "").partition(" ")[2].strip()
    if not trigger:
        return await message.answer("Использование: /delfilter слово")
    await message.answer("✅ Фильтр удалён." if community.delete_chat_filter(message.chat.id, trigger) else "Фильтр не найден.")


@dp.message(Command("filters"))
async def list_filters_command(message: Message):
    rows = community.list_chat_filters(message.chat.id)
    await message.answer("Фильтры: " + (", ".join(html.escape(r['trigger']) for r in rows) if rows else "не настроены"), parse_mode="HTML")


@dp.message(Command("antilink"))
async def antilink_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    value = (message.text or "").partition(" ")[2].strip().casefold()
    if value not in {"on", "off", "вкл", "выкл"}:
        return await message.answer("Использование: /antilink on или /antilink off")
    enabled = value in {"on", "вкл"}
    community.set_chat_setting(message.chat.id, "antilink", "1" if enabled else "0")
    await message.answer("🔗 Антиссылки включены." if enabled else "🔗 Антиссылки отключены.")


@dp.message(Command("antiflood"))
async def antiflood_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    value = (message.text or "").partition(" ")[2].strip().casefold()
    if value not in {"on", "off", "вкл", "выкл"}:
        return await message.answer("Использование: /antiflood on или /antiflood off")
    enabled = value in {"on", "вкл"}
    community.set_chat_setting(message.chat.id, "antiflood", "1" if enabled else "0")
    await message.answer("🚦 Антифлуд включён: 6 сообщений за 5 секунд вызовут ограничение." if enabled else "🚦 Антифлуд отключён.")


@dp.message(Command("anticaps"))
async def anticaps_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    value = (message.text or "").partition(" ")[2].strip().casefold()
    if value not in {"on", "off", "вкл", "выкл"}:
        return await message.answer("Использование: /anticaps on или /anticaps off")
    enabled = value in {"on", "вкл"}
    community.set_chat_setting(message.chat.id, "anticaps", "1" if enabled else "0")
    await message.answer("🔠 Анти-CAPS включён." if enabled else "🔠 Анти-CAPS отключён.")


@dp.message(Command("capslimit"))
async def capslimit_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    parts = (message.text or "").split()
    if len(parts) != 3:
        return await message.answer("Использование: /capslimit 75 12 — процент CAPS (50–100) и минимум букв (5–100).")
    try:
        percent, minimum = int(parts[1]), int(parts[2])
        if not 50 <= percent <= 100 or not 5 <= minimum <= 100:
            raise ValueError
    except ValueError:
        return await message.answer("Процент должен быть 50–100, минимум букв — 5–100.")
    community.set_chat_setting(message.chat.id, "anticaps_percent", str(percent))
    community.set_chat_setting(message.chat.id, "anticaps_min_letters", str(minimum))
    await message.answer(f"✅ Анти-CAPS: порог {percent}%, минимум {minimum} букв. Включи /anticaps on.")


@dp.message(Command("messagelimit"))
async def messagelimit_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    parts = (message.text or "").split()
    if len(parts) != 2:
        return await message.answer("Использование: /messagelimit 1000 (0 отключает; диапазон 0–4000).")
    try:
        limit = int(parts[1])
        if not 0 <= limit <= 4000:
            raise ValueError
    except ValueError:
        return await message.answer("Укажи число от 0 до 4000.")
    community.set_chat_setting(message.chat.id, "message_max_length", str(limit))
    await message.answer("✅ Ограничение длины: " + (f"{limit} символов." if limit else "отключено."))


@dp.message(Command("setwelcome"))
async def set_welcome_template(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    template = (message.text or "").partition(" ")[2].strip()
    if not template:
        return await message.answer("Укажи шаблон. Переменные: {name}, {title}. Пример: /setwelcome Привет, {name}! Добро пожаловать в {title}.")
    if len(template) > 800:
        return await message.answer("Шаблон должен быть не длиннее 800 символов.")
    community.set_chat_setting(message.chat.id, "welcome", template)
    community.set_chat_setting(message.chat.id, "welcome_enabled", "1")
    await message.answer("✅ Шаблон приветствия сохранён. Переменные: {name}, {title}.")


@dp.message(Command("setgoodbye"))
async def set_goodbye_template(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    template = (message.text or "").partition(" ")[2].strip()
    if not template:
        return await message.answer("Укажи шаблон прощания. Переменные: {name}, {title}. Пример: /setgoodbye Пока, {name}! Будем ждать снова.")
    if len(template) > 800:
        return await message.answer("Шаблон должен быть не длиннее 800 символов.")
    community.set_chat_setting(message.chat.id, "goodbye", template)
    community.set_chat_setting(message.chat.id, "goodbye_enabled", "1")
    await message.answer("✅ Шаблон прощания сохранён.")


@dp.message(F.sticker)
async def community_sticker_moderation(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not message.from_user:
        return
    if community.get_chat_setting(message.chat.id, "antistickers", "0") != "1":
        return
    if await _chat_admin(message):
        return
    try:
        await message.delete()
        community.log_moderation(message.chat.id, 0, message.from_user.id, "sticker_delete", "Стикеры запрещены")
    except Exception:
        log.exception("Could not remove sticker")


@dp.message(Command("antistickers"))
async def antistickers_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    value = (message.text or "").partition(" ")[2].strip().casefold()
    if value not in {"on", "off", "вкл", "выкл"}:
        return await message.answer("Использование: /antistickers on или /antistickers off")
    enabled = value in {"on", "вкл"}
    community.set_chat_setting(message.chat.id, "antistickers", "1" if enabled else "0")
    await message.answer("🧩 Антистикеры включены." if enabled else "🧩 Антистикеры отключены.")


@dp.message(Command("purge"))
async def purge_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы.")
    parts = (message.text or "").split()
    try:
        count = max(1, min(int(parts[1]) if len(parts) > 1 else 1, 100))
    except ValueError:
        return await message.answer("Использование: ответь на сообщение командой /purge 10")
    if not message.reply_to_message:
        return await message.answer("Ответь на сообщение, с которого начать удаление: /purge 10")
    start_id = message.reply_to_message.message_id
    deleted = 0
    for mid in range(start_id, min(start_id + count, message.message_id)):
        try:
            await message.bot.delete_message(message.chat.id, mid)
            deleted += 1
        except Exception:
            pass
    try:
        await message.bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    await message.answer(f"🧹 Удалено сообщений: {deleted}.")


@dp.message(Command("report"))
async def report_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not message.reply_to_message:
        return await message.answer("Ответь на сообщение нарушителя командой /report причина")
    reason = (message.text or "").partition(" ")[2].strip() or "Причина не указана"
    target = message.reply_to_message.from_user
    if not target:
        return await message.answer("Не удалось определить автора сообщения.")
    admins = await message.bot.get_chat_administrators(message.chat.id)
    text = f"🚨 <b>Жалоба в {html.escape(message.chat.title or 'группе')}</b>\nНа: {html.escape(target.full_name)} (ID <code>{target.id}</code>)\nОт: {html.escape(message.from_user.full_name)}\nПричина: {html.escape(reason)}"
    sent = 0
    for admin in admins:
        if admin.user and not admin.user.is_bot:
            try:
                await message.bot.send_message(admin.user.id, text, parse_mode="HTML")
                sent += 1
            except Exception:
                pass
    await message.answer("🚨 Жалоба передана администраторам, которым бот может написать в личку." if sent else "Не удалось доставить жалобу в личку администраторам; сообщи им вручную.")


@dp.chat_member()
async def greet_new_member(event: ChatMemberUpdated):
    if event.chat.type not in {"group", "supergroup"}:
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if old_status in {"member", "administrator", "creator", "restricted"} or new_status not in {"member", "administrator", "creator"}:
        return
    user = event.new_chat_member.user
    if user.is_bot or community.get_chat_setting(event.chat.id, "welcome_enabled", "1") == "0":
        return
    template = community.get_chat_setting(event.chat.id, "welcome", "👋 Добро пожаловать, {name}, в {title}! Напиши /rules, чтобы прочитать правила.")
    rendered = template.replace("{name}", html.escape(user.full_name)).replace("{title}", html.escape(event.chat.title or "наш чат"))
    try:
        await event.bot.send_message(event.chat.id, rendered[:1000], parse_mode="HTML")
    except Exception:
        log.exception("Could not greet new member")


@dp.chat_member()
async def greet_departing_member(event: ChatMemberUpdated):
    if event.chat.type not in {"group", "supergroup"}:
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if old_status not in {"member", "administrator", "creator", "restricted"} or new_status not in {"left", "kicked", "banned"}:
        return
    user = event.old_chat_member.user
    if user.is_bot or community.get_chat_setting(event.chat.id, "goodbye_enabled", "0") != "1":
        return
    template = community.get_chat_setting(event.chat.id, "goodbye", "👋 {name} покинул(а) {title}.")
    rendered = template.replace("{name}", html.escape(user.full_name)).replace("{title}", html.escape(event.chat.title or "наш чат"))
    try:
        await event.bot.send_message(event.chat.id, rendered[:1000], parse_mode="HTML")
    except Exception:
        log.exception("Could not send goodbye")


@dp.message(Command("chatstats"))
async def community_stats(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        return await message.answer("Статистика доступна в группе.")
    if not await _chat_admin(message):
        return await message.answer("⛔ Статистика доступна администраторам чата.")
    users, messages, warnings = community.stats(message.chat.id)
    await message.answer(f"📊 <b>Статистика чата</b>\nУчастников в экономике: {users}\nУчтено сообщений: {messages}\nПредупреждений: {warnings}", parse_mode="HTML")


@dp.message(Command("filterexempt"))
async def filter_exempt_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not await _chat_admin(message):
        return await message.answer("⛔ Только администратор группы может менять исключения.")
    args = (message.text or "").split()
    key = "filter_exempt_users"
    try:
        exempt = json.loads(community.get_chat_setting(message.chat.id, key, "[]"))
        if not isinstance(exempt, list): exempt = []
    except Exception:
        exempt = []
    if len(args) == 1 or args[1].casefold() == "list":
        return await message.answer("Исключения из автоматических фильтров: " + (", ".join(map(str, exempt)) if exempt else "нет") + "\nДобавить: /filterexempt add ID\nУдалить: /filterexempt del ID")
    if len(args) != 3 or args[1].casefold() not in {"add", "del"}:
        return await message.answer("Использование: /filterexempt add ID или /filterexempt del ID")
    try:
        user_id = int(args[2])
    except ValueError:
        return await message.answer("ID должен быть числом. Узнать ID можно по сообщению/профилю участника.")
    if args[1].casefold() == "add":
        if user_id not in exempt: exempt.append(user_id)
    else:
        exempt = [value for value in exempt if value != user_id]
    community.set_chat_setting(message.chat.id, key, json.dumps(exempt[:200]))
    await message.answer(f"✅ Исключения обновлены. Сейчас в списке: {len(exempt)}.")


async def _send_community_profile_card(message: Message, target):
    """Send a per-user glass profile card in the current group."""
    if not target:
        return
    cid = message.chat.id
    profile = community.get_user_profile(cid, target.id)
    stats = community.get_community_profile(cid, target.id)
    shown_name = profile.get("nickname") or target.full_name
    path = render_profile_card(
        name=shown_name, user_id=target.id, title=profile.get("title", ""),
        bio=profile.get("bio", ""), level=int(stats.get("level", 1)),
        xp=int(stats.get("xp", 0)), messages=int(stats.get("messages", 0)),
        coins=int(stats.get("coins", 0)), first_seen=stats.get("first_seen", ""),
        chat_title=message.chat.title or "Сообщество PULSAR",
    )
    try:
        await message.reply_photo(FSInputFile(path), caption="✨ Стеклянная карточка профиля сообщества")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@dp.message(F.text, ~F.text.startswith("/"))
async def community_text_router(message: Message):
    if not message.from_user or not message.text or message.chat.type not in {"group", "supergroup"}:
        raise SkipHandler()
    # Keep a tiny rolling context from ordinary public group messages for AI replies.
    # This is in-memory only and disappears when the bot process restarts.
    if len(message.text.strip()) >= 2 and not message.text.startswith("/"):
        ai_memory.remember(message.chat.id, message.chat.title or str(message.chat.id),
                           message.from_user.id, message.from_user.username or message.from_user.full_name,
                           message.text.strip())
        _GROUP_AI_MEMORY[message.chat.id].append({
            "role": "user",
            "content": f"{message.from_user.full_name}: {message.text.strip()[:500]}",
        })
    community.add_message(
        message.chat.id, message.from_user.id,
        message.from_user.username or message.from_user.full_name,
        message.chat.title or str(message.chat.id),
    )
    # Separate callout module maintains a roster of participants seen by the bot.
    group_call.record_member(
        message.chat.id, message.from_user.id, message.from_user.full_name,
        message.from_user.username or "",
    )
    raw = message.text.strip()
    command = raw.casefold().strip(" .,!?:;")
    uid, cid = message.from_user.id, message.chat.id
    # Each group may choose its own free text-command prefix (default: none).
    custom_prefix = community.get_chat_setting(cid, "custom_prefix", "")
    if custom_prefix and raw.startswith(custom_prefix):
        raw = raw[len(custom_prefix):].strip()
        command = raw.casefold().strip(" .,!?:;")

    # Per-chat delegated moderator ranks. Telegram admins remain the authority ceiling.
    if command.startswith("повысить") or command.startswith("назначить модер"):
        target = await _target_from_reply(message)
        parts = raw.split(maxsplit=2)
        if not target:
            await message.reply("Ответь на сообщение участника: повысить 3 [права через запятую]"); return
        if target.id in {uid, message.bot.id}:
            await message.reply("⛔ Нельзя назначить самого себя или бота."); return
        try:
            target_member = await message.bot.get_chat_member(cid, target.id)
            actor_member = await message.bot.get_chat_member(cid, uid)
        except Exception:
            await message.reply("Не удалось проверить роли участников. Назначение отменено."); return
        if target_member.status in {"administrator", "creator"}:
            await message.reply("⛔ Участник уже является администратором Telegram."); return
        try:
            rank = int(parts[1])
        except (IndexError, ValueError):
            await message.reply("Укажи уровень от 1 до 4: повысить 3 [warn,mute,ban,unmute,call]"); return
        if rank not in {1, 2, 3, 4}:
            await message.reply("Уровень должен быть от 1 до 4."); return
        actor_role = community.get_moderator_role(cid, uid)
        actor_is_admin = actor_member.status in {"administrator", "creator"}
        if not actor_is_admin and (not actor_role or "appoint" not in actor_role["permissions"] or rank >= actor_role["rank"]):
            await message.reply("⛔ Назначать модераторов может администратор чата или модератор с правом appoint; он может назначить только более низкий уровень."); return
        allowed = {"warn", "mute", "ban", "unmute", "call", "appoint", "viewlog"}
        requested = set(parts[2].replace(" ", "").casefold().split(",")) if len(parts) > 2 else {"warn", "mute", "call"}
        if not requested or not requested <= allowed:
            await message.reply("Права: warn,mute,ban,unmute,call,appoint,viewlog"); return
        if not actor_is_admin and not requested <= actor_role["permissions"]:
            await message.reply("⛔ Нельзя передать права, которых нет у тебя."); return
        community.set_moderator_role(cid, target.id, rank, requested, uid)
        community.log_moderation(cid, uid, target.id, "promote", f"rank={rank};permissions={','.join(sorted(requested))}")
        await message.reply(f"🛡️ {html.escape(target.full_name)} назначен модератором уровня {rank}. Права: {html.escape(', '.join(sorted(requested)))}")
        return

    if command.startswith("понизить") or command.startswith("снять модер"):
        target = await _target_from_reply(message)
        if not target:
            await message.reply("Ответь на сообщение модератора и напиши: понизить"); return
        if not await _chat_admin(message):
            actor_role = community.get_moderator_role(cid, uid)
            if not actor_role or "appoint" not in actor_role["permissions"]:
                await message.reply("⛔ Снять роль может администратор или модератор с правом appoint."); return
        target_role = community.get_moderator_role(cid, target.id)
        if not target_role:
            await message.reply("У этого участника нет назначенной роли модератора."); return
        actor_role = community.get_moderator_role(cid, uid)
        if not await _chat_admin(message) and (not actor_role or target_role["rank"] >= actor_role["rank"]):
            await message.reply("⛔ Нельзя снимать роль равного или старшего уровня."); return
        community.remove_moderator_role(cid, target.id)
        community.log_moderation(cid, uid, target.id, "demote", "Роль модератора снята")
        await message.reply(f"Роль модератора снята с {html.escape(target.full_name)}.")
        return

    if command in {"модеры", "модераторы", "права модераторов"}:
        if not await _chat_admin(message) and not (community.get_moderator_role(cid, uid) and "viewlog" in community.get_moderator_role(cid, uid)["permissions"]):
            await message.reply("⛔ Просматривать список может администратор или модератор с правом viewlog."); return
        roles = community.list_moderator_roles(cid)
        if not roles:
            await message.reply("Назначенных модераторов пока нет."); return
        lines = []
        for role in roles[:50]:
            try:
                member = await message.bot.get_chat_member(cid, int(role["user_id"]))
                name = html.escape(member.user.full_name)
            except Exception:
                name = f"ID {role['user_id']}"
            lines.append(f"• {name} — уровень {role['rank']}; {html.escape(role['permissions'])}")
        await message.reply("🛡️ <b>Модераторы чата</b>\n" + "\n".join(lines), parse_mode="HTML"); return

    if command in {"кал", "калл", "созыв", "созвать всех", "все сюда"}:
        role = community.get_moderator_role(cid, uid)
        if not await _chat_admin(message) and not (role and "call" in role["permissions"]):
            await message.reply("⛔ Созывать участников может только администратор или назначенный модератор с правом call."); return
        # Telegram cannot provide a full member list. Mention everyone the bot has
        # observed in this group, excluding the caller to avoid a redundant ping.
        members = group_call.list_members(cid, exclude_user_id=uid)
        chunks = group_call.render_mentions(members)
        if not chunks:
            await message.answer(
                "📣 <b>СРОЧНЫЙ СОЗЫВ ВСЕХ УЧАСТНИКОВ!</b>\n"
                "Пока некого отметить: бот ещё не видел сообщений от других участников.",
                parse_mode="HTML",
            )
            return
        await message.answer(
            f"📣 <b>СРОЧНЫЙ СОЗЫВ ВСЕХ УЧАСТНИКОВ!</b>\n"
            f"Отмечаю {len(members)} участников, чьи сообщения бот уже видел в этой группе.\n"
            "Важно: Telegram не даёт боту получить список молчащих/неизвестных участников, поэтому они могут не попасть в созыв.",
            parse_mode="HTML",
        )
        for chunk in chunks:
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
        return

    # Text-only moderation aliases. These are handled before automod and never require a slash.
    if command == "варн" or command.startswith("варн ") or command == "предупреждение" or command.startswith("предупреждение "):
        role = community.get_moderator_role(cid, uid)
        if not await _chat_admin(message) and not (role and "warn" in role["permissions"]):
            await message.reply("⛔ Нужна роль администратора или право warn."); return
        target = await _target_from_reply(message)
        if not target:
            await message.reply("Ответь на сообщение участника и напиши: варн причина"); return
        try:
            target_member = await message.bot.get_chat_member(cid, target.id)
            if target.id == uid or target.id == message.bot.id or target_member.status in {"administrator", "creator"}:
                await message.reply("⛔ Нельзя выдать предупреждение этому участнику."); return
        except Exception:
            await message.reply("Не удалось проверить роль участника. Действие отменено."); return
        reason = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else "Причина не указана"
        count = community.add_warning(cid, target.id, uid, reason)
        community.log_moderation(cid, uid, target.id, "warn", reason)
        await message.reply(f"⚠️ {html.escape(target.full_name)} получил предупреждение ({count}). Причина: {html.escape(reason)}")
        return
    if command == "мут" or command.startswith("мут "):
        role = community.get_moderator_role(cid, uid)
        if not await _chat_admin(message) and not (role and "mute" in role["permissions"]):
            await message.reply("⛔ Нужна роль администратора или право mute."); return
        target = await _target_from_reply(message)
        if not target:
            await message.reply("Ответь на сообщение участника и напиши: мут 10 причина"); return
        try:
            target_member = await message.bot.get_chat_member(cid, target.id)
            if target.id == uid or target.id == message.bot.id or target_member.status in {"administrator", "creator"}:
                await message.reply("⛔ Нельзя ограничить этого участника."); return
        except Exception:
            await message.reply("Не удалось проверить роль участника. Действие отменено."); return
        parts = raw.split(maxsplit=2); duration = 10; reason = "Нарушение правил"
        if len(parts) > 1:
            try:
                duration = max(1, min(10080, int(parts[1])))
                if len(parts) > 2: reason = parts[2]
            except ValueError:
                reason = " ".join(parts[1:])
        from datetime import datetime, timedelta, timezone
        try:
            await message.bot.restrict_chat_member(cid, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc)+timedelta(minutes=duration))
            community.log_moderation(cid, uid, target.id, "mute", reason)
            await message.reply(f"🔇 {html.escape(target.full_name)} ограничен на {duration} мин. Причина: {html.escape(reason)}")
        except Exception:
            log.exception("Text mute failed")
            await message.reply("Не удалось выдать мут. Проверь права администратора бота.")
        return
    if command in {"размут", "снять мут"}:
        role = community.get_moderator_role(cid, uid)
        if not await _chat_admin(message) and not (role and "unmute" in role["permissions"]):
            await message.reply("⛔ Нужна роль администратора или право unmute."); return
        target = await _target_from_reply(message)
        if not target:
            await message.reply("Ответь на сообщение участника и напиши: размут"); return
        try:
            await message.bot.restrict_chat_member(cid, target.id, permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
            community.log_moderation(cid, uid, target.id, "unmute", "")
            await message.reply(f"🔊 Ограничения сняты с {html.escape(target.full_name)}.")
        except Exception:
            log.exception("Text unmute failed")
            await message.reply("Не удалось снять ограничения. Проверь права бота.")
        return

    if command == "бан" or command.startswith("бан "):
        role = community.get_moderator_role(cid, uid)
        if not await _chat_admin(message) and not (role and "ban" in role["permissions"]):
            await message.reply("⛔ Нужна роль администратора или право ban."); return
        target = await _target_from_reply(message)
        if not target:
            await message.reply("Ответь на сообщение участника и напиши: бан причина"); return
        try:
            target_member = await message.bot.get_chat_member(cid, target.id)
            if target.id == uid or target.id == message.bot.id or target_member.status in {"administrator", "creator"}:
                await message.reply("⛔ Нельзя заблокировать этого участника."); return
        except Exception:
            await message.reply("Не удалось проверить роль участника. Действие отменено."); return
        reason = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else "Причина не указана"
        try:
            await message.bot.ban_chat_member(cid, target.id)
            community.log_moderation(cid, uid, target.id, "ban", reason)
            await message.reply(f"🚫 {html.escape(target.full_name)} заблокирован. Причина: {html.escape(reason)}")
        except Exception:
            log.exception("Text ban failed")
            await message.reply("Не удалось заблокировать участника. Проверь права бота.")
        return

    # Text-first administration: Iris-like group utilities without slash commands.
    # These are intentionally handled before automod so configuration messages are not filtered.
    if command.startswith("задать правила ") or command.startswith("правила установить "):
        if not await _chat_admin(message):
            await message.reply("⛔ Менять правила может только администратор группы."); return
        body = raw.split(maxsplit=2)[2].strip()
        if not body or len(body) > 3500:
            await message.reply("Текст правил должен содержать от 1 до 3500 символов."); return
        community.set_chat_setting(cid, "rules", body)
        await message.reply("📜 Правила группы сохранены.")
        return
    if command.startswith("задать приветствие ") or command.startswith("приветствие установить "):
        if not await _chat_admin(message):
            await message.reply("⛔ Менять приветствие может только администратор группы."); return
        template = raw.split(maxsplit=2)[2].strip()
        if not template or len(template) > 1000:
            await message.reply("Шаблон должен содержать от 1 до 1000 символов."); return
        community.set_chat_setting(cid, "welcome", template)
        community.set_chat_setting(cid, "welcome_enabled", "1")
        await message.reply("👋 Приветствие сохранено. Переменные: {name}, {title}.")
        return
    if command.startswith("задать прощание ") or command.startswith("прощание установить "):
        if not await _chat_admin(message):
            await message.reply("⛔ Менять прощание может только администратор группы."); return
        template = raw.split(maxsplit=2)[2].strip()
        if not template or len(template) > 1000:
            await message.reply("Шаблон должен содержать от 1 до 1000 символов."); return
        community.set_chat_setting(cid, "goodbye", template)
        community.set_chat_setting(cid, "goodbye_enabled", "1")
        await message.reply("👋 Прощание сохранено. Переменные: {name}, {title}.")
        return
    if command in {"выключить приветствия", "приветствия выкл"}:
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        community.set_chat_setting(cid, "welcome_enabled", "0")
        await message.reply("Приветствия отключены."); return
    if command in {"включить приветствия", "приветствия вкл"}:
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        community.set_chat_setting(cid, "welcome_enabled", "1")
        await message.reply("Приветствия включены."); return
    if command in {"выключить прощания", "прощания выкл"}:
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        community.set_chat_setting(cid, "goodbye_enabled", "0")
        await message.reply("Прощания отключены."); return
    if command in {"включить прощания", "прощания вкл"}:
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        community.set_chat_setting(cid, "goodbye_enabled", "1")
        await message.reply("Прощания включены."); return
    if command.startswith("добавить фильтр "):
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        trigger = raw[len("добавить фильтр "):].strip()
        if not community.set_chat_filter(cid, trigger, "🚫 Это сообщение содержит запрещённый фильтром текст.", uid):
            await message.reply("Фраза должна содержать минимум 2 символа."); return
        await message.reply("🛡️ Фильтр добавлен. Совпавшие сообщения будут отмечаться; для удаления используй «удалить фильтр фраза».")
        return
    if command.startswith("удалить фильтр "):
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        trigger = raw[len("удалить фильтр "):].strip()
        removed = community.delete_chat_filter(cid, trigger)
        await message.reply("🗑 Фильтр удалён." if removed else "Такой фильтр не найден."); return
    if command in {"список фильтров", "фильтры чата"}:
        rows = community.list_chat_filters(cid)
        await message.reply("🛡️ Фильтры: " + (", ".join(html.escape(x["trigger"]) for x in rows) if rows else "не настроены"), parse_mode="HTML")
        return
    if command in {"помощь сообщества", "команды сообщества", "помощь группы"}:
        await message.reply(
            "<b>Команды сообщества без /</b>\n"
            "профиль · мой уровень · топ чата · топ активности\n"
            "ник Текст · титул Текст · о себе Текст\n"
            "заметки · заметка название · правила\n"
            "монетка · кубик · ежедневный бонус · собрать ресурс\n"
            "Админам: задать правила Текст · задать приветствие Текст · задать прощание Текст\n"
            "добавить фильтр фраза · удалить фильтр фраза · список фильтров\n"
            "Для модерации ответь на сообщение: варн причина · мут 10 причина · бан причина · разбан · репорт причина",
            parse_mode="HTML")
        return

    # A profile request renders a shareable glass card for the author, or for a replied-to member.
    if command in {"профиль", "мой профиль сообщества", "профиль сообщества", "карточка профиля", "моя карточка"}:
        target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else message.from_user
        await _send_community_profile_card(message, target)
        return

    # Free profile customization, per-chat command prefix and settings backup.
    if command.startswith("ник ") or command.startswith("никнейм "):
        value = raw.split(maxsplit=1)[1].strip()
        community.set_user_profile(cid, uid, "nickname", value)
        await message.reply("✅ Ник в этой группе: " + html.escape(value[:32] or "сброшен"))
        return
    if command.startswith("титул "):
        value = raw.split(maxsplit=1)[1].strip()
        community.set_user_profile(cid, uid, "title", value)
        await message.reply("🏷️ Титул сохранён: " + html.escape(value[:48] or "сброшен"))
        return
    if command.startswith("о себе ") or command.startswith("био "):
        value = raw.split(maxsplit=1)[1].strip()
        community.set_user_profile(cid, uid, "bio", value)
        await message.reply("📝 Информация о себе сохранена." if value else "📝 Информация о себе очищена.")
        return
    if command in {"мой профиль сообщества", "профиль сообщества", "мой уровень", "статистика сообщества"}:
        profile = community.get_user_profile(cid, uid)
        stats = community.get_community_profile(cid, uid)
        shown_name = profile["nickname"] or message.from_user.full_name
        xp = int(stats.get("xp", 0)); level = int(stats.get("level", 1))
        await message.reply(f"👤 <b>{html.escape(shown_name)}</b>\n🏷️ {html.escape(profile['title'] or 'Без титула')}\n📝 {html.escape(profile['bio'] or 'О себе не заполнено')}\n⭐ Уровень: <b>{level}</b> · XP: <b>{xp}</b>\n💬 Сообщений: <b>{int(stats.get('messages', 0))}</b> · 🪙 Коинов: <b>{int(stats.get('coins', 0))}", parse_mode="HTML")
        return
    if command.startswith("префикс "):
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы может менять префикс."); return
        value = raw.split(maxsplit=1)[1].strip()
        if value.lower() in {"off", "выкл", "нет", "сброс"}: value = ""
        if len(value) > 3 or any(ch.isspace() for ch in value):
            await message.reply("Использование: префикс ! (или «префикс выкл»; максимум 3 символа)."); return
        community.set_chat_setting(cid, "custom_prefix", value)
        await message.reply("✅ Префикс: " + (html.escape(value) if value else "отключён") + (". Пример: " + html.escape(value) + "помощь" if value else "."), parse_mode="HTML")
        return
    # Custom commands (per chat), similar to Iris shortcuts/autoresponders.
    if command.startswith("команда создать ") and " => " in raw:
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы может создавать команды."); return
        spec = raw[len("команда создать "):].split(" => ", 1)
        name = spec[0].strip().casefold().strip(" !./")
        reply = spec[1].strip()
        if not name or len(name) > 40 or not reply or len(reply) > 1000 or name.startswith(("команда ", "бан", "мут", "варн")):
            await message.reply("Формат: команда создать привет => Добро пожаловать! (имя до 40 символов, ответ до 1000)."); return
        community.set_chat_setting(cid, "custom_command:" + name, reply)
        await message.reply("✅ Команда «" + html.escape(name) + "» сохранена.")
        return
    if command.startswith("команда удалить "):
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        name = raw[len("команда удалить "):].strip().casefold().strip(" !./")
        with community.connect() as db:
            db.execute("DELETE FROM community_settings WHERE chat_id=? AND setting=?", (cid, "custom_command:" + name))
        await message.reply("🗑 Команда удалена (если существовала).")
        return
    if command in {"мои предупреждения", "мои варны"}:
        rows = community.user_warnings(cid, uid)
        if not rows:
            await message.reply("✅ У тебя нет предупреждений в этом чате.")
        else:
            lines = [f"{i}. {html.escape(row['reason'])} — {html.escape(str(row['created_at']))}" for i,row in enumerate(rows, 1)]
            await message.reply("⚠️ <b>Твои предупреждения</b>\n" + "\n".join(lines), parse_mode="HTML")
        return
    if command in {"экспорт настроек", "бэкап настроек", "backup settings"}:
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        payload = json.dumps(community.export_chat_settings(cid), ensure_ascii=False, indent=2)
        if len(payload) <= 3800:
            await message.reply("<b>Резервная копия настроек</b>\n<code>" + html.escape(payload) + "</code>\n\nСкопируй JSON для восстановления.", parse_mode="HTML")
        else:
            await message.reply("Бэкап слишком большой для сообщения. Удали часть заметок/фильтров и повтори.")
        return
    if command.startswith("импорт настроек "):
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор группы."); return
        try:
            payload = json.loads(raw[len("импорт настроек "):].strip())
            ok = community.import_chat_settings(cid, payload)
        except Exception:
            ok = False
        await message.reply("✅ Настройки импортированы." if ok else "❌ JSON не распознан. Используй JSON из «экспорт настроек». Для больших бэкапов отправь его отдельным файлом в чат с ботом — автоматический импорт файлов пока не поддержан.")
        return

    # Free community features inspired by common Telegram group-bot utilities.
    if command in {"топ чата", "топ коины", "топ сообщества", "богачи чата"}:
        rows = community.leaderboard(cid, 10)
        if not rows:
            await message.reply("🏆 Пока нет участников в рейтинге. Пиши сообщения и участвуй в жизни группы!")
            return
        all_rows = community.leaderboard(cid, 100000)
        await send_live_chat_rank_card(
            message, title="ТОП СООБЩЕСТВА ПО КОИНАМ", rows=all_rows,
            metric_label="Коины", metric_key="coins", period_label="ВСЁ ВРЕМЯ", xp_key="xp",
        )
        return
    if command in {"топ активности", "актив чата", "топ сообщений"}:
        with community.connect() as db:
            rows = db.execute("SELECT user_id, count FROM community_messages WHERE chat_id=? ORDER BY count DESC, user_id ASC LIMIT 10", (cid,)).fetchall()
        if not rows:
            await message.reply("📊 Статистика активности пока пуста.")
            return
        all_rows = []
        with community.connect() as db:
            all_rows = [dict(r) for r in db.execute(
                "SELECT user_id, MAX(username) AS username, COUNT(*) AS messages FROM community_activity_events WHERE chat_id=? GROUP BY user_id ORDER BY messages DESC, user_id ASC",
                (cid,),
            ).fetchall()]
        await send_live_chat_rank_card(
            message, title="ТОП АКТИВНОСТИ", rows=all_rows, metric_label="Сообщения",
            metric_key="messages", period_label="ЗА ВСЁ ВРЕМЯ", xp_key=None,
        )
        return
    if command in {"инфо", "инфо участника", "карточка участника", "профиль участника"} and message.reply_to_message and message.reply_to_message.from_user:
        await _send_community_profile_card(message, message.reply_to_message.from_user)
        return
    if command in {"монетка", "орёл или решка", "подбросить монетку"}:
        await message.reply("🪙 Выпало: <b>" + random.choice(["Орёл", "Решка"]) + "</b>", parse_mode="HTML")
        return
    if command in {"кубик", "бросить кубик", "кинь кубик"}:
        roll = random.randint(1, 6)
        await message.reply(f"🎲 Выпало <b>{roll}</b> из 6.", parse_mode="HTML")
        return

    # Independent community scratch-card game and social role-play (not casino balance).
    if command in {"скретч", "скретч-карта", "стереть карту", "лотерейка", "карточка"}:
        result, wait = community.scratch_card(cid, uid)
        if result is None:
            await message.reply(f"🎟️ Следующая скретч-карта будет доступна через {max(1, wait // 60)} мин. Это игра за коины сообщества, не казино.")
        elif result["reward"]:
            await message.reply(f"🎟️ <b>СКРЕТЧ-КАРТА</b>\n\n✨ Ты стёр защитный слой и нашёл <b>+{result['reward']} коинов</b>!\n🪙 Баланс сообщества: <b>{result['balance']}</b>", parse_mode="HTML")
        else:
            await message.reply(f"🎟️ <b>СКРЕТЧ-КАРТА</b>\n\n😅 Пусто! В следующий раз повезёт.\n🪙 Баланс сообщества: <b>{result['balance']}</b>", parse_mode="HTML")
        return

    social_aliases = {
        "поцелуй": ("kiss", "💋", "поцеловал(а)"), "поцеловать": ("kiss", "💋", "поцеловал(а)"),
        "обнять": ("hug", "🫂", "обнял(а)"), "обнимашки": ("hug", "🫂", "обнял(а)"), "обнимашка": ("hug", "🫂", "обнял(а)"),
        "секс": ("adult_18", "❤️‍🔥", "предложил(а) взрослый романтический ролевой момент с"),
        "сюрприз": ("gift_surprise", "🎁", "приготовил(а) сюрприз для"), "приготовить сюрприз": ("gift_surprise", "🎁", "приготовил(а) сюрприз для"),
        "сделать сюрприз": ("gift_surprise", "🎁", "приготовил(а) сюрприз для"),
        "признание": ("confession", "💌", "признался(ась) в чувствах к"),
        "свидание": ("date", "🌹", "пригласил(а) на свидание"),
        "массаж": ("massage", "🕯️", "предложил(а) расслабляющий массаж"),
        "цветы": ("flowers", "💐", "подарил(а) цветы"),
        "вечер вместе": ("movie_night", "🎬", "предложил(а) уютный вечер вместе с"),
        "поддержать": ("support", "🤝", "поддержал(а)"),
        "флирт": ("flirt", "😻", "флиртует с"),
        "любовь": ("love", "💖", "отправил(а) знак любви для"),
    }
    async def reply_with_social_card(card: str, caption: str):
        card_path = Path(__file__).resolve().parent.parent / "assets" / "social_cards" / f"{card}.jpg"
        if card_path.is_file():
            await message.reply_photo(FSInputFile(card_path), caption=caption, parse_mode="HTML")
        else:
            await message.reply(caption, parse_mode="HTML")

    if command in {"жениться", "выйти замуж", "предложение", "брак"}:
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not target:
            await message.reply("💍 Чтобы сделать предложение, ответь на сообщение человека словом «жениться» или «выйти замуж».")
        elif target.is_bot:
            await message.reply("🤖 Бот не может вступить в брак — выбери участника группы.")
        elif target.id == uid:
            await message.reply("💍 Нельзя сделать предложение самому себе — выбери другого участника.")
        else:
            ok, result_text = community.marry(cid, uid, message.from_user.full_name, target.id, target.full_name)
            card = "marry" if ok else "proposal"
            await reply_with_social_card(card, ("💍 " if ok else "💌 ") + html.escape(result_text))
        return
    if command in {"развод", "развестись"}:
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not target:
            await message.reply("💔 Ответь на сообщение супруга/супруги словом «развод».")
        elif community.divorce(cid, uid, target.id):
            await reply_with_social_card("divorce", f"💔 {html.escape(message.from_user.full_name)} и {html.escape(target.full_name)} теперь не состоят в браке в этой группе.")
        else:
            await reply_with_social_card("divorce", "💔 Брак с этим участником не найден.")
        return
    if command in {"семья", "моя семья", "семья статус", "дети", "родители"}:
        status = community.social_status(cid, uid)
        lines = ["👨‍👩‍👧 <b>СЕМЬЯ</b>"]
        if status["partner"]: lines.append("💍 Партнёр: " + html.escape(status["partner"]["name"]))
        else: lines.append("💍 Пока без партнёра")
        lines.append("👶 Дети: " + (", ".join(html.escape(x["child_name"]) for x in status["children"]) or "пока нет"))
        lines.append("🧑‍🧑‍🧒 Родители: " + (", ".join(html.escape(x["parent_name"]) for x in status["parents"]) or "не указаны"))
        card = "children" if command == "дети" else "parents" if command == "родители" else "family"
        await reply_with_social_card(card, "\n".join(lines))
        return
    if command in {"усыновить", "удочерить", "добавить в семью"}:
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not target:
            await message.reply("👨‍👩‍👧 Ответь на сообщение участника словом «усыновить» или «удочерить».")
        elif target.is_bot:
            await message.reply("Бота нельзя добавить в семью.")
        elif target.id == uid:
            await message.reply("Нельзя добавить самого себя в семью.")
        else:
            ok, result_text = community.add_family_member(cid, uid, message.from_user.full_name, target.id, target.full_name, "ребёнок")
            await reply_with_social_card("adopt", html.escape(result_text))
        return
    if command in social_aliases:
        target = message.reply_to_message.from_user if message.reply_to_message else None
        card, emoji, verb = social_aliases[command]
        if not target:
            await message.reply(f"{emoji} Ответь на сообщение человека командой «{command}».")
        elif target.id == uid:
            await message.reply("🙂 Самому себе это действие не адресовать — выбери другого участника.")
        elif target.is_bot:
            await message.reply("🤖 Лучше адресуй это действие живому участнику группы.")
        else:
            if command == "секс":
                caption = f"🔞 {html.escape(message.from_user.full_name)} предлагает {html.escape(target.full_name)} взрослый романтический ролевой момент. Только по взаимному согласию — отказ всегда окей."
            else:
                caption = f"{emoji} {html.escape(message.from_user.full_name)} {verb} {html.escape(target.full_name)}! 💖"
            await reply_with_social_card(card, caption)
        return

    if command in {"поддержи", "мне грустно", "мне плохо", "мотивируй"}:
        answer = await _pulsar_ai_reply(message, "support")
        await message.answer(answer or "🫂 Ты не обязан решать всё сразу. Сделай один маленький шаг, выдохни и дай себе немного времени.")
        return
    if command in {"прожарь", "оскорби меня", "roast", "подколи меня"}:
        answer = await _pulsar_ai_reply(message, "roast")
        await message.answer(answer or "🔥 У тебя такая энергия главного героя, что даже побочные квесты ждут, пока ты освободишься.")
        return
    if command.startswith(("пульсар ", "пульсар,", "бот,", "бот ", "ответь ")):
        answer = await _pulsar_ai_reply(message, "chat")
        await message.answer(answer or "🤖 🤖 ИИ сейчас недоступен. Попробуй позже.")
        return
    # Autonomous context-aware banter: public groups only, API key required,
    # sampled probabilistically and rate-limited per group so it never floods chat.
    # Never let probabilistic banter swallow a real command/shortcut.
    # This block runs before some text-command branches, so explicitly exclude their aliases.
    _known_shortcuts = {
        "топ", "топ чата", "топ коинов", "богачи чата", "топ все", "топ всего",
        "топ вся", "топ весь", "топ неделя", "топ недели", "топ месяц", "топ месяца",
        "профиль", "играть", "пополнить", "вывести", "правила", "админы", "админы чата",
        "кто админ", "кто администратор", "модеры", "созыв", "созвать всех", "все сюда",
        "кал", "калл", "помощь", "команды", "дейлик", "ежедневный бонус", "ежедневка",
    }
    _looks_like_shortcut = command in _known_shortcuts or command.startswith((
        "повысить ", "понизить", "варн ", "предупреждение ", "мут ", "бан ",
        "размут", "разбан", "заметка ", "купить ", "продать ", "пульсар ",
        "пульсар,", "бот ", "бот,", "ответь ",
    ))
    if (os.getenv("PULSAR_AI_API_KEY", "").strip() and len(raw) >= 8 and not raw.startswith("/")
            and not _looks_like_shortcut and not message.from_user.is_bot
            and message.chat.type in {"group", "supergroup"}):
        now = time.time()
        if random.random() < 0.08 and now - _GROUP_AI_LAST_REPLY.get(cid, 0) >= _AI_REPLY_COOLDOWN:
            _GROUP_AI_LAST_REPLY[cid] = now
            answer = await _pulsar_ai_reply(message, "chat")
            if answer:
                await message.reply(answer)
                return

    # Plain-language free community-helper shortcuts (no slash required).
    if command in {"id", "мой id", "айди", "мой айди", "кто я"}:
        await message.answer(f"🪪 {html.escape(message.from_user.full_name)}\nВаш Telegram ID: <code>{uid}</code>\nID чата: <code>{cid}</code>", parse_mode="HTML")
        return
    if command in {"админы", "админы чата", "кто админ", "кто администратор"}:
        try:
            admins = await message.bot.get_chat_administrators(cid)
            lines = [f"• {html.escape(a.user.full_name)}" + (" — владелец" if a.status == "creator" else " — администратор") for a in admins if a.user]
            await message.answer("🛡️ <b>Администраторы чата</b>\n" + ("\n".join(lines) or "Не удалось получить список."), parse_mode="HTML")
        except Exception:
            await message.answer("Не удалось получить список администраторов.")
        return
    if command in {"правила", "правила чата"}:
        body = community.get_chat_setting(cid, "rules", "")
        await message.answer("📜 <b>Правила чата</b>\n\n" + html.escape(body) if body else "Правила ещё не настроены.", parse_mode="HTML")
        return
    if command in {"заметки", "список заметок"}:
        notes = community.list_chat_notes(cid)
        await message.answer("📝 Заметки: " + (", ".join(html.escape(n) for n in notes) if notes else "пока нет") + "\nОткрыть: заметка название", parse_mode="HTML")
        return
    if command.startswith("заметка "):
        note_name = raw.split(maxsplit=1)[1].strip()
        body = community.get_chat_note(cid, note_name)
        if body:
            await message.answer("📝 <b>" + html.escape(note_name) + "</b>\n\n" + html.escape(body), parse_mode="HTML")
        else:
            await message.answer("Заметка не найдена. Напиши «заметки», чтобы увидеть список.")
        return
    if command.startswith("правила установить "):
        if not await _chat_admin(message):
            await message.answer("⛔ Только администратор может менять правила.")
            return
        body = raw[len("правила установить "):].strip()
        if not body:
            await message.answer("После фразы «правила установить» напиши текст правил.")
        else:
            community.set_chat_setting(cid, "rules", body[:3500])
            await message.answer("✅ Правила сохранены. Просмотр: «правила».")
        return
    if command.startswith("приветствие установить "):
        if not await _chat_admin(message):
            await message.answer("⛔ Только администратор может менять приветствие.")
            return
        body = raw[len("приветствие установить "):].strip()
        community.set_chat_setting(cid, "welcome", body[:1000])
        community.set_chat_setting(cid, "welcome_enabled", "1")
        await message.answer("✅ Приветствие сохранено. Переменные: {name}, {title}.")
        return
    if command in {"антиссылки вкл", "включить антиссылки", "антиссылки выкл", "выключить антиссылки", "антифлуд вкл", "включить антифлуд", "антифлуд выкл", "выключить антифлуд"}:
        if not await _chat_admin(message):
            await message.answer("⛔ Только администратор может менять защиту.")
            return
        setting = "antilink" if "антиссылки" in command else "antiflood"
        enabled = command.endswith("вкл") or command.startswith("включить")
        community.set_chat_setting(cid, setting, "1" if enabled else "0")
        await message.answer(("🔗 Антиссылки " if setting == "antilink" else "🚦 Антифлуд ") + ("включены." if enabled else "отключены."))
        return
    if command.startswith("фильтр ") and " => " in raw:
        if not await _chat_admin(message):
            await message.answer("⛔ Только администратор может создавать фильтры.")
            return
        spec = raw[len("фильтр "):].split(" => ", 1)
        if len(spec) == 2 and community.set_chat_filter(cid, spec[0], spec[1], uid):
            await message.answer("✅ Автоответ сохранён. При совпадении с фразой бот ответит текстом справа от =>.")
        else:
            await message.answer("Формат: фильтр слово или фраза => текст ответа")
        return
    if command.startswith("удалить фильтр "):
        if not await _chat_admin(message):
            await message.answer("⛔ Только администратор может удалять фильтры.")
            return
        trigger = raw[len("удалить фильтр "):].strip()
        ok = community.delete_chat_filter(cid, trigger)
        await message.answer("✅ Фильтр удалён." if ok else "Такой фильтр не найден.")
        return

    if command.startswith("запретить слово ") or command.startswith("разрешить слово ") or command == "запрещённые слова":
        if command == "запрещённые слова":
            try: words = json.loads(community.get_chat_setting(cid, "blocked_words", "[]"))
            except Exception: words = []
            await message.reply("🚫 Запрещённые слова: " + (", ".join(html.escape(str(w)) for w in words) if words else "список пуст") + "\nНастройка: запретить слово <слово> / разрешить слово <слово>.")
            return
        if not await _chat_admin(message):
            await message.reply("⛔ Только администратор может менять список запрещённых слов."); return
        adding = command.startswith("запретить слово ")
        word = raw.split(maxsplit=2)[2].strip().casefold() if len(raw.split(maxsplit=2)) > 2 else ""
        if not word or len(word) > 80:
            await message.reply("Укажи слово или короткую фразу (до 80 символов)."); return
        try: words = json.loads(community.get_chat_setting(cid, "blocked_words", "[]"))
        except Exception: words = []
        words = [str(w).casefold() for w in words if str(w).strip()]
        if adding and word not in words: words.append(word)
        if not adding: words = [w for w in words if w != word]
        community.set_chat_setting(cid, "blocked_words", json.dumps(words[:100], ensure_ascii=False))
        await message.reply(("🚫 Добавлено в фильтр: " if adding else "✅ Удалено из фильтра: ") + html.escape(word))
        return

    # Explicitly trusted users can be exempted from automated filters; manual admin moderation still applies.
    try:
        exempt_ids = json.loads(community.get_chat_setting(cid, "filter_exempt_users", "[]"))
        filter_exempt = uid in {int(value) for value in exempt_ids if str(value).isdigit()}
    except Exception:
        filter_exempt = False

    # Configurable maximum message length (0 means disabled).
    try:
        max_length = int(community.get_chat_setting(cid, "message_max_length", "0") or "0")
    except (TypeError, ValueError):
        max_length = 0
    if 0 < max_length <= 4000 and len(raw) > max_length and not filter_exempt and not await _chat_admin(message):
        try:
            await message.delete()
            await message.answer(f"✂️ Сообщение удалено: лимит длины — {max_length} символов.")
            community.log_moderation(cid, 0, uid, "message_length_delete", f"{len(raw)} символов; лимит {max_length}")
        except Exception:
            log.exception("Could not remove overlong message")
        return

    # Optional free automoderation toggles configured by group admins. Avoid
    # Telegram admin API calls on ordinary messages when both toggles are off.
    try:
        antiflood_on = community.get_chat_setting(cid, "antiflood", "0") == "1"
        antilink_on = community.get_chat_setting(cid, "antilink", "0") == "1"
        anticaps_on = community.get_chat_setting(cid, "anticaps", "0") == "1"
        try:
            blocked_words = json.loads(community.get_chat_setting(cid, "blocked_words", "[]"))
        except Exception:
            blocked_words = []
        if blocked_words and not filter_exempt and not await _chat_admin(message):
            folded_text = raw.casefold()
            matched_word = next((str(w) for w in blocked_words if str(w).casefold() and str(w).casefold() in folded_text), None)
            if matched_word:
                try:
                    await message.delete()
                    await message.answer("🚫 Сообщение удалено: сработал фильтр запрещённых слов.")
                    community.log_moderation(cid, 0, uid, "blocked_word_delete", matched_word)
                except Exception:
                    log.exception("Could not remove blocked-word message")
                return
        if antiflood_on or antilink_on or anticaps_on:
            is_admin = await _chat_admin(message)
            if not is_admin and not filter_exempt and antiflood_on:
                from time import time as _now
                stamp = _now()
                key = (cid, uid)
                recent = [(t, txt) for t, txt in _recent_group_messages.get(key, []) if stamp - t <= 5]
                recent.append((stamp, raw.casefold()))
                _recent_group_messages[key] = recent[-8:]
                if len(recent) >= 6:
                    from datetime import datetime, timedelta, timezone
                    await message.bot.restrict_chat_member(cid, uid, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc) + timedelta(minutes=1))
                    community.log_moderation(cid, 0, uid, "antiflood_mute", "6 сообщений за 5 секунд")
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    await message.answer(f"🚦 {html.escape(message.from_user.full_name)} ограничен на 1 минуту за флуд.")
                    _recent_group_messages[key] = []
                    return
            letters = [ch for ch in raw if ch.isalpha()]
            try:
                caps_min_letters = max(5, min(100, int(community.get_chat_setting(cid, "anticaps_min_letters", "12"))))
                caps_percent = max(50, min(100, int(community.get_chat_setting(cid, "anticaps_percent", "75"))))
            except (TypeError, ValueError):
                caps_min_letters, caps_percent = 12, 75
            if not is_admin and not filter_exempt and anticaps_on and len(letters) >= caps_min_letters and sum(ch.isupper() for ch in letters) / max(1, len(letters)) >= caps_percent / 100:
                try:
                    await message.delete()
                    await message.answer("🔠 Сообщение удалено: слишком много CAPS.")
                    community.log_moderation(cid, 0, uid, "anticaps_delete", f"Более {caps_percent}% заглавных букв; минимум {caps_min_letters}")
                except Exception:
                    log.exception("Could not remove CAPS message")
                return
            if not is_admin and not filter_exempt and antilink_on and any(token in raw.casefold() for token in ("http://", "https://", "t.me/", "www.")):
                try:
                    await message.delete()
                    await message.answer("🔗 Ссылки в этой группе запрещены.")
                    community.log_moderation(cid, 0, uid, "antilink_delete", "Ссылка удалена")
                except Exception:
                    log.exception("Could not remove link")
                return
    except Exception:
        log.exception("Optional automoderation failed")

    # Reply-based moderation without slash commands. Only current group admins
    # can trigger actions; the replied-to message identifies the target.
    mod_parts = raw.split(maxsplit=2)
    mod_action = mod_parts[0].casefold().strip(".,!?:;") if mod_parts else ""
    mod_aliases = {
        "бан": "ban", "забанить": "ban", "банить": "ban",
        "мут": "mute", "замутить": "mute", "варн": "warn", "пред": "warn",
        "предупреждение": "warn", "размут": "unmute", "снятьмут": "unmute",
        "разбан": "unban", "разблокировать": "unban",
        "репорт": "report", "репортить": "report", "жалоба": "report",
    }
    if mod_action in mod_aliases:
        if mod_aliases[mod_action] == "report":
            target = await _target_from_reply(message)
            if not target:
                await message.answer("Ответь на сообщение и напиши: репорт причина")
                return
            reason = " ".join(mod_parts[1:]).strip() or "Причина не указана"
            report_text = (f"🚨 <b>Жалоба в группе</b>\nЧат: {html.escape(message.chat.title or str(cid))} (<code>{cid}</code>)\n"
                           f"На: {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
                           f"От: {html.escape(message.from_user.full_name)} (<code>{uid}</code>)\nПричина: {html.escape(reason)}")
            try:
                admins = await message.bot.get_chat_administrators(cid)
                sent = 0
                for admin in admins:
                    if admin.user.is_bot or admin.user.id == uid: continue
                    try:
                        await message.bot.send_message(admin.user.id, report_text, parse_mode="HTML")
                        sent += 1
                    except Exception: pass
                await message.answer("✅ Жалоба отправлена администраторам." if sent else "⚠️ Жалоба отмечена, но бот не смог написать администраторам в личку. Им нужно сначала открыть бота.")
            except Exception:
                log.exception("Report forwarding failed")
                await message.answer("Не удалось отправить жалобу администраторам.")
            return
        if not await _chat_admin(message):
            await message.answer("⛔ Модерировать чат могут только администраторы.")
            return
        target = await _target_from_reply(message)
        if not target:
            await message.answer("Ответь на сообщение участника словом «бан», «мут 10», «варн причина», «размут» или «разбан».")
            return
        if target.id == uid or target.id == (await message.bot.get_me()).id:
            await message.answer("Нельзя применить это действие к самому себе или боту.")
            return
        try:
            target_member = await message.bot.get_chat_member(cid, target.id)
            if target_member.status in {"administrator", "creator"}:
                await message.answer("🛡️ Нельзя модерировать администратора или владельца чата.")
                return
        except Exception:
            log.exception("Could not verify target member status")
            await message.answer("Не удалось проверить роль участника. Действие отменено для безопасности.")
            return
        action = mod_aliases[mod_action]
        reason = "Нарушение правил"
        try:
            if action == "ban":
                reason = " ".join(mod_parts[1:]).strip() or reason
                await message.bot.ban_chat_member(cid, target.id)
                community.log_moderation(cid, uid, target.id, "ban", reason)
                await message.answer(f"🚫 {html.escape(target.full_name)} заблокирован. Причина: {html.escape(reason)}")
            elif action == "unban":
                await message.bot.unban_chat_member(cid, target.id, only_if_banned=True)
                community.log_moderation(cid, uid, target.id, "unban")
                await message.answer(f"✅ Блокировка {html.escape(target.full_name)} снята.")
            elif action == "mute":
                from datetime import datetime, timedelta, timezone
                duration = 10
                rest = raw[len(mod_parts[0]):].strip()
                parsed = _parse_duration_minutes(rest)
                if parsed is not None:
                    duration = parsed
                else:
                    import re
                    match = re.match(r"^((?:\d+\s*(?:дней|дня|дн|день|д|часов|часа|час|ч|минуты|минута|минут|мин|м|hr|h|d|min|m)\s*)+)(.*)$", rest, re.I)
                    if match:
                        parsed = _parse_duration_minutes(match.group(1).strip())
                        if parsed is not None:
                            duration = parsed
                            reason = match.group(2).strip() or reason
                    elif rest:
                        reason = rest
                await message.bot.restrict_chat_member(cid, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc)+timedelta(minutes=duration))
                community.log_moderation(cid, uid, target.id, "mute", reason)
                days, rem = divmod(duration, 1440)
                hours, mins = divmod(rem, 60)
                duration_text = (f"{days} д " if days else "") + (f"{hours} ч " if hours else "") + (f"{mins} мин" if mins or not (days or hours) else "")
                await message.answer(f"🔇 {html.escape(target.full_name)} получил мут на {duration_text.strip()}. Причина: {html.escape(reason)}")
            elif action == "unmute":
                await message.bot.restrict_chat_member(cid, target.id, permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
                community.log_moderation(cid, uid, target.id, "unmute")
                await message.answer(f"🔊 Ограничения сняты с {html.escape(target.full_name)}.")
            elif action == "warn":
                reason = " ".join(mod_parts[1:]).strip() or reason
                count = community.add_warning(cid, target.id, uid, reason)
                await message.answer(f"⚠️ {html.escape(target.full_name)} получил предупреждение ({count}/3). Причина: {html.escape(reason)}")
                if count >= 3:
                    await message.bot.ban_chat_member(cid, target.id, revoke_messages=False)
                    community.log_moderation(cid, uid, target.id, "auto_ban_3_warns", reason)
                    await message.answer("🚫 Автобан: накоплено 3 предупреждения.")
        except Exception:
            log.exception("Reply moderation action failed: %s", action)
            await message.answer("❌ Не удалось выполнить действие. Проверь, что бот — администратор и у него есть право блокировать участников.")
        return
    if not raw.startswith("/"):
        filter_reply = community.match_chat_filter(cid, raw)
        if filter_reply:
            await message.answer(filter_reply)
            return

    username = message.from_user.username or message.from_user.full_name

    # PULSAR's original social RPG: a 24-hour world event, collectible cards, packs and a sell-back market.
    if command in {"мир pulsar", "мир", "событие", "ивент", "ивент дня", "событие дня"}:
        event = community.world_event(cid)
        left = max(0, event["ends_at"] - int(time.time()))
        await message.answer(
            f"🌌 <b>МИР РАЙОНОВ · СОБЫТИЕ ДНЯ</b>\n\n"
            f"{event['emoji']} <b>{html.escape(event['title'])}</b>\n{html.escape(event['desc'])}\n\n"
            f"🎁 Бонус к поиску: <b>+{event['bonus']} коинов</b>\n"
            f"⏳ До смены события: <b>{left//3600} ч {(left%3600)//60} мин</b>\n\n"
            "Напиши «поиск в мире», чтобы найти карту (раз в 30 минут). «альбом» — коллекция, «пак карт» — купить 3 случайные карты за 35 коинов, «рынок карт» — цены.\n"
            "🪙 Это игровая валюта сообщества, не реальные деньги.", parse_mode="HTML")
        return
    if command in {"поиск в мире", "исследовать", "искать карту", "поиск карты"}:
        card, wait, event = community.world_search(cid, uid, username)
        if card is None:
            await message.answer(f"⏳ Район ещё не обновился. Следующий поиск через {wait//60} мин.")
        else:
            await send_community_game_photo(message,
                f"{event['emoji']} <b>СОБЫТИЕ: {html.escape(event['title'])}</b>\n\n"
                f"Ты нашёл карту: {card['emoji']} <b>{html.escape(card['name'])}</b>\n"
                f"Редкость: <b>{html.escape(card['rarity'])}</b> · цена продажи: <b>{card['value']} коинов</b>\n"
                f"Награда за поиск: 🪙 <b>+{card['coins']}</b>\n\nНапиши «альбом», чтобы посмотреть коллекцию.")
        return
    if command in {"альбом", "мои карты", "коллекция карт", "карты"}:
        cards = community.card_album(cid, uid)
        lines = [f"{c['emoji']} <b>{html.escape(c['name'])}</b> — ×{c['quantity']} · {html.escape(c['rarity'])} · {c['value']} 🪙" for c in cards]
        count = sum(c['quantity'] for c in cards)
        await send_community_game_photo(message, "🗂 <b>АЛЬБОМ КАРТ</b>\n\n" + "\n".join(lines) + f"\n\nВсего карт: <b>{count}</b> / {len(cards)} видов\nПродажа: <code>продать карту дворовый_кот 1</code>.")
        return
    if command in {"пак карт", "купить пак", "открыть пак", "пак"}:
        ok, result = community.open_card_pack(cid, uid, username)
        if not ok:
            await message.answer(f"❌ {html.escape(result)}")
        else:
            lines = [f"{c['emoji']} <b>{html.escape(c['name'])}</b> · {c['rarity']}" for c in result['cards']]
            await send_community_game_photo(message, "🎴 <b>ПАК ОТКРЫТ!</b>\n\n" + "\n".join(lines) + f"\n\nСписано: {result['cost']} коинов. Повторы можно продать на рынке.")
        return
    if command in {"рынок карт", "цены карт", "рынок"}:
        lines = [f"{c['emoji']} <b>{html.escape(c['name'])}</b> — {c['value']} 🪙 · {html.escape(c['rarity'])}" for c in community.WORLD_CARDS.values()]
        await send_community_game_photo(message, "🏪 <b>РЫНОК КАРТ</b>\n\n" + "\n".join(lines) + "\n\nПак: 3 случайные карты за 35 коинов. Продажа: <code>продать карту ключ 2</code>.")
        return
    if command.startswith("продать карту "):
        parts = raw.split()
        card_key = parts[2].casefold() if len(parts) > 2 else ""
        try: qty = int(parts[3]) if len(parts) > 3 else 1
        except ValueError: qty = 0
        ok, value = community.sell_card(cid, uid, card_key, qty)
        await message.answer(f"✅ Продано за 🪙 {value} коинов." if ok else f"❌ {html.escape(str(value))}")
        return

    # Social mini-games use only the separate community-coin wallet, never real-money balance.
    if command in {"игры чата", "мини-игры", "игры", "игры pulsar"}:
        await message.answer(
            "🎮 <b>МИНИ-ИГРЫ</b>\n\n"
            "🪙 <b>Монетка</b>: «монетка орёл 5» или «монетка решка 5» — угадай сторону, рискнув коинами.\n"
            "✊ <b>Дуэль</b>: «камень», «ножницы» или «бумага» — победа даёт 5 коинов.\n"
            "🎲 <b>Кости</b>: «кости 5» — брось кубик; 5–6 дают выигрыш, 1–2 — проигрыш 2 коинов, остальные — ничья.\n"
            "🎰 <b>Слоты</b>: «слоты 5» — три символа, комбинации дают приз.\n"
            "🎯 <b>Число</b>: «угадай число», затем отправь число от 1 до 5; победа даёт 10 коинов.\n"
            "🧠 <b>Викторина</b>: «викторина» — первый правильный ответ получает 8 коинов.\n"
            "🏆 <b>Топ чата</b>: рейтинг по коинам.\n\n"
            "Коины — игровая валюта сообщества, не деньги и не выводятся.", parse_mode="HTML")
        return
    # Community mini-games use only community coins. Syntax is validated before any debit.
    if command.startswith("монетка "):
        parts = command.split()
        choice = parts[1] if len(parts) > 1 else ""
        try: stake = int(parts[2]) if len(parts) > 2 else 1
        except ValueError: stake = 0
        if choice not in {"орёл", "орел", "решка"} or not 1 <= stake <= 1000:
            await message.answer("🪙 Формат: «монетка орёл 5» или «монетка решка 5». Ставка 1–1000 коинов.")
            return
        wallet = community.wallet(cid, uid, username)
        if wallet["coins"] < stake:
            await message.answer(f"Не хватает коинов. Баланс: {wallet['coins']} 🪙")
            return
        community.change_coins(cid, uid, -stake, username)
        result = random.choice(["орёл", "решка"])
        if choice in {result, "орёл" if result == "орел" else result}:
            _, balance = community.change_coins(cid, uid, stake * 2, username)
            await message.answer(f"🪙 Выпало <b>{result}</b>! Победа: чистыми +{stake} 🪙. Баланс: {balance} 🪙", parse_mode="HTML")
        else:
            balance = community.wallet(cid, uid, username)["coins"]
            await message.answer(f"🪙 Выпало <b>{result}</b>. Ставка сгорела: −{stake} 🪙. Баланс: {balance} 🪙", parse_mode="HTML")
        return
    if command.startswith("кости"):
        parts = command.split()
        try: stake = int(parts[1]) if len(parts) > 1 else 1
        except ValueError: stake = 0
        wallet = community.wallet(cid, uid, username)
        if not 1 <= stake <= 1000 or wallet["coins"] < stake:
            await message.answer(f"Формат: «кости 5». Ставка 1–1000 коинов. Твой баланс: {wallet['coins']} 🪙")
            return
        community.change_coins(cid, uid, -stake, username)
        roll = random.randint(1, 6)
        if roll >= 5:
            _, balance = community.change_coins(cid, uid, stake * 2, username)
            result_text = f"Победа! +{stake} коинов чистыми."
        elif roll <= 2:
            balance = community.wallet(cid, uid, username)["coins"]
            result_text = f"Проигрыш −{stake} коинов."
        else:
            _, balance = community.change_coins(cid, uid, stake, username)
            result_text = "Ничья, ставка возвращена."
        await message.answer(f"🎲 Выпало <b>{roll}</b>. {result_text} Баланс: {balance} 🪙", parse_mode="HTML")
        return
    if command.startswith("слоты"):
        parts = command.split()
        try: stake = int(parts[1]) if len(parts) > 1 else 1
        except ValueError: stake = 0
        wallet = community.wallet(cid, uid, username)
        if not 1 <= stake <= 1000 or wallet["coins"] < stake:
            await message.answer(f"Формат: «слоты 5». Ставка 1–1000 коинов. Твой баланс: {wallet['coins']} 🪙")
            return
        community.change_coins(cid, uid, -stake, username)
        symbols = ["🍒", "🍋", "🍇", "💎", "7️⃣"]
        spin = [random.choice(symbols) for _ in range(3)]
        if spin[0] == spin[1] == spin[2]: multiplier = 8 if spin[0] == "7️⃣" else 5
        elif len(set(spin)) == 2: multiplier = 2
        else: multiplier = 0
        payout = stake * multiplier
        if payout:
            _, balance = community.change_coins(cid, uid, payout, username)
        else:
            balance = community.wallet(cid, uid, username)["coins"]
        await message.answer(f"🎰 {' | '.join(spin)}\nСтавка: {stake} 🪙 · Выплата: {payout} 🪙 · Баланс: {balance} 🪙", parse_mode="HTML")
        return

    if command in {"орёл", "орел", "решка"}:
        choice = "орёл" if command in {"орёл", "орел"} else "решка"
        result = random.choice(["орёл", "решка"])
        if choice == result:
            _, balance = community.change_coins(cid, uid, 3, username)
            await message.answer(f"🪙 Монетка: <b>{result}</b>! {html.escape(message.from_user.full_name)} угадал и получает 3 коина. Баланс: {balance} 🪙", parse_mode="HTML")
        else:
            ok, balance = community.change_coins(cid, uid, -1, username)
            await message.answer(f"🪙 Выпало <b>{result}</b>. Не угадал! {'Потерян 1 коин.' if ok else 'Коины не списаны — баланс уже 0.'} Баланс: {balance} 🪙", parse_mode="HTML")
        return
    if command in {"камень", "ножницы", "бумага"}:
        bot_choice = random.choice(["камень", "ножницы", "бумага"])
        if command == bot_choice:
            outcome, delta = "Ничья!", 0
        elif (command, bot_choice) in {("камень", "ножницы"), ("ножницы", "бумага"), ("бумага", "камень")}:
            outcome, delta = "Ты победил и получаешь 5 коинов!", 5
        else:
            outcome, delta = "На этот раз победил бот. Попробуй ещё!", 0
        _, balance = community.change_coins(cid, uid, delta, username)
        await message.answer(f"✊ Ты: <b>{command}</b>\n🤖 Бот: <b>{bot_choice}</b>\n{outcome}\nБаланс: {balance} 🪙", parse_mode="HTML")
        return
    if command in {"викторина", "вопрос дня", "квиз"}:
        q, answers, display = random.choice(QUIZ_QUESTIONS)
        active_quizzes[cid] = {"answers": answers, "display": display, "starter": uid}
        await message.answer(f"🧠 <b>ВИКТОРИНА</b>\n\n{html.escape(q)}\n\nКто первым ответит правильно — получит 8 коинов!", parse_mode="HTML")
        return
    if command == "угадай число":
        number = random.randint(1, 5)
        active_quizzes[cid] = {"number": number, "starter": uid}
        await message.answer("🔢 Я загадала число от 1 до 5. Напиши число в чат! Первое правильное угадывание — 10 коинов.")
        return
    if cid in active_quizzes and command:
        game = active_quizzes[cid]
        if "number" in game and command.isdigit() and 1 <= int(command) <= 5:
            guess = int(command)
            if guess == game["number"]:
                active_quizzes.pop(cid, None)
                _, balance = community.change_coins(cid, uid, 10, username)
                await message.answer(f"🎉 Верно, это было {guess}! {html.escape(message.from_user.full_name)} получает 10 коинов. Баланс: {balance} 🪙", parse_mode="HTML")
            else:
                await message.answer("Не угадал! Попробуй ещё раз — число от 1 до 5.")
            return
        if "answers" in game and command in game["answers"]:
            active_quizzes.pop(cid, None)
            _, balance = community.change_coins(cid, uid, 8, username)
            await message.answer(f"🧠 Правильно: <b>{html.escape(game['display'])}</b>! Победитель: {html.escape(message.from_user.full_name)} — +8 коинов. Баланс: {balance} 🪙", parse_mode="HTML")
            return

    if command in {"экономика", "экономика чата", "помощь чата", "команды чата"}:
        await message.answer("🌌 <b>Экономика чата</b>\n\n<b>Собрать</b> — найти карту события (раз в 2 часа); «собрать ресурс» — обычный ресурс (раз в 4 часа)\n<b>Инвентарь</b> — твои предметы\n<b>Магазин</b> — цены предметов\n<b>Купить дерево 2</b> — покупка\n<b>Продать рыба 3</b> — продажа\n<b>Коины</b> — баланс чата\n<b>Ежедневный бонус</b> — награда раз в сутки\n<b>Топ чата</b> — рейтинг\n<b>Игры чата</b> — монетка, дуэль, кости, слоты, викторина и угадай число\n<b>Мир районов</b> — случайное событие на 24 часа; «поиск в мире», «альбом», «пак карт», «рынок карт», «продать карту ключ 1»\n\nМодерация без слеш-команд: ответь на сообщение участника и напиши «бан», «мут 10», «варн причина», «размут» или «разбан». Только администраторы чата.", parse_mode="HTML")
    elif command in {"б", "баланс", "кошелёк", "кошелек"}:
        stats = get_profile_stats(uid)
        await message.answer(f"💰 <b>Баланс:</b> ${get_balance(uid):.2f} USD\n📈 <b>Оборот:</b> ${stats.get('turnover', 0):,.2f} USD", parse_mode="HTML")
        return
    elif command in {"коины", "баланс чата", "мой баланс чата"}:
        w = community.wallet(cid, uid, username)
        await message.answer(f"🪙 <b>Баланс чата</b>\nКоины: <b>{w['coins']}</b>\nОпыт: <b>{w['xp']}</b>\nУровень: <b>{1 + w['xp']//100}</b>", parse_mode="HTML")
    elif command in {"собрать", "добыть", "поиск в мире", "поиск"}:
        card, wait, event = community.world_search(cid, uid, username)
        if card is None:
            await message.answer(f"⏳ Событие «{html.escape(event['title'])}»: следующий сбор через {wait//3600} ч {(wait%3600)//60} мин.")
        else:
            await message.answer(f"{event['emoji']} <b>{html.escape(event['title'])}</b>\n🎴 Ты нашёл {card['emoji']} <b>{html.escape(card['name'])}</b> ({html.escape(card['rarity'])}) и получил 🪙 {card['coins']} коинов!\nСбор доступен раз в 2 часа. Напиши «альбом», чтобы увидеть карты.", parse_mode="HTML")
        return
    elif command in {"собрать ресурс", "добыть ресурс"}:
        result, wait = community.collect(cid, uid, username)
        if result is None:
            await message.answer(f"⏳ Следующий сбор ресурса через {wait//3600} ч {(wait%3600)//60} мин.")
        else:
            await message.answer(f"🎒 Ты нашёл {result['emoji']} <b>{result['item']}</b> ({result['rarity']}) и получил 🪙 {result['coins']} коинов!\nПродать можно командой «продать {result['item']}».", parse_mode="HTML")
        return
    elif command in {"инвентарь", "мои предметы", "рюкзак"}:
        inv = community.inventory(cid, uid)
        w = community.wallet(cid, uid, username)
        lines = [f"{community.ITEMS[item]['emoji']} {html.escape(item)} × {qty}" for item,qty in inv]
        await message.answer("🎒 <b>Инвентарь</b>\n" + ("\n".join(lines) if lines else "Пока пусто — напиши «собрать».") + f"\n\n🪙 Баланс: {w['coins']}", parse_mode="HTML")
    elif command == "магазин":
        lines = [f"{v['emoji']} <b>{name}</b> — купить {v['buy']} 🪙 · продать {v['sell']} 🪙 ({v['rarity']})" for name,v in community.ITEMS.items()]
        await message.answer("🏪 <b>Магазин чата</b>\n" + "\n".join(lines) + "\n\nПокупка: «купить дерево 2».", parse_mode="HTML")
    elif command.startswith("продать ") or command.startswith("продать:"):
        parts = raw.replace(":", " ").split()
        item = parts[1] if len(parts)>1 else ""
        try: qty = int(parts[2]) if len(parts)>2 else 1
        except ValueError: qty = 0
        ok, value = community.sell_item(cid, uid, item, qty)
        await message.answer(f"✅ Продано! Получено 🪙 {value}." if ok else f"❌ {value}")
    elif command.startswith("купить ") or command.startswith("купить:"):
        parts = raw.replace(":", " ").split()
        item = parts[1] if len(parts)>1 else ""
        try: qty = int(parts[2]) if len(parts)>2 else 1
        except ValueError: qty = 0
        ok, value = community.buy_item(cid, uid, item, qty, username)
        await message.answer(f"✅ Покупка выполнена за 🪙 {value}." if ok else f"❌ {value}")
    elif command in {"ежедневный бонус", "дейлик", "ежедневка"}:
        ok, reward = community.daily(cid, uid, username)
        await message.answer(f"🎁 Ты получил 🪙 {reward} коинов!" if ok else "⏳ Ежедневный бонус уже получен. Возвращайся завтра.")
    elif command in {"топ чата", "топ коинов", "богачи чата", "топ", "топ все", "топ всего", "топ вся", "топ весь", "топ неделя", "топ недели", "топ месяц", "топ месяца"}:
        if "недел" in command:
            feature, period, title = "top_week", "week", "Топ чата за 7 дней"
            top = community.activity_leaderboard(cid, period="week")
            lines = [f"{i}. {html.escape(row['username'] or str(row['user_id']))} — 💬 {row['messages']} сообщ." for i,row in enumerate(top,1)]
        elif "месяц" in command or "месяца" in command:
            feature, period, title = "top_month", "month", "Топ чата за 30 дней"
            top = community.activity_leaderboard(cid, period="month")
            lines = [f"{i}. {html.escape(row['username'] or str(row['user_id']))} — 💬 {row['messages']} сообщ." for i,row in enumerate(top,1)]
        elif command in {"топ все", "топ всего", "топ вся", "топ весь"}:
            feature, title = "top_all", "Общий топ чата по коинам"
            top = community.leaderboard(cid)
            lines = [f"{i}. {html.escape(row['username'] or str(row['user_id']))} — 🪙 {row['coins']} · XP {row['xp']}" for i,row in enumerate(top,1)]
        else:
            feature, title = "top_chat", "Топ чата по коинам"
            top = community.leaderboard(cid)
            lines = [f"{i}. {html.escape(row['username'] or str(row['user_id']))} — 🪙 {row['coins']} · XP {row['xp']}" for i,row in enumerate(top,1)]
        if "недел" in command or "месяц" in command or "месяца" in command:
            # Activity ranking cards reflect the selected period and current user's exact position.
            period_label = "ЗА 7 ДНЕЙ" if "недел" in command else "ЗА 30 ДНЕЙ"
            with community.connect() as db:
                span_seconds = 7 * 86400 if "недел" in command else 30 * 86400
                cutoff = int(time.time()) - span_seconds
                all_rows = [dict(r) for r in db.execute(
                    "SELECT user_id, MAX(username) AS username, COUNT(*) AS messages FROM community_activity_events WHERE chat_id=? AND created_at>=? GROUP BY user_id ORDER BY messages DESC, user_id ASC",
                    (cid, cutoff),
                ).fetchall()]
            await send_live_chat_rank_card(message, title=title, rows=all_rows, metric_label="Сообщения",
                                           metric_key="messages", period_label=period_label, xp_key=None)
        else:
            all_rows = community.leaderboard(cid, 100000)
            await send_live_chat_rank_card(message, title=title, rows=all_rows, metric_label="Коины",
                                           metric_key="coins", period_label="ВСЁ ВРЕМЯ", xp_key="xp")
    else:
        # Custom command lookup happens after built-in handlers, so aliases cannot shadow core commands.
        settings_payload = community.export_chat_settings(cid).get("settings", {})
        custom_reply = settings_payload.get("custom_command:" + command)
        if custom_reply:
            await message.reply(str(custom_reply)[:1000])
            return
        raise SkipHandler()


@dp.message(
    F.text,
    ~F.text.startswith("/"),
    lambda message: not (
        message.from_user and (message.from_user.id in pending_withdrawal_amount or message.from_user.id in pending_deposit_amount)
    ),
)
async def chat_shortcuts(message: Message):
    """Natural-language chat commands; no slash prefix required."""
    if not message.from_user or not message.text:
        return
    command = message.text.strip().casefold().split("@", 1)[0].strip(" .,!?:;")
    if command in CHAT_PROFILE_WORDS:
        text = f"{CUSTOM_EMOJI['profile']} <b>ТВОЙ ПРОФИЛЬ</b>\n" + profile_text(message.from_user.id, message.from_user.full_name)
        if message.chat.type == "private":
            sent = await send_feature_card_for_chat(message.bot, message.chat.id, "profile", caption=text, reply_markup=back_menu(), parse_mode="HTML")
            if not sent:
                await message.answer(text, reply_markup=back_menu(), parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=back_menu(), parse_mode="HTML")
    elif command in CHAT_GAME_WORDS:
        if message.chat.type in {"group", "supergroup"}:
            await message.answer(
                "🎮 <b>МИНИ-ИГРЫ</b>\n\n"
                "🪙 Монетка: «орёл» или «решка»\n"
                "✊ Дуэль: «камень», «ножницы» или «бумага»\n"
                "🧠 Викторина: «викторина» — первый правильный ответ получает 8 коинов\n"
                "🔢 Число: «угадай число» — угадай число от 1 до 5\n"
                "🌌 Все награды — только коины сообщества, не реальные деньги. Напиши «игры чата» для правил.",
                parse_mode="HTML")
        else:
            text = f"{CUSTOM_EMOJI['games']} <b>ВЫБЕРИ ИГРУ</b>\nВыбери игру, затем сумму ставки — раунд пройдёт прямо в чате."
            sent = await send_feature_card_for_chat(message.bot, message.chat.id, "games", caption=text, reply_markup=games_menu(), parse_mode="HTML")
            if not sent:
                await message.answer(text, reply_markup=games_menu(), parse_mode="HTML")
    elif command in {"правила коллекции", "как играть в карточки", "инструкция карточки", "карточки помощь"}:
        await message.answer(
            "🎴 <b>КАК ИГРАТЬ В КОЛЛЕКЦИЮ PULSAR</b>\n\n"
            "1. <b>Поиск</b> / «собрать» — находи карту и получай коины-бонусы; действует перерыв между поисками.\n"
            "2. <b>Паки</b> / «пак карт» — 3 случайные карты за 35 коинов сообщества.\n"
            "3. <b>Альбом</b> / «альбом» — показывает количество каждой карты и редкость.\n"
            "4. <b>Рынок</b> / «рынок карт» — цены продажи. Продажа: <code>продать карту дворовый_кот 1</code>.\n"
            "5. <b>Событие</b> / «мир» — активный районный бонус.\n\n"
            "Редкости: обычная → редкая → эпическая → легендарная → мифическая. Более редкие карты встречаются реже и стоят дороже.\n"
            "Важно: коины — только внутриигровые, они не являются деньгами и не выводятся. Карточная экономика полностью отделена от казино.",
            parse_mode="HTML", reply_markup=community_menu())
    elif command in {"сообщество", "мир", "коллекция", "ивент", "событие", "мир и коллекция", "центр сообщества"} and message.chat.type == "private":
        text = (f"{CUSTOM_EMOJI['globe']} <b>МИР И СООБЩЕСТВО PULSAR</b>\n\n"
                "Собирай карточки, исследуй события, открывай паки и торгуй дубликатами. Это отдельная игровая экономика, не баланс казино.\n\n"
                + _community_wallet_text(message.chat.id, message.from_user.id, message.from_user.username or message.from_user.full_name))
        await answer_with_feature_card(message, "community", text, reply_markup=community_menu())
    elif command in {"собрать", "поиск в мире", "искать карту", "поиск карты"} and message.chat.type == "private":
        card, wait, event = community.world_search(message.chat.id, message.from_user.id, message.from_user.username or message.from_user.full_name)
        if card is None:
            await message.answer(f"{CUSTOM_EMOJI['timer']} До следующего поиска: {wait//3600} ч {(wait%3600)//60} мин.\nСобытие: <b>{html.escape(event['title'])}</b>", parse_mode="HTML", reply_markup=community_menu())
        else:
            await message.answer(
                f"{CUSTOM_EMOJI['celebration']} <b>КАРТА НАЙДЕНА</b>\n\n{card['emoji']} <b>{html.escape(card['name'])}</b>\n"
                f"Редкость: {html.escape(card['rarity'])} · цена: {card['value']} коинов\nНаграда: +{card['coins']} коинов\n"
                f"Событие: {html.escape(event['title'])}\n\nСледующий поиск доступен через 2 часа.",
                parse_mode="HTML", reply_markup=community_menu())
    elif command in {"альбом", "мои карты", "карты", "коллекция карт"} and message.chat.type == "private":
        await answer_with_feature_card(message, "collection", _album_text(message.chat.id, message.from_user.id), reply_markup=community_menu())
    elif command in {"пак", "пак карт", "открыть пак", "купить пак"} and message.chat.type == "private":
        ok, result = community.open_card_pack(message.chat.id, message.from_user.id, message.from_user.username or message.from_user.full_name)
        if not ok:
            await message.answer(f"{CUSTOM_EMOJI['warning']} {html.escape(result)}", reply_markup=community_menu(), parse_mode="HTML")
        else:
            lines = [f"{c['emoji']} <b>{html.escape(c['name'])}</b> · {html.escape(c['rarity'])}" for c in result['cards']]
            text = f"{CUSTOM_EMOJI['gift']} <b>ПАК ОТКРЫТ</b>\n\n" + "\n".join(lines) + f"\n\nСписано: {result['cost']} коинов."
            await answer_with_feature_card(message, "collection", text, reply_markup=community_menu())
    elif command in {"рынок карт", "цены карт", "рынок"} and message.chat.type == "private":
        lines = [f"{c['emoji']} <b>{html.escape(c['name'])}</b> — {c['value']} коинов · {html.escape(c['rarity'])}" for c in community.WORLD_CARDS.values()]
        text = f"{CUSTOM_EMOJI['shopping']} <b>РЫНОК КАРТ PULSAR</b>\n\n" + "\n".join(lines) + "\n\nПак содержит 3 карты и стоит 35 коинов. Продажа: <code>продать карту ключ 2</code>."
        await answer_with_feature_card(message, "collection", text, reply_markup=community_menu())
    elif command in {"коины", "мой баланс сообщества", "сообщество баланс"} and message.chat.type == "private":
        await answer_with_feature_card(message, "wallet", _community_wallet_text(message.chat.id, message.from_user.id, message.from_user.username or message.from_user.full_name), reply_markup=community_menu())
    elif command == "помощь" and message.chat.type == "private":
        await help_cmd(message)
    elif command in CHAT_DEPOSIT_WORDS:
        if message.chat.type == "private":
            pending_deposit_amount.add(message.from_user.id)
            text = "💳 Напиши сумму в USD числом (например, 5 или 12.50). Минимум $0.50, максимум $500. Для отмены напиши «отмена»."
            sent = await send_feature_card_for_chat(message.bot, message.chat.id, "deposit", caption=text, parse_mode=None)
            if not sent:
                await message.answer(text)
        else:
            await message.answer("💳 Пополнение доступно в личке бота: открой бота и напиши «пополнить».")
    elif command in CHAT_WITHDRAW_WORDS:
        text = f"{CUSTOM_EMOJI['wallet']} <b>ВЫВОД СРЕДСТВ</b>\nВыбери сумму в USDT. Для выплаты потребуется числовой Telegram ID получателя."
        if message.chat.type == "private":
            sent = await send_feature_card_for_chat(message.bot, message.chat.id, "withdraw", caption=text, reply_markup=withdrawal_menu(), parse_mode="HTML")
            if not sent:
                await message.answer(text, reply_markup=withdrawal_menu(), parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
    elif command in CHAT_WALLET_WORDS:
        stats = get_profile_stats(message.from_user.id)
        text = (f"{CUSTOM_EMOJI['wallet']} <b>Баланс:</b> ${get_balance(message.from_user.id):.2f} USD\n"
                f"{CUSTOM_EMOJI['trend_up']} <b>Оборот:</b> ${stats.get('turnover', 0):,.2f} USD")
        if message.chat.type == "private":
            sent = await send_feature_card_for_chat(message.bot, message.chat.id, "wallet", caption=text, reply_markup=wallet_menu(), parse_mode="HTML")
            if not sent:
                await message.answer(text, reply_markup=wallet_menu(), parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")


def stake_menu(game_key: str):
    kb = InlineKeyboardBuilder()
    for i, amount in enumerate(BET_OPTIONS):
        cents = int(amount * 100)
        kb.button(text=f"${amount:.2f}", callback_data=f"stake_{game_key}_{cents}")
    kb.button(text="Назад к играм", callback_data="games")
    kb.adjust(3, 3, 1)
    return kb.as_markup()


def casino_daily_event() -> dict:
    """Deterministic UTC daily event; the same event is shown to all users that day."""
    events = [
        {"name": "Ночь кубиков", "game": "dice", "bonus": Decimal("0.20"), "desc": "+0.20x к выигрышной выплате в кубике"},
        {"name": "Футбольная лихорадка", "game": "football", "bonus": Decimal("0.20"), "desc": "+0.20x к выигрышной выплате в футболе"},
        {"name": "Бильярдный турнир", "game": "billiards", "bonus": Decimal("0.25"), "desc": "+0.25x к выигрышной выплате в бильярде"},
        {"name": "Красное или чёрное", "game": "roulette", "bonus": Decimal("0.15"), "desc": "+0.15x к выигрышной выплате в рулетке"},
        {"name": "Карточный стол", "game": "blackjack", "bonus": Decimal("0.20"), "desc": "+0.20x к выигрышной выплате в блэкджеке"},
        {"name": "Ракета PULSAR", "game": "crash", "bonus": Decimal("0.20"), "desc": "+0.20x к выигрышной выплате в Crash"},
    ]
    day = datetime.now(timezone.utc).date().toordinal()
    return events[day % len(events)]


def payout_multiplier(game_key: str, value: int) -> Decimal:
    # Gross multipliers include return of stake. All outcomes are settled once by the ledger.
    if game_key == "dice": return Decimal("5.00") if value == 6 else Decimal("0.00")
    if game_key == "football": return Decimal("1.80") if value >= 3 else Decimal("0.00")
    if game_key == "basketball": return Decimal("1.80") if value >= 4 else Decimal("0.00")
    if game_key == "darts": return Decimal("4.00") if value == 6 else Decimal("0.00")
    if game_key == "bowling": return Decimal("3.00") if value == 6 else Decimal("0.00")
    if game_key == "billiards": return Decimal("2.50") if value == 1 else Decimal("0.00")
    if game_key == "roulette": return Decimal("1.90") if value in (1, 2) else Decimal("0.00")
    if game_key == "blackjack": return {1: Decimal("2.50"), 2: Decimal("1.00"), 3: Decimal("0.00")}.get(value, Decimal("0.00"))
    if game_key == "baccarat": return Decimal("1.90") if value == 1 else Decimal("0.00")
    if game_key == "crash": return {1: Decimal("1.20"), 2: Decimal("1.50"), 3: Decimal("2.00"), 4: Decimal("3.00"), 5: Decimal("0.00")}.get(value, Decimal("0.00"))
    if game_key == "mines": return Decimal("2.00") if value == 1 else Decimal("0.00")
    return Decimal("0.00")


async def play_game(message: Message, game_key: str, player_id: int | None = None, stake: Decimal | None = None):
    info = GAME_INFO[game_key]
    owner_id = player_id or (message.from_user.id if message.from_user else None)
    if owner_id is None:
        await message.answer("Не удалось определить игрока. Запусти игру в личном чате или нажми кнопку игры.")
        return
    if stake is None:
        await message.answer(
            f"{info['emoji']} <b>{info['name'].upper()}</b>\n{html.escape(info['desc'])}\nВыбери ставку. Баланс: <b>${get_balance(owner_id):.2f}</b>",
            reply_markup=stake_menu(game_key), parse_mode="HTML")
        return
    wager_id = uuid.uuid4().hex
    try:
        accepted = place_bet(wager_id, owner_id, game_key, stake)
    except Exception:
        log.exception("Could not place wager")
        await message.answer("Не удалось создать раунд. Баланс не должен измениться.")
        return
    if not accepted:
        await message.answer(f"Недостаточно средств или ставка уже обработана. Баланс: ${get_balance(owner_id):.2f}", reply_markup=stake_menu(game_key))
        return
    event = casino_daily_event()
    try:
        await message.answer(f"{info['emoji']} <b>{info['name'].upper()}</b>\nСтавка: <b>${stake:.2f}</b>\n🎉 Ивент дня: <b>{html.escape(event['name'])}</b>", parse_mode="HTML")
        if game_key in {"dice", "football", "basketball", "darts", "bowling"}:
            result = await message.answer_dice(emoji=info["emoji"])
            value = result.dice.value
            result_label = f"результат анимации: {value}"
        else:
            # Secure server-side RNG for games without a native Telegram dice animation.
            value = secrets.randbelow(6) + 1
            if game_key == "roulette":
                result_label = f"шар остановился на {'красном' if value in (1, 2) else 'чёрном' if value in (3, 4) else 'зелёном (0)'} секторе"
            elif game_key == "billiards":
                result_label = "шар забит в лузу!" if value == 1 else f"удар мимо (позиция {value})"
            elif game_key == "blackjack":
                result_label = {1: "блэкджек! 21 очко", 2: "ничья с дилером", 3: "дилер выиграл"}.get(value if value <= 3 else 3, "дилер выиграл")
                value = value if value <= 3 else 3
            elif game_key == "baccarat":
                result_label = "победил игрок" if value == 1 else "победил банкир" if value in (2, 3) else "ничья"
                value = 1 if value == 1 else 2
            elif game_key == "crash":
                value = secrets.choice([1, 2, 3, 4, 5])
                result_label = {1: "забрал на x1.20", 2: "забрал на x1.50", 3: "забрал на x2.00", 4: "забрал на x3.00", 5: "РАКЕТА КРАШНУЛАСЬ"}[value]
            else:
                result_label = "клетка безопасна" if value == 1 else "БАХ! МИНА"
    except Exception:
        log.exception("Game animation/result failed; refunding wager")
        refund_bet(wager_id)
        await message.answer("Не удалось завершить раунд. Ставка возвращена на баланс.")
        return
    multiplier = payout_multiplier(game_key, value)
    if game_key == event["game"] and multiplier > 0:
        multiplier += event["bonus"]
    settled = settle_bet(wager_id, value, multiplier)
    if settled is None:
        await message.answer("Раунд уже обработан или недоступен. Обратись в поддержку, если баланс выглядит неверно.")
        return
    outcome = "ПОБЕДА" if settled["payout"] > 0 else "ПРОИГРЫШ"
    await message.answer(
        f"🏆 <b>{outcome} — {info['name'].upper()}</b>\n{html.escape(result_label)}\n"
        f"Ставка: <b>${stake:.2f}</b>\nВыплата: <b>${settled['payout']:.2f}</b>\nКешбэк: <b>${settled['cashback']:.2f}</b>\n"
        f"Баланс: <b>${settled['balance']:.2f}</b>\nКоэффициент: <b>{multiplier}x</b>", reply_markup=back_menu(), parse_mode="HTML")


@dp.message(Command(*GAME_EMOJIS.keys()))
async def play_telegram_game(message: Message):
    command = (message.text or "").split()[0].split("@", 1)[0].lstrip("/").lower()
    if command in GAME_INFO:
        await play_game(message, command)


def add_to_group_menu(bot_username: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить PULSAR • ПОМОЩНИК СООБЩЕСТВ в группу", url=f"https://t.me/{bot_username}?startgroup=play")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(Command("weeklytop"))
async def weekly_top_command(message: Message):
    rows = community.global_weekly_leaderboard(10)
    if not rows:
        await message.answer("🏆 Глобальный недельный топ пока пуст. Пиши в группах, где есть бот — активность суммируется между всеми группами.")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, row in enumerate(rows, 1):
        group_count = len([x for x in str(row.get("chats", "")).split(",") if x])
        label = "@" + row["username"] if row.get("username") else f"ID {row['user_id']}"
        lines.append(f"{medals.get(i, str(i)+'.')} {html.escape(label)} — {row['points']} очк. · {row['messages']} сообщ. · {group_count} групп")
    await message.answer("🌍 <b>ГЛОБАЛЬНЫЙ ТОП НЕДЕЛИ</b>\nРейтинг общий для всех групп, где работает бот. Очки активности начисляются не чаще раза в 15 секунд на пользователя.\n\n" + "\n".join(lines), parse_mode="HTML")


@dp.message(Command("weeklydraw"))
async def weekly_draw_command(message: Message):
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer("⛔ Команда доступна только владельцам бота из ADMIN_TELEGRAM_IDS.")
        return
    week, winners, created = community.finalize_weekly_awards(
        cash_prizes=tuple(os.getenv(f"WEEKLY_CASH_PRIZE_{i}", default) for i, default in enumerate(("50 USD", "25 USD", "10 USD"), 1)),
        nft_prizes=tuple(os.getenv(f"WEEKLY_NFT_PRIZE_{i}", default) for i, default in enumerate(("NFT-подарок #1", "NFT-подарок #2", "NFT-подарок #3"), 1)),
    )
    if not winners:
        await message.answer(f"За неделю, начавшуюся {week}, нет участников в рейтинге.")
        return
    text = _weekly_awards_text(week, winners, created)
    await message.answer(text, parse_mode="HTML")
    if created:
        await _broadcast_weekly_awards(message.bot, text)


def _weekly_awards_text(week, winners, created=True):
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"{medals.get(int(w['place']), '🏅')} {html.escape(w.get('username') or str(w['user_id']))} (ID <code>{w['user_id']}</code>) — {w['points']} очк.\n   💵 {html.escape(w.get('cash_prize') or 'без денежного приза')} + 🎁 {html.escape(w.get('nft_prize') or 'без NFT-подарка')}" for w in winners]
    prefix = "🎉 <b>ИТОГИ ГЛОБАЛЬНОГО РОЗЫГРЫША</b>" if created else "📋 <b>Уже зафиксированные итоги</b>"
    return prefix + f"\nНеделя с {week} (UTC)\n" + "\n".join(lines) + "\n\nСтатус призов: ожидают ручной выдачи администрацией. Реальные деньги и NFT не отправляются автоматически ботом."


async def _broadcast_weekly_awards(bot: Bot, text: str):
    for chat_id in community.tracked_group_ids():
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            log.exception("Could not announce weekly winners to chat %s", chat_id)


async def weekly_awards_watcher(bot: Bot):
    """Finalize the previous UTC week on Monday and announce winners globally."""
    from datetime import datetime, timezone
    while True:
        now = datetime.now(timezone.utc)
        if now.weekday() == 0:
            week, winners, created = community.finalize_weekly_awards(
                cash_prizes=tuple(os.getenv(f"WEEKLY_CASH_PRIZE_{i}", default) for i, default in enumerate(("50 USD", "25 USD", "10 USD"), 1)),
                nft_prizes=tuple(os.getenv(f"WEEKLY_NFT_PRIZE_{i}", default) for i, default in enumerate(("NFT-подарок #1", "NFT-подарок #2", "NFT-подарок #3"), 1)),
            )
            if created and winners:
                await _broadcast_weekly_awards(bot, _weekly_awards_text(week, winners, True))
        await asyncio.sleep(3600)


@dp.message(Command("group_setup"))
async def group_setup(message: Message):
    bot_username = (await message.bot.get_me()).username
    if not bot_username:
        await message.answer("Не удалось получить ссылку бота. Попробуй позже.")
        return
    await message.answer(
        "👥 <b>ИГРАЙ В ГРУППЕ</b>\n\n"
        "Нажми кнопку ниже и Telegram предложит выбрать группу, куда добавить PULSAR • ПОМОЩНИК СООБЩЕСТВ. "
        "После добавления открой группу и используй кнопки игры в меню бота или игровые команды.\n\n"
        "У бота должны быть права отправлять сообщения в выбранной группе.",
        reply_markup=add_to_group_menu(bot_username), parse_mode="HTML",
    )


@dp.message(F.text, ~F.text.startswith("/"))
async def process_deposit_amount(message: Message):
    if not message.from_user or message.from_user.id not in pending_deposit_amount or message.chat.type != "private": return
    raw = (message.text or "").strip().casefold().replace(",", ".")
    if raw in {"отмена", "cancel", "стоп"}:
        pending_deposit_amount.discard(message.from_user.id)
        await message.answer("Пополнение отменено.")
        return
    try: amount = Decimal(raw)
    except Exception:
        await message.answer("Введи сумму числом, например 5 или 12.50. Для отмены — «отмена».")
        return
    if not amount.is_finite() or amount < Decimal("0.50") or amount > Decimal("500.00") or amount.as_tuple().exponent < -2:
        await message.answer("Сумма должна быть от $0.50 до $500.00, максимум два знака после запятой.")
        return
    if not settings.payments_enabled or not settings.crypto_pay_api_token:
        pending_deposit_amount.discard(message.from_user.id)
        await message.answer("💰 Пополнение временно недоступно.")
        return
    try:
        invoice = await payment_client().create_invoice(asset="USDT,TON,BTC", amount=amount,
            description=f"PULSAR wallet deposit for Telegram user {message.from_user.id}", payload=str(message.from_user.id))
        invoice_id = invoice.get("invoice_id")
        pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
        if not invoice_id or not pay_url: raise RuntimeError("Crypto Pay invoice response is missing invoice_id or payment URL")
        init_store(); save_invoice(invoice_id, message.from_user.id, amount)
        kb = InlineKeyboardBuilder(); kb.button(text=f"💳 Оплатить ${amount:.2f}", url=pay_url); kb.adjust(1)
        pending_deposit_amount.discard(message.from_user.id)
        await message.answer(f"✅ Счёт создан на <b>${amount:.2f} USD</b>. После подтверждения оплаты баланс будет зачислен автоматически.", reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        log.exception("Failed to create typed Crypto Pay invoice for user %s", message.from_user.id)
        pending_deposit_amount.discard(message.from_user.id)
        await message.answer("💳 Не удалось создать счёт. Деньги не списаны — попробуй позже.")


@dp.message(F.text, ~F.text.startswith("/"))
async def process_withdrawal_recipient(message: Message):
    if not message.from_user or message.from_user.id not in pending_withdrawal_amount:
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Отправь числовой Telegram ID получателя. Чтобы отменить, нажми «Отмена» в меню кошелька.")
        return
    amount = pending_withdrawal_amount.pop(message.from_user.id)
    if not settings.payments_enabled or not settings.crypto_pay_api_token:
        await message.answer("💸 Вывод временно недоступен.", reply_markup=wallet_menu())
        return
    withdrawal_id = uuid.uuid4().hex
    recipient_id = int(raw)
    try:
        reserved = reserve_withdrawal(withdrawal_id, message.from_user.id, recipient_id, amount)
    except Exception:
        log.exception("Could not reserve withdrawal for user %s", message.from_user.id)
        await message.answer("⚠️ Заявку создать не удалось. Проверь баланс и попробуй позже.", reply_markup=wallet_menu())
        return
    if not reserved:
        await message.answer(f"Недостаточно средств для вывода ${amount:.2f}. Текущий баланс: ${get_balance(message.from_user.id):.2f}.", reply_markup=wallet_menu())
        return
    try:
        result = await payment_client().transfer(
            user_id=recipient_id, asset="USDT", amount=amount,
            spend_id=withdrawal_id, comment="PULSAR • ПОМОЩНИК СООБЩЕСТВ withdrawal",
        )
        transfer_id = str(result.get("transfer_id") or result.get("spend_id") or withdrawal_id)
        finish_withdrawal(withdrawal_id, "paid", transfer_id=transfer_id)
        await message.answer(
            f"<b>Вывод отправлен</b>\n\n"
            f"Сумма: <b>{amount:.2f} USDT</b>\nПолучатель ID: <code>{recipient_id}</code>\n"
            f"Номер операции: <code>{html.escape(transfer_id)}</code>\n"
            f"Остаток баланса: <b>${get_balance(message.from_user.id):.2f}</b>",
            reply_markup=wallet_menu(), parse_mode="HTML",
        )
    except Exception as exc:
        # A timeout may happen after the provider accepted a transfer. Do not auto-refund,
        # because that could allow a duplicate payout. Mark for manual reconciliation.
        log.exception("Crypto Pay withdrawal outcome uncertain: %s", withdrawal_id)
        finish_withdrawal(withdrawal_id, "review", error=str(exc), refund=False)
        await message.answer(
            "<b>Вывод требует проверки</b>\n\nПлатёжный сервис не подтвердил результат однозначно. "
            "Сумма временно удерживается, чтобы исключить двойную выплату. Обратись в поддержку с номером заявки:\n"
            f"<code>{withdrawal_id}</code>", reply_markup=wallet_menu(), parse_mode="HTML",
        )


@dp.message(Command("admin"))
async def admin_menu(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Эта команда доступна только администратору.")
        return
    await message.answer(
        "🛡 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n\n"
        "/admin_status — состояние бота\n/admin_help — список команд\n\n"
        "ℹ️ Пополнение кошелька: $0.50, $1, $5, $10, $25 и $50 при включённой настройке платежей.",
        reply_markup=back_menu(), parse_mode="HTML",
    )


@dp.message(Command("admin_help"))
async def admin_help(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Эта команда доступна только администратору.")
        return
    await message.answer(
        "🛡 <b>КОМАНДЫ АДМИНИСТРАТОРА</b>\n\n"
        "/admin — панель\n/admin_status — состояние конфигурации\n/healthcheck — локальная проверка файлов, БД и конфигурации\n\n"
        "🔒 Пополнение кошелька: $0.50, $1, $5, $10, $25 и $50 при включённой настройке платежей; вывод и денежные ставки отключены.",
        reply_markup=back_menu(), parse_mode="HTML",
    )


@dp.message(Command("healthcheck"))
async def healthcheck_command(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Проверка доступна только администратору.")
        return
    from app.diagnostics import run_diagnostics
    checks = run_diagnostics()
    ok_count = sum(item["status"] == "OK" for item in checks)
    warn_count = len(checks) - ok_count
    lines = [f"{'✅' if item['status'] == 'OK' else '⚠️'} <b>{html.escape(item['name'])}</b> — {html.escape(item['detail'])}" for item in checks]
    await message.answer(
        "🩺 <b>ЛОКАЛЬНАЯ ПРОВЕРКА PULSAR</b>\n\n"
        + "\n".join(lines)
        + f"\n\nИтог: {ok_count} OK · {warn_count} предупреждений.\n"
        "Проверка не выполняет сетевые запросы и не подтверждает работу внешних сервисов в реальном времени.",
        parse_mode="HTML",
    )


@dp.message(Command("admin_status"))
async def admin_status(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Эта команда доступна только администратору.")
        return
    await message.answer(
        f"{CUSTOM_EMOJI['stats']} <b>СТАТУС PULSAR • ПОМОЩНИК СООБЩЕСТВ</b>\n\n"
        f"Среда: {settings.app_env}\nПлатежи: {'включены' if settings.payments_enabled and settings.crypto_pay_api_token else 'выключены'}\nАвтозачисление: после подтверждения оплаты\nСтавки: доступны в поддерживаемых играх при наличии баланса",
        reply_markup=back_menu(), parse_mode="HTML",
    )


def game_history_text(telegram_id: int) -> str:
    from app.payments.deposit_store import get_game_history
    rows = get_game_history(telegram_id, 10)
    if not rows:
        return f"{CUSTOM_EMOJI['stats']} <b>ИСТОРИЯ ИГР</b>\n\nПока нет завершённых раундов. Выбери игру и сыграй первый раунд."
    lines = []
    for row in rows:
        status = "🟢 выигрыш" if Decimal(row["payout_usd"]) > 0 else "⚪ без выигрыша"
        lines.append(f"{row['created_at']} · {html.escape(GAME_INFO.get(row['game_key'], {}).get('name', row['game_key']))} · ставка ${Decimal(row['stake_usd']):.2f} · выплата ${Decimal(row['payout_usd']):.2f} · {status}")
    return f"{CUSTOM_EMOJI['stats']} <b>ИСТОРИЯ ПОСЛЕДНИХ ИГР</b>\n\n" + "\n".join(lines)


def profile_text(telegram_id: int, full_name: str) -> str:
    stats = get_profile_stats(telegram_id)
    progress = stats["progress"]
    bar = "🟩" * round(progress / 10) + "⬛" * (10 - round(progress / 10))
    nxt = stats["next_threshold"]
    progress_label = f"{stats['turnover']:.2f} / {nxt:.0f} $" if nxt is not None else "Максимальный уровень"
    return (
        f"{CUSTOM_EMOJI['crown']} <b>МОЙ ПРОФИЛЬ PULSAR • ПОМОЩНИК СООБЩЕСТВ</b>\n\n"
        f"{CUSTOM_EMOJI['greeting']} Игрок: <b>{html.escape(full_name)}</b>\n"
        f"{CUSTOM_EMOJI['card']} Ваш ID: <code>{telegram_id}</code>\n\n"
        f"{CUSTOM_EMOJI['money']} Баланс: <b>{stats['balance']:.2f} $</b>\n"
        f"{CUSTOM_EMOJI['cash']} Всего пополнено: <b>{stats['total_deposits']:.2f} $</b>\n"
        f"{CUSTOM_EMOJI['gem']} PAX-баланс: <b>{stats['pax_balance']} PAX</b>\n\n"
        f"{CUSTOM_EMOJI['games']} Сыграно игр: <b>{stats['games_played']}</b>\n"
        f"{CUSTOM_EMOJI['trend_up']} Оборот казино: <b>{stats['turnover']:.2f} $</b>\n"
        f"{CUSTOM_EMOJI['gem']} Опыт казино: <b>{int(stats.get('xp', 0)):,} XP</b>\n"
        f"{CUSTOM_EMOJI['gem']} Оборот коллекции: <b>{int(stats.get('turnover_abx', 0)):,} ABX</b>\n\n"
        f"{CUSTOM_EMOJI['gold']} Уровень: <b>{stats['level']}</b>\n{bar} <b>{progress}%</b>\n"
        f"<i>Прогресс: {progress_label}</i>\n\n"
        f"{CUSTOM_EMOJI['gift']} Кешбэк: <b>1%</b>\n"
        "<i>Кешбэк начисляется только по поддерживаемым ставкам. В этой версии денежные ставки отключены, поэтому начислений пока нет.</i>"
    )


@dp.callback_query(F.data)
async def menu_router(call: CallbackQuery):
    if not call.message:
        await call.answer()
        return
    data = call.data or ""
    if data == "bonus_check_chat":
        await call.answer()
        try:
            member = await call.bot.get_chat_member(BONUS_CHAT_ID, call.from_user.id)
            joined = member.status in {"member", "administrator", "creator"} or (member.status == "restricted" and getattr(member, "is_member", False))
            if not joined:
                await call.message.answer("Пока не вижу тебя в чате. Вступи по кнопке выше и проверь ещё раз.", reply_markup=bonus_menu())
                return
            credited = claim_task_reward(call.from_user.id, "join_chat_1004371502806", Decimal("0.10"))
            if credited:
                balance = get_balance(call.from_user.id)
                await call.message.answer(f"🎉 Бонус $0.10 начислен! Твой баланс: <b>${balance:.2f}</b>.", reply_markup=bonus_menu(), parse_mode="HTML")
            else:
                await call.message.answer("Ты уже получал этот бонус. Повторное начисление невозможно.", reply_markup=bonus_menu())
        except Exception:
            log.exception("Bonus membership check failed for user %s", call.from_user.id)
            await call.message.answer("Не удалось проверить участие. Убедись, что бот является администратором чата, и попробуй снова.", reply_markup=bonus_menu())
        return

    if data == "community_hub":
        await call.answer()
        await call.message.answer(
            f"{CUSTOM_EMOJI['globe']} <b>МИР И СООБЩЕСТВО PULSAR</b>\n\n"
            "Коллекционируй карты, ищи награды в событиях, открывай паки и смотри глобальный рейтинг активности.\n\n"
            + _community_wallet_text(call.from_user.id, call.from_user.id, call.from_user.username or call.from_user.full_name),
            reply_markup=community_menu(), parse_mode="HTML")
        return
    if data in {"world_event", "world_search", "card_album", "card_pack", "card_market", "global_top"}:
        await call.answer()
        uid = call.from_user.id
        scope_id = call.from_user.id
        username = call.from_user.username or call.from_user.full_name
        if data == "world_event":
            event = community.world_event(scope_id)
            left = max(0, event["ends_at"] - int(time.time()))
            text = (f"{CUSTOM_EMOJI['globe']} <b>СОБЫТИЕ PULSAR</b>\n\n{event['emoji']} <b>{html.escape(event['title'])}</b>\n"
                    f"{html.escape(event['desc'])}\nБонус: +{event['bonus']} коинов\nДо смены: {left//3600} ч {(left%3600)//60} мин.")
        elif data == "world_search":
            card, wait, event = community.world_search(scope_id, uid, username)
            if card is None:
                text = f"{CUSTOM_EMOJI['timer']} Следующий поиск через {wait//3600} ч {(wait%3600)//60} мин.\nСобытие: <b>{html.escape(event['title'])}</b>"
            else:
                text = (f"{CUSTOM_EMOJI['celebration']} <b>КАРТА НАЙДЕНА</b>\n\n{card['emoji']} <b>{html.escape(card['name'])}</b>\n"
                        f"Редкость: {html.escape(card['rarity'])} · цена: {card['value']} коинов\nНаграда: +{card['coins']} коинов")
        elif data == "card_album":
            text = _album_text(scope_id, uid)
        elif data == "card_pack":
            ok, result = community.open_card_pack(scope_id, uid, username)
            if not ok:
                text = f"{CUSTOM_EMOJI['warning']} {html.escape(result)}"
            else:
                lines = [f"{c['emoji']} <b>{html.escape(c['name'])}</b> · {html.escape(c['rarity'])}" for c in result['cards']]
                text = f"{CUSTOM_EMOJI['gift']} <b>ПАК ОТКРЫТ</b>\n\n" + "\n".join(lines) + f"\n\nСписано: {result['cost']} коинов."
        elif data == "card_market":
            lines = [f"{c['emoji']} <b>{html.escape(c['name'])}</b> — {c['value']} коинов · {html.escape(c['rarity'])}" for c in community.WORLD_CARDS.values()]
            text = f"{CUSTOM_EMOJI['shopping']} <b>РЫНОК КАРТ</b>\n\n" + "\n".join(lines) + "\nПак: 3 карты за 35 коинов."
        else:
            rows = community.global_weekly_leaderboard(10)
            medals = [CUSTOM_EMOJI['gold'], CUSTOM_EMOJI['silver'], CUSTOM_EMOJI['bronze']]
            lines = [f"{medals[i] if i < 3 else str(i+1)+'.'} {html.escape(row['username'] or str(row['user_id']))} — {row['points']} очк. · {row['messages']} сообщ." for i, row in enumerate(rows)]
            text = f"{CUSTOM_EMOJI['stats']} <b>ГЛОБАЛЬНЫЙ ТОП НЕДЕЛИ</b>\n" + ("\n".join(lines) if lines else "Пока нет активности в группах.") + "\n\nРейтинг учитывает группы, где бот получил сообщения. Личный чат в глобальный топ групп не входит."
        await call.message.answer(text, parse_mode="HTML", reply_markup=community_menu())
        return

    if data == "donate":
        await call.answer()
        await call.message.answer(
            f"{CUSTOM_EMOJI['heart']} <b>ПОДДЕРЖАТЬ БОТА</b>\n\n"
            "Если тебе нравится бот, можешь поддержать его развитие через Telegram Stars. "
            "Пожертвование добровольное: оно не даёт игровых преимуществ и не зачисляется на баланс.",
            reply_markup=donate_menu(), parse_mode="HTML")
        return
    if data.startswith("donate_") and data.removeprefix("donate_").isdigit():
        amount = int(data.removeprefix("donate_"))
        if amount not in {25, 50, 100, 250}:
            await call.answer("Недопустимая сумма", show_alert=True)
            return
        await call.answer()
        try:
            await call.bot.send_invoice(
                chat_id=call.from_user.id,
                title="Поддержка бота",
                description=f"Добровольная поддержка развития бота — {amount} Telegram Stars.",
                payload=f"nova_donation:{call.from_user.id}:{amount}",
                currency="XTR",
                prices=[LabeledPrice(label="Поддержка бота", amount=amount)],
            )
        except Exception:
            log.exception("Could not create Telegram Stars donation invoice for %s", call.from_user.id)
            await call.message.answer("Не удалось создать счёт Stars. Попробуй позже.")
        return

    if data.startswith("stake_"):
        parts = data.split("_")
        if len(parts) == 3 and parts[1] in GAME_INFO and parts[2].isdigit():
            stake = Decimal(int(parts[2])) / Decimal(100)
            if stake not in BET_OPTIONS:
                await call.answer("Недопустимая сумма ставки", show_alert=True); return
            await call.answer("Ставка выбрана")
            await play_game(call.message, parts[1], call.from_user.id, stake)
        return
    if data.startswith("play_") and data[5:] in GAME_INFO:
        key = data[5:]
        await call.answer("Выбери ставку")
        await play_game(call.message, key, call.from_user.id)
        return

    if data.startswith("topup_") and data.removeprefix("topup_") in DEPOSIT_OPTIONS:
        await call.answer()
        amount = DEPOSIT_OPTIONS[data.removeprefix("topup_")]
        if not settings.payments_enabled:
            await call.message.answer("💰 Пополнение временно недоступно. Попробуй позже.")
            return
        if not settings.crypto_pay_api_token:
            await call.message.answer("💳 Пополнение временно недоступно. Попробуй позже.")
            return
        try:
            invoice = await payment_client().create_invoice(
                asset="USDT,TON,BTC",
                amount=amount,
                description=f"PULSAR • ПОМОЩНИК СООБЩЕСТВ wallet deposit for Telegram user {call.from_user.id}",
                payload=str(call.from_user.id),
            )
            invoice_id = invoice.get("invoice_id")
            pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
            if not invoice_id or not pay_url:
                raise RuntimeError("Crypto Pay invoice response is missing invoice_id or payment URL")
            init_store()
            save_invoice(invoice_id, call.from_user.id, amount)
            kb = InlineKeyboardBuilder()
            kb.button(text=f"💳 Оплатить ${amount:.2f}", url=pay_url)
            kb.adjust(1)
            await call.message.answer(
                f"{CUSTOM_EMOJI['card']} <b>Счёт создан</b>\n\nСумма: <b>${amount:.2f} USD</b>\nОплати счёт по кнопке. После подтверждения оплаты бот автоматически зачислит депозит на баланс.",
                reply_markup=kb.as_markup(), parse_mode="HTML",
            )
        except Exception:
            log.exception("Failed to create Crypto Pay invoice for user %s", call.from_user.id)
            await call.message.answer("Не удалось создать счёт. Попробуй позже; средства не списаны.")
        return

    if data == "withdraw":
        await call.answer()
        await call.message.answer(
            "<b>ВЫВОД СРЕДСТВ</b>\n\n"
            "Выбери сумму для вывода. Средства будут удержаны на время операции. "
            "Для получения нужен числовой Telegram ID пользователя. "
            "Проверь ID перед подтверждением.", reply_markup=withdrawal_menu(), parse_mode="HTML")
        return
    if data.startswith("withdraw_amount_"):
        cents_text = data.removeprefix("withdraw_amount_")
        if not cents_text.isdigit():
            await call.answer("Некорректная сумма", show_alert=True); return
        amount = Decimal(int(cents_text)) / Decimal(100)
        if amount not in WITHDRAWAL_OPTIONS:
            await call.answer("Недопустимая сумма", show_alert=True); return
        if get_balance(call.from_user.id) < amount:
            await call.answer("Недостаточно средств", show_alert=True); return
        pending_withdrawal_amount[call.from_user.id] = amount
        await call.answer()
        await call.message.answer(
            f"Выбрано: <b>${amount:.2f} → USDT</b>\n\n"
            "Теперь отправь числовой Telegram ID получателя отдельным сообщением. "
            "Не отправляй пароль, код входа или API-токен.", parse_mode="HTML")
        return

    if data == "casino_event":
        event = casino_daily_event()
        info = GAME_INFO[event["game"]]
        await call.answer()
        await call.message.answer(
            f"🎉 <b>ИВЕНТ КАЗИНО PULSAR</b>\n\n{info['emoji']} <b>{html.escape(event['name'])}</b>\n"
            f"{html.escape(event['desc'])}\n\nИвент общий для всех игроков и обновляется по UTC. Бонус применяется только к выигрышным раундам указанной игры.",
            reply_markup=games_menu(), parse_mode="HTML")
        return

    if data == "games":
        text = (
            "🎮 <b>ВЫБЕРИ СВОЮ ИГРУ</b> ✨\n"
            "\n"
            "Выбирай, во что сыграем сегодня! Нажми на кнопку — и сразу увидишь игровую анимацию и результат.\n\n"
            f"{CUSTOM_EMOJI['dice']} <b>Кубик</b> — испытание удачи\n"
            "⚽ <b>Футбол</b> — забей гол\n"
            "🏀 <b>Баскетбол</b> — бросок в кольцо\n"
            "🎯 <b>Дартс</b> — проверь меткость\n"
            f"{CUSTOM_EMOJI['bowling']} <b>Боулинг</b> — сбей кегли\n"
            "🎱 <b>Бильярд</b> · 🎡 <b>Рулетка</b> · 🃏 <b>Блэкджек</b> · ♠️ <b>Баккара</b>\n"
            "🚀 <b>Crash</b> · 💣 <b>Мины</b>\n\n"
            "🍀 Ивент дня даёт дополнительный множитель для выбранной игры."
        )
        sent_with_card = await send_feature_card_for_chat(
            call.bot, call.message.chat.id, "games", caption=text,
            reply_markup=games_menu(), parse_mode="HTML",
        )
        if not sent_with_card:
            await call.message.answer(text, reply_markup=games_menu(), parse_mode="HTML")
    elif data == "back":
        await send_welcome(call.message)
    else:
        pages = {
            "wallet": f"{CUSTOM_EMOJI['wallet']} <b>ТВОЙ КОШЕЛЁК</b>\n\n{CUSTOM_EMOJI['money']} Баланс: <b>${get_balance(call.from_user.id):.2f} USD</b>\n\n{CUSTOM_EMOJI['cash']} Выбери сумму пополнения или вывода ниже.",
            "profile": profile_text(call.from_user.id, call.from_user.full_name),
            "history": f"📜 <b>ИСТОРИЯ ИГР</b>\n\n{game_history_text(call.from_user.id)}",
            "bonuses": f"{CUSTOM_EMOJI['celebration']} <b>БОНУСЫ</b>\n\n{CUSTOM_EMOJI['gold']} Вступи в наш чат и получи <b>$0.10</b> на игровой баланс! Нажми «Перейти в чат», затем вернись и выбери «Проверить подписку». Бонус начисляется один раз.\n\n{CUSTOM_EMOJI['heart']} Кешбэк составляет 1% от принятой ставки. Удачи!",
            "rules": f"{CUSTOM_EMOJI['castle']} <b>ПРАВИЛА ИГРЫ</b>\n\n1️⃣ Играй честно и уважай других участников.\n2️⃣ Не отправляй никому пароли и секретные данные.\n3️⃣ Ставка списывается перед раундом. Выплаты зависят от результата и коэффициента, указанного в результате. Играй ответственно.\n4️⃣ Если игра не запустилась, попробуй ещё раз чуть позже.\n\n{CUSTOM_EMOJI['celebration']} Главное — хорошее настроение!",
            "group_info": f"{CUSTOM_EMOJI['swords']} <b>ИГРА С ДРУЗЬЯМИ</b>\n\nДобавь PULSAR в группу и разреши ему отправлять сообщения. Выбирайте игру в меню — результаты появятся прямо в чате.\n\n🏆 Соревнуйтесь и определяйте чемпиона!",
            "support": f"🆘 <b>ПОДДЕРЖКА PULSAR • ПОМОЩНИК СООБЩЕСТВ</b>\n\nВозникла проблема? Напиши администратору бота и приложи описание того, что произошло. Не отправляй пароли, токены или приватные ключи.\n\n{CUSTOM_EMOJI['heart']} Постараемся помочь! {CUSTOM_EMOJI['stop']}",
        }
        if data in pages:
            card_for_page = {"wallet": "wallet", "profile": "profile_detail", "history": "history", "bonuses": "bonuses", "rules": "rules", "group_info": "play_with_friends", "support": "support"}.get(data)
            if data == "wallet":
                markup = wallet_menu()
            elif data == "bonuses":
                markup = bonus_menu()
            elif data == "group_info":
                bot_username = (await call.bot.get_me()).username
                markup = add_to_group_menu(bot_username) if bot_username else back_menu()
            else:
                markup = back_menu()
            sent = await send_feature_card_for_chat(call.bot, call.message.chat.id, card_for_page or data, caption=pages[data], reply_markup=markup, parse_mode="HTML")
            if not sent:
                await call.message.answer(pages[data], reply_markup=markup, parse_mode="HTML")
        await call.answer()


async def payment_watcher(bot: Bot):
    """Poll Crypto Pay and automatically credit paid invoices without user interaction."""
    if not settings.payments_enabled or not settings.crypto_pay_api_token:
        log.info("Crypto Pay watcher disabled (payments off or token missing)")
        return
    client = payment_client()
    log.info("Crypto Pay automatic deposit watcher started")
    while True:
        try:
            for record in get_pending_deposits(limit=100):
                invoice_id = str(record["invoice_id"])
                try:
                    invoice = await client.get_invoice(invoice_id)
                    if not invoice:
                        continue
                    status = str(invoice.get("status", "unknown")).lower()
                    owner_id = int(record["telegram_id"])
                    # Validate provider response against the immutable local invoice record.
                    if (str(invoice.get("payload", "")) != str(owner_id)
                            or str(invoice.get("fiat", "")).upper() != "USD"
                            or Decimal(str(invoice.get("amount", "0"))) != Decimal(record["amount_usd"])):
                        log.error("Invoice validation mismatch for invoice %s; not crediting", invoice_id)
                        continue
                    update_deposit_status(invoice_id, status)
                    if status == "paid":
                        credited = mark_paid_and_credit(invoice_id)
                        if credited:
                            balance = get_balance(owner_id)
                            try:
                                await bot.send_message(
                                    owner_id,
                                    f"✅ <b>Оплата подтверждена автоматически!</b>\n"
                                    f"На баланс зачислено: <b>${Decimal(record['amount_usd']):.2f}</b>\n"
                                    f"Текущий баланс: <b>${balance:.2f}</b>",
                                    parse_mode="HTML",
                                    reply_markup=wallet_menu(),
                                )
                            except Exception:
                                log.exception("Could not notify user %s about paid invoice %s", owner_id, invoice_id)
                except Exception:
                    log.exception("Error polling Crypto Pay invoice %s", invoice_id)
        except Exception:
            log.exception("Crypto Pay watcher iteration failed")
        await asyncio.sleep(10)


async def main():
    init_store()
    init_withdrawal_store()
    community.init_community_store()
    ai_memory.init_memory()
    if not settings.bot_token or settings.bot_token == "replace_me":
        raise RuntimeError("Set BOT_TOKEN in .env before starting the bot")
    bot = Bot(settings.bot_token, session=AiohttpSession(timeout=90))
    watcher_task = asyncio.create_task(payment_watcher(bot), name="crypto-pay-payment-watcher")
    weekly_task = asyncio.create_task(weekly_awards_watcher(bot), name="global-weekly-awards-watcher")
    # Keep the public Telegram command menu intentionally minimal.
    # Group setup is exposed as a regular inline button, not a slash command.
    commands = [
        BotCommand(command="start", description="Открыть PULSAR"),
        BotCommand(command="help", description="Как пользоваться ботом"),
    ]
    try:
        # A transient Telegram connection issue must not prevent polling from starting.
        for attempt in range(1, 6):
            try:
                await bot.set_my_commands(commands, scope=BotCommandScopeDefault(), request_timeout=20)
                await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats(), request_timeout=20)
                await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats(), request_timeout=20)
                break
            except TelegramNetworkError:
                if attempt == 5:
                    log.warning("Could not update Telegram command menu after 5 attempts; starting polling anyway")
                    break
                delay = min(2 ** attempt, 20)
                log.warning("Telegram connection failed while setting commands (attempt %s/5); retrying in %ss", attempt, delay)
                await asyncio.sleep(delay)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watcher_task.cancel()
        weekly_task.cancel()
        for task in (watcher_task, weekly_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
