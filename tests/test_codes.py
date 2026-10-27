import re

import pytest

from marinremote.codes import fingerprint, make_code, make_link, parse_code, pretty

KEY = "ab" * 32
RELAY = "https://euw1-1.relay.n0.iroh.link./"


def test_round_trip_with_n0_relay():
    code = make_code(KEY, RELAY)
    assert code.startswith("mr-") and len(code) < 70
    assert parse_code(code) == {"id": KEY, "relay": RELAY}


def test_round_trip_custom_relay_and_none():
    custom = "https://relay.example.org/"
    assert parse_code(make_code(KEY, custom))["relay"] == custom
    assert parse_code(make_code(KEY))["relay"] is None


def test_pasted_with_spaces_and_line_breaks():
    code = make_code(KEY, RELAY)
    assert parse_code("  " + code[:20] + "\n  " + code[20:] + "\t")["id"] == KEY
    assert parse_code(pretty(code))["id"] == KEY


def test_bare_hex_key_is_accepted():
    assert parse_code(KEY.upper()) == {"id": KEY, "relay": None}


@pytest.mark.parametrize("bad", ["", "hello", "mr-", "mr-0OIl", "mr-111", KEY[:-2]])
def test_garbage(bad):
    with pytest.raises(ValueError):
        parse_code(bad)


def test_a_typo_is_caught():
    code = make_code(KEY, RELAY)
    for i in range(3, len(code)):
        typo = code[:i] + ("2" if code[i] != "2" else "3") + code[i + 1:]
        with pytest.raises(ValueError):
            parse_code(typo)


def test_cut_short_is_caught():
    code = make_code(KEY, RELAY)
    with pytest.raises(ValueError, match="целиком"):
        parse_code(code[:-4])


def test_marincall_invite_is_told_apart():
    with pytest.raises(ValueError, match="MarinCall"):
        parse_code("marin-3yZe7d3Cs1Bm8Pq")


def test_newer_version_says_so():
    from marinremote import codes
    body = bytes([2]) + bytes.fromhex(KEY) + bytes([0])
    code = codes.PREFIX + codes.b58encode(body + codes._check(body))
    with pytest.raises(ValueError, match="новой версии"):
        parse_code(code)


def test_fingerprint_is_short_stable_and_readable():
    fp = fingerprint(KEY)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}", fp)
    assert fp == fingerprint(KEY)
    assert fp != fingerprint("cd" * 32)


def test_links_as_browsers_hand_them_over():
    code = make_code(KEY, "https://euc1-1.relay.n0.iroh.link./")
    link = make_link(KEY, "https://euc1-1.relay.n0.iroh.link./")
    assert link == "marinremote://" + code
    want = {"id": KEY, "relay": "https://euc1-1.relay.n0.iroh.link./"}
    for text in (link, link + "/", link + "/?from=chat", link + "#x", "MarinRemote://" + code,
                 "marinremote:" + code, "marinremote://" + code.replace("-", "%2D"), f" {link}\n"):
        assert parse_code(text) == want, text
    with pytest.raises(ValueError):
        parse_code("marinremote://")
    with pytest.raises(ValueError):
        parse_code("marinremote://marin-abc")


def test_connect_argument_and_the_registered_command():
    import app
    from marinremote.system import link_command
    assert app.link_arg(["--connect", "mr-abc"]) == "mr-abc"
    assert app.link_arg(["--autostart"]) is None
    assert app.link_arg(["--connect"]) is None
    assert app.link_arg(["marinremote://mr-abc/"]) == "marinremote://mr-abc/"
    exe = r"C:\Program Files\MR\MarinRemote.exe"
    assert link_command(exe) == f'"{exe}" --connect "%1"'


def test_pair_code_takes_every_part():
    from marinremote.codes import pair_code
    a, b, na, nb = "aa" * 32, "bb" * 32, b"\1" * 32, b"\2" * 32
    code = pair_code(a, b, na, nb)
    assert re.match(r"^\d{3} \d{3}$", code) and code == pair_code(a, b, na, nb)
    others = {pair_code(b, a, na, nb), pair_code(a, b, nb, na), pair_code(a, b, na, b"\3" * 32)}
    assert code not in others
