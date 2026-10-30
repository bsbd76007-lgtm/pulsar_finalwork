# Telegram admin MVP

## Access control
Set `ADMIN_TELEGRAM_IDS=123456789,987654321`. Only exact numeric Telegram user IDs are authorized. Never authorize by username; usernames can change.

## Commands
- `/admin`
- `/admin_status`
- `/admin_help`

## Intentionally unavailable
- Direct balance edits
- Manual marking of payments as paid
- Withdrawal execution
- Game result changes
- Secret/token display

Before adding further actions: role-based access, mandatory reason, audit event, confirmation step, idempotency, and second-person approval for high-risk actions.


## Групповые роли и массовый созыв

Все команды ниже вводятся обычным текстом, без `/`. Для назначения/снятия роли ответьте на сообщение участника.

- `повысить 3` — назначить уровень 3; можно передать права списком: `повысить 3 warn,mute,call`.
- `понизить` — снять роль.
- `модеры` — показать роли и права.
- `кал`, `калл`, `созыв`, `созвать всех`, `все сюда` — массовый созыв; требуется администратор Telegram или право `call`.

`app/group_call.py` отдельно сохраняет реестр участников, замеченных в сообщениях группы, и отправляет упоминания чанками. Важно: Telegram Bot API не позволяет боту получить полный список участников обычной группы, поэтому нельзя гарантированно упомянуть пользователей, которые никогда не писали в доступных боту сообщениях.
