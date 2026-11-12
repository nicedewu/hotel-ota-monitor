from src.event_source_health import entries_fingerprint


def test_entry_fingerprint_is_stable_for_equivalent_key_order() -> None:
    first = [{"title": "活动", "url": "https://example.test/a"}]
    second = [{"url": "https://example.test/a", "title": "活动"}]

    assert entries_fingerprint(first) == entries_fingerprint(second)
