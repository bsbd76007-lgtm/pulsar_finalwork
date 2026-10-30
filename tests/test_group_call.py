from app import group_call


def test_render_mentions_escapes_names_and_splits_chunks():
    members = [
        {"user_id": 101, "full_name": "Alice & Bob", "username": "alice"},
        {"user_id": 102, "full_name": "<Admin>", "username": "admin"},
    ]
    chunks = group_call.render_mentions(members)
    assert len(chunks) == 1
    assert 'tg://user?id=101' in chunks[0]
    assert 'Alice &amp; Bob' in chunks[0]
    assert '&lt;Admin&gt;' in chunks[0]


def test_render_mentions_chunks_large_roster():
    members = [{"user_id": i, "full_name": f"Member {i}", "username": ""} for i in range(1, 60)]
    chunks = group_call.render_mentions(members)
    assert len(chunks) == 3
    assert sum(chunk.count('tg://user?id=') for chunk in chunks) == 59
