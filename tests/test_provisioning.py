import pytest

from telemetry import provisioning

WANTED = {
    "dev_addr": "01000001",
    "app_s_key": "AA" * 16,
    "nwk_s_enc_key": "bb" * 16,
    "s_nwk_s_int_key": "bb" * 16,
    "f_nwk_s_int_key": "bb" * 16,
}


def test_activation_matches_ignores_case():
    current = {k: v.lower() for k, v in WANTED.items()} | {"f_cnt_up": 33}
    assert provisioning.activation_matches(current, WANTED)


def test_activation_missing_does_not_match():
    assert not provisioning.activation_matches(None, WANTED)


@pytest.mark.parametrize("field", provisioning.ACTIVATION_FIELDS)
def test_any_changed_field_does_not_match(field):
    current = dict(WANTED)
    current[field] = "ff" * 4 if field == "dev_addr" else "ff" * 16
    assert not provisioning.activation_matches(current, WANTED)


def _fetcher(total):
    calls = []

    def fetch(limit, offset):
        calls.append((limit, offset))
        return list(range(offset, min(offset + limit, total))), total

    return fetch, calls


def test_list_all_pages_past_the_first_page():
    fetch, calls = _fetcher(250)
    assert provisioning.list_all(fetch, page_size=100) == list(range(250))
    assert calls == [(100, 0), (100, 100), (100, 200)]


def test_list_all_exact_page_boundary_and_empty():
    fetch, calls = _fetcher(200)
    assert len(provisioning.list_all(fetch, page_size=100)) == 200
    assert calls == [(100, 0), (100, 100)]
    fetch, _ = _fetcher(0)
    assert provisioning.list_all(fetch) == []


def test_list_all_stops_on_short_count():
    # A server that reports a larger total than it returns must not loop forever.
    def fetch(limit, offset):
        return ([] if offset else [1, 2]), 10

    assert provisioning.list_all(fetch) == [1, 2]
