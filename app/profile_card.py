"""Render elegant glass-style community profile cards as PNG images."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os
import tempfile

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1200, 760


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _short(value, limit=38):
    value = str(value or "").replace("\n", " ").strip()
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _days(first_seen: str) -> tuple[int, str]:
    if not first_seen:
        return 0, "с момента первого сообщения"
    try:
        dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = max(0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days)
        return days, dt.strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return 0, "с момента первого сообщения"


def render_profile_card(*, name: str, user_id: int, title: str = "", bio: str = "",
                        level: int = 1, xp: int = 0, messages: int = 0, coins: int = 0,
                        first_seen: str = "", chat_title: str = "Сообщество") -> str:
    """Return a temporary PNG path. first_seen is first observed message, not Telegram join date."""
    base = Image.new("RGB", (W, H), (11, 17, 42))
    px = base.load()
    # Deep navy-to-violet gradient with teal glow.
    for y in range(H):
        for x in range(W):
            glow1 = max(0.0, 1 - (((x - 150) / 620) ** 2 + ((y - 110) / 430) ** 2) ** 0.5)
            glow2 = max(0.0, 1 - (((x - 1040) / 520) ** 2 + ((y - 650) / 420) ** 2) ** 0.5)
            px[x, y] = (int(11 + 18 * glow1), int(17 + 20 * glow2), int(42 + 70 * glow1 + 38 * glow2))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-120, -160, 500, 430), fill=(66, 230, 218, 90))
    gd.ellipse((760, 390, 1390, 1010), fill=(153, 94, 255, 95))
    glow = glow.filter(ImageFilter.GaussianBlur(95))
    base = Image.alpha_composite(base.convert("RGBA"), glow)
    # Outer glass panel and inner card.
    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(panel)
    d.rounded_rectangle((38, 38, W-38, H-38), radius=42, fill=(255,255,255,25), outline=(220,245,255,115), width=2)
    d.rounded_rectangle((60, 60, W-60, H-60), radius=32, fill=(10,18,45,95), outline=(255,255,255,32), width=1)
    base = Image.alpha_composite(base, panel)
    d = ImageDraw.Draw(base)
    white = (246, 249, 255, 255); muted = (181, 198, 226, 255); cyan = (105, 237, 231, 255)
    d.text((88, 82), "P U L S A R   /   COMMUNITY", font=_font(21, True), fill=cyan)
    d.text((88, 125), "ПРОФИЛЬ УЧАСТНИКА", font=_font(37, True), fill=white)
    d.text((88, 185), _short(name, 35), font=_font(42, True), fill=white)
    d.text((90, 245), f"ID  {user_id}", font=_font(20), fill=muted)
    d.rounded_rectangle((88, 300, 1110, 302), radius=1, fill=(255,255,255,65))
    # Highlight level badge.
    d.rounded_rectangle((88, 330, 355, 430), radius=24, fill=(24, 75, 100, 255), outline=(105,237,231,180), width=2)
    d.text((112, 345), "УРОВЕНЬ", font=_font(17, True), fill=cyan)
    d.text((112, 370), str(level), font=_font(39, True), fill=white)
    d.text((390, 337), "О СЕБЕ", font=_font(17, True), fill=cyan)
    d.text((390, 368), _short(bio or "Участник сообщества PULSAR", 55), font=_font(22), fill=white)
    d.text((390, 402), "Титул: " + _short(title or "не установлен", 42), font=_font(18), fill=muted)
    # Stats tiles.
    tiles = [(88, 465, 330, 565, "СООБЩЕНИЯ", f"{messages:,}".replace(",", " ")),
             (352, 465, 594, 565, "ОПЫТ", f"{xp:,} XP".replace(",", " ")),
             (616, 465, 858, 565, "КОИНЫ", f"{coins:,}".replace(",", " ")),
             (880, 465, 1110, 565, "В ЧАТЕ", f"{_days(first_seen)[0]} дн.")]
    for x1,y1,x2,y2,label,value in tiles:
        d.rounded_rectangle((x1,y1,x2,y2), radius=20, fill=(22,34,67,255), outline=(105,237,231,95), width=1)
        d.text((x1+18,y1+15), label, font=_font(15, True), fill=muted)
        d.text((x1+18,y1+47), value, font=_font(25, True), fill=white)
    days, seen = _days(first_seen)
    d.text((90, 610), f"Первое сообщение, замеченное ботом: {seen}", font=_font(17), fill=muted)
    d.text((90, 654), _short(chat_title, 70), font=_font(17), fill=(160, 178, 211, 255))
    d.text((960, 654), "GLASS CARD  •  PULSAR", font=_font(14, True), fill=cyan)
    digest = hashlib.sha1(f"{user_id}-{datetime.now().timestamp()}".encode()).hexdigest()[:10]
    path = os.path.join(tempfile.gettempdir(), f"pulsar_profile_{digest}.png")
    base.convert("RGB").save(path, "PNG", optimize=True)
    return path
