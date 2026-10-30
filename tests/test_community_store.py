from app import community_store as c


def setup_function():
    import tempfile
    from pathlib import Path
    fd, path = tempfile.mkstemp(suffix='.sqlite3')
    import os
    os.close(fd)
    c.DB_PATH = path
    c.init_community_store()


def test_chat_wallet_is_separate_per_chat():
    a = c.wallet(-1001, 123, 'alice')
    b = c.wallet(-1002, 123, 'alice')
    assert a['coins'] == b['coins'] == 0
    c.daily(-1001, 123, 'alice')
    c.buy_item(-1001, 123, 'дерево', 1, 'alice')
    assert c.wallet(-1001, 123)['coins'] >= 8
    assert c.inventory(-1001, 123) == [('дерево', 1)]
    assert c.inventory(-1002, 123) == []


def test_collect_cooldown_and_inventory():
    item, wait = c.collect(-1001, 456, 'bob', now=2_000_000_000)
    assert item is not None and wait == 0
    item2, wait2 = c.collect(-1001, 456, 'bob', now=2_000_000_001)
    assert item2 is None and wait2 > 0
    assert sum(q for _, q in c.inventory(-1001, 456)) == 1


def test_sell_and_daily_are_transactional():
    c.daily(-1001, 789, 'charlie')
    c.buy_item(-1001, 789, 'дерево', 1, 'charlie')
    ok, earned = c.sell_item(-1001, 789, 'дерево')
    assert ok and earned == c.ITEMS['дерево']['sell']
    ok, reward = c.daily(-1001, 789, 'charlie')
    assert not ok and reward == 0  # daily reward was already claimed before buying


def test_change_coins_rewards_and_prevents_negative_balance():
    ok, balance = c.change_coins(-1001, 101, 5, 'player')
    assert ok and balance == 5
    ok, balance = c.change_coins(-1001, 101, -3, 'player')
    assert ok and balance == 2
    ok, balance = c.change_coins(-1001, 101, -9, 'player')
    assert not ok and balance == 2
    assert c.wallet(-1001, 101)['coins'] == 2


def test_world_event_persists_for_24_hours_and_rotates():
    first = c.world_event(-3001, now=2_000_000_000)
    same = c.world_event(-3001, now=2_000_000_100)
    assert first['key'] == same['key']
    assert same['ends_at'] - same['starts_at'] == 86400
    next_event = c.world_event(-3001, now=2_000_086_401)
    assert next_event['key'] != first['key']


def test_world_search_cooldown_and_card_album():
    found, wait, event = c.world_search(-3002, 777, 'tester', now=2_000_000_000)
    assert found and wait == 0
    album = c.card_album(-3002, 777)
    assert sum(x['quantity'] for x in album) == 1
    found2, wait2, _ = c.world_search(-3002, 777, 'tester', now=2_000_000_001)
    assert found2 is None and wait2 > 0


def test_card_pack_and_sale_use_community_coins_only():
    c.change_coins(-3003, 888, 100, 'tester')
    ok, result = c.open_card_pack(-3003, 888, 'tester')
    assert ok and len(result['cards']) == 3
    assert c.wallet(-3003, 888)['coins'] == 65
    album = c.card_album(-3003, 888)
    owned = next(x for x in album if x['quantity'] > 0)
    ok, earned = c.sell_card(-3003, 888, owned['key'], 1)
    assert ok and earned == owned['value']


def test_global_weekly_activity_combines_groups_and_limits_spam_points():
    from datetime import datetime, timezone
    base = int(datetime(2026, 1, 6, 12, tzinfo=timezone.utc).timestamp())
    for i in range(20):
        c.add_message(-1001 if i % 2 == 0 else -1002, 9001, 'alice', now=base + i)
    c.add_message(-1003, 9002, 'bob', now=base)
    top = c.global_weekly_leaderboard(now=base + 100)
    assert top[0]['user_id'] == 9001
    assert top[0]['messages'] == 20
    assert len(top[0]['chats'].split(',')) == 2
    assert top[0]['points'] == 2  # one point every 15 seconds, across chats


def test_weekly_awards_are_frozen_once_for_previous_utc_week():
    from datetime import datetime, timezone
    monday = int(datetime(2026, 1, 12, 12, tzinfo=timezone.utc).timestamp())
    prev_week = int(datetime(2026, 1, 6, 12, tzinfo=timezone.utc).timestamp())
    c.add_message(-1001, 101, 'alice', now=prev_week)
    c.add_message(-1002, 202, 'bob', now=prev_week + 20)
    week, winners, created = c.finalize_weekly_awards(now=monday, cash_prizes=('50 USD','25 USD','10 USD'), nft_prizes=('NFT A','NFT B','NFT C'))
    assert week == '2026-01-05'
    assert created and len(winners) == 2
    assert winners[0]['user_id'] == 101
    assert winners[0]['cash_prize'] == '50 USD'
    _, again, created_again = c.finalize_weekly_awards(now=monday, cash_prizes=('999 USD',))
    assert not created_again
    assert again[0]['cash_prize'] == '50 USD'


def test_free_helper_settings_notes_and_filters_persist_per_chat():
    c.set_chat_setting(-5001, "antilink", "1")
    assert c.get_chat_setting(-5001, "antilink") == "1"
    assert c.get_chat_setting(-5002, "antilink", "0") == "0"
    assert c.set_chat_note(-5001, "rules", "Be respectful", 123)
    assert c.get_chat_note(-5001, "rules") == "Be respectful"
    assert c.get_chat_note(-5002, "rules") is None
    assert c.set_chat_filter(-5001, "hello", "Hi there", 123)
    assert c.match_chat_filter(-5001, "Well HELLO everyone") == "Hi there"
    assert c.match_chat_filter(-5002, "hello") is None
    assert c.delete_chat_filter(-5001, "hello")
    assert c.match_chat_filter(-5001, "hello") is None


def test_world_events_have_unique_keys_and_valid_duration():
    keys = [event["key"] for event in c.WORLD_EVENTS]
    assert len(keys) == len(set(keys))
    assert all(event["title"] and event["desc"] and event["bonus"] >= 0 for event in c.WORLD_EVENTS)


def test_user_profile_fields_are_limited_and_isolated_per_chat():
    assert c.set_user_profile(-5001, 42, 'nickname', '  Alice  ')
    assert c.set_user_profile(-5001, 42, 'title', 'Helper')
    assert c.set_user_profile(-5001, 42, 'bio', 'Hello')
    assert c.get_user_profile(-5001, 42) == {'nickname': 'Alice', 'title': 'Helper', 'bio': 'Hello'}
    assert c.get_user_profile(-5002, 42) == {'nickname': '', 'title': '', 'bio': ''}
    assert not c.set_user_profile(-5001, 42, 'coins', '999999')


def test_settings_export_import_roundtrip_and_chat_isolation():
    c.set_chat_setting(-6001, 'custom_prefix', '!')
    assert c.set_chat_note(-6001, 'rules', 'Be kind', 77)
    assert c.set_chat_filter(-6001, 'bad phrase', 'Please keep it civil', 77)
    payload = c.export_chat_settings(-6001)
    assert c.import_chat_settings(-6002, payload)
    imported = c.export_chat_settings(-6002)
    assert imported['settings']['custom_prefix'] == '!'
    assert imported['notes'][0]['body'] == 'Be kind'
    assert imported['filters'][0]['trigger'] == 'bad phrase'
    assert c.export_chat_settings(-6003)['settings'] == {}


def test_settings_import_rejects_wrong_schema_version():
    assert not c.import_chat_settings(-6001, {'version': 999, 'settings': {}})


def test_chat_activity_leaderboard_uses_real_rolling_periods():
    base = 2_000_000_000
    for i in range(4):
        c.add_message(-9001, 100, 'alice', now=base - 2 * 86400 + i)
    for i in range(2):
        c.add_message(-9001, 200, 'bob', now=base - 20 * 86400 + i)
    c.add_message(-9001, 300, 'carol', now=base - 40 * 86400)
    weekly = c.activity_leaderboard(-9001, 'week', now=base)
    monthly = c.activity_leaderboard(-9001, 'month', now=base)
    assert [(x['user_id'], x['messages']) for x in weekly] == [(100, 4)]
    assert [(x['user_id'], x['messages']) for x in monthly] == [(100, 4), (200, 2)]
