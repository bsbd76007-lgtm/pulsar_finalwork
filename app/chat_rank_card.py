"""Generate live, rank-aware PULLSAR cards for group-chat leaderboards."""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import hashlib
import math
import os
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _short(text: str, limit: int = 23) -> str:
    text = str(text or "Участник").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_chat_rank_card(*, chat_title: str, ranking_title: str, ranked_rows: list[dict],
                          viewer_name: str, viewer_rank: int | None, viewer_value: int,
                          metric_label: str, viewer_xp: int = 0, period_label: str = "ВСЁ ВРЕМЯ") -> str:
    """Create a 1200x675 branded card whose accent and badge reflect the viewer's live rank."""
    W, H = 1200, 675
    rank_colors = {
        1: ((255, 205, 76), (255, 137, 32), "ЧЕМПИОН ЧАТА"),
        2: ((210, 228, 255), (115, 155, 255), "СЕРЕБРО"),
        3: ((255, 164, 112), (184, 93, 255), "БРОНЗА"),
    }
    accent, glow, rank_label = rank_colors.get(viewer_rank, ((73, 202, 255), (136, 60, 255), "В РЕЙТИНГЕ"))
    # Deep blue-violet gradient background.
    im = Image.new("RGB", (W, H), (3, 5, 20))
    px = im.load()
    for y in range(H):
        for x in range(W):
            wave = (math.sin(x / 91 + y / 77) + 1) / 2
            px[x, y] = (int(3 + 5 * wave), int(6 + 3 * wave), int(20 + 17 * wave))
    # Blurred neon pools make the glass feel cinematic without hiding text.
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.ellipse((760, -140, 1370, 460), fill=(*glow, 80))
    gd.ellipse((-180, 300, 500, 860), fill=(35, 95, 255, 65))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(90))
    im = Image.alpha_composite(im.convert("RGBA"), glow_layer)
    d = ImageDraw.Draw(im, "RGBA")
    # Outer frame and inner glass panel.
    d.rounded_rectangle((22, 22, W-22, H-22), radius=38, fill=(3, 9, 28, 226), outline=(*accent, 235), width=3)
    d.rounded_rectangle((36, 36, W-36, H-36), radius=30, fill=(5, 13, 37, 210), outline=(50, 130, 255, 150), width=2)
    # Tech corner lines.
    for x, sign in ((56, 1), (W-56, -1)):
        d.line((x, 80, x + sign*85, 80), fill=(*accent, 210), width=3)
        d.line((x, 80, x, 132), fill=(*accent, 210), width=3)
    d.text((62, 48), "PULLSAR", font=_font(31, True), fill=(76, 193, 255, 255), stroke_width=1, stroke_fill=(45, 40, 255, 180))
    d.text((W-62, 56), _short(chat_title, 26), font=_font(19, True), fill=(185, 208, 255, 235), anchor="ra")
    d.text((62, 119), ranking_title.upper(), font=_font(27, True), fill=(242, 246, 255, 255))
    d.text((64, 159), period_label.upper(), font=_font(16, True), fill=(*accent, 255))
    # Viewer rank panel.
    d.rounded_rectangle((58, 205, 520, 580), radius=28, fill=(8, 17, 46, 226), outline=(*accent, 220), width=3)
    d.text((88, 231), "ТВОЁ МЕСТО", font=_font(21, True), fill=(185, 208, 255, 255))
    rank_text = f"#{viewer_rank}" if viewer_rank is not None else "—"
    d.text((83, 265), rank_text, font=_font(105, True), fill=(*accent, 255), stroke_width=2, stroke_fill=(*glow, 170))
    d.text((90, 390), rank_label, font=_font(20, True), fill=(*accent, 255))
    d.text((90, 431), _short(viewer_name, 27), font=_font(25, True), fill=(245, 248, 255, 255))
    d.text((90, 477), f"{metric_label}: {viewer_value:,}".replace(",", " "), font=_font(23, True), fill=(90, 224, 255, 255))
    if viewer_xp:
        d.text((90, 518), f"XP: {viewer_xp:,}".replace(",", " "), font=_font(19), fill=(184, 195, 235, 255))
    # Live top-three podium. Values and names are read from the current leaderboard.
    d.text((574, 210), "ЛИДЕРЫ СЕЙЧАС", font=_font(24, True), fill=(242, 246, 255, 255))
    podium_x = [620, 795, 970]
    podium_h = [178, 230, 145]
    podium_order = [1, 0, 2]  # visual 2nd, 1st, 3rd
    medal_colors = [(195, 215, 255), (255, 205, 76), (255, 157, 105)]
    for j, row_index in enumerate(podium_order):
        row = ranked_rows[row_index] if row_index < len(ranked_rows) else None
        x = podium_x[j]
        h = podium_h[j]
        y = 552 - h
        color = medal_colors[j]
        d.rounded_rectangle((x, y, x+125, 552), radius=16, fill=(*color, 44), outline=(*color, 230), width=3)
        place = row_index + 1
        d.text((x+62, y+17), f"#{place}", font=_font(29, True), fill=(*color, 255), anchor="ma")
        if row:
            name = _short(row.get("username") or f"ID {row.get('user_id', '?')}", 12)
            value = int(row.get("value", row.get("coins", row.get("messages", 0))) or 0)
            d.text((x+62, y+62), name, font=_font(14, True), fill=(246, 248, 255, 255), anchor="ma")
            d.text((x+62, y+87), f"{value:,}".replace(",", " "), font=_font(17, True), fill=(92, 225, 255, 255), anchor="ma")
        else:
            d.text((x+62, y+65), "—", font=_font(22, True), fill=(210, 220, 255, 220), anchor="ma")
    d.text((W-62, H-56), "LIVE CHAT RANKING  •  PULLSAR", font=_font(15, True), fill=(133, 160, 220, 240), anchor="ra")
    # Save to a deterministic temporary file for easy cleanup after Telegram upload.
    key = hashlib.sha1(f"{chat_title}|{ranking_title}|{viewer_name}|{viewer_rank}|{viewer_value}|{ranked_rows}".encode("utf-8", "ignore")).hexdigest()[:12]
    path = Path(tempfile.gettempdir()) / f"pulsar_chat_rank_{key}.png"
    im.convert("RGB").save(path, format="PNG", optimize=True)
    return str(path)
