# Release checklist — casino, games, community and Stars

## Before deployment

1. Stop the current bot process so two pollers do not run with the same token.
2. Back up `pulsar_fan.sqlite3` and `.env`. Never include `.env`, database files, tokens or payment secrets in a shared archive.
3. Install dependencies in the project's virtual environment with `python -m pip install -r requirements.txt`.
4. Run `python -m py_compile app/bot.py app/community_store.py app/group_call.py app/payments/deposit_store.py` and `python -m pytest -q`.
5. Check that the bot token, payment-provider token and payment settings are configured only in `.env` or the deployment secret store.

## Smoke test in a private chat

- Open `/start`, navigate to games, profile, wallet, history, bonuses, rules and support.
- Check that a game cannot debit the balance when the balance is insufficient.
- Check a successful and a losing game; verify balance, wager history, turnover, game count and XP.
- Create a Stars donation invoice with a supported amount. Confirm that currency is `XTR`, amount matches the invoice, and the success update is recorded once. A donation must never credit the casino balance.
- Test duplicate delivery of the same payment update; it must not create a second donation record.
- Check profile statistics after restarting the bot; values must persist in SQLite.

## Smoke test in a group

- Promote the bot only with the permissions required for the enabled features.
- Check welcome/onboarding, economy, collection, daily reward and leaderboard.
- Test moderation by replying to a test account's message. Verify a regular member cannot moderate, and that admins/owner cannot be targeted by ordinary moderation actions.
- Check that missing admin permissions produce a readable error rather than a crash.

## Important operational notes

- Keep separate backups before database schema changes.
- Do not edit SQLite files while the bot is running.
- Treat the displayed owner profile seed as a presentation/testing baseline, not as proof of real deposits or independently verified wagering volume.
- Telegram Premium custom emoji only render when the ID is valid and Telegram allows the bot to use it; always retain readable fallback text.
- Test real payments with small amounts in a controlled environment before public launch. Never mark an invoice paid based only on a button click or client-side message.

## Global update: local diagnostics

- `/healthcheck` is available to IDs configured in `ADMIN_TELEGRAM_IDS`. It checks required files, all 20 social-card assets, dependency availability, payment/AI configuration presence, and SQLite `quick_check` where database files exist.
- The command is read-only and does not call Telegram, DeepSeek, or внешних платёжных API. `OK` for configuration does not mean a remote service is reachable.
- TON deposit verification remains disabled until a trusted indexer and finality/replay checks are implemented. Do not treat screenshots or user-supplied transaction IDs as payment proof.
- Never distribute `.env`, payment tokens, SQLite databases or runtime logs with secrets in them.


## Group-wide callout verification

- Verify `app/group_call.py` is present and `tests/test_group_call.py` passes.
- In a test group, send messages from several test accounts, then run `созыв` as an authorized admin or `call` moderator. Check that known participants are mentioned in chunks.
- Confirm an ordinary member without `call` cannot trigger a callout.
- Remember: Telegram Bot API cannot enumerate all group members; only participants seen by the bot can be included reliably.

- Verify `docs/PULSAR_GROUP_GUIDE.md` is attached when the bot becomes a group administrator.
- Confirm XP increases independently from monetary turnover.
