# Telegram Premium custom emoji

`assets/emoji_catalog.json` contains the custom emoji IDs transcribed from the Telegram message entities supplied by the project owner. IDs are exact; entries with suffixes such as `_alt` represent different custom stickers attached to the same visible glyph.

`app/bot.py` uses Telegram HTML tags (`<tg-emoji emoji-id="...">...</tg-emoji>`) for a set of these stickers in the welcome text and reusable emoji constants. The sender automatically applies catalogued IDs to outgoing message text and photo captions where supported. Inline keyboard labels use Unicode emoji because Telegram does not parse HTML custom-emoji tags inside button text. If Telegram cannot render a custom emoji in a particular client or context, the inner Unicode glyph is the fallback.

The game is still demo-only; this change does not enable real-money deposits, wagers, or payouts.
