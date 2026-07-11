"""notifier.py v2 (Task 8 req 5): HTML parse mode, 3500-char chunking, env-only
credentials, dry-run prints chunks, and a failed send returns False (so brief
exits non-zero) with the response body logged.
"""

import responses

from flight_deals import output
from flight_deals.notifier import CHUNK_LIMIT, TelegramNotifier, chunk_message


def test_chunk_message_respects_limit_and_line_boundaries():
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(200))
    chunks = chunk_message(text, limit=CHUNK_LIMIT)
    assert all(len(c) <= CHUNK_LIMIT for c in chunks)
    assert len(chunks) > 1
    # Re-joining the chunks reproduces the original lines (split only on \n).
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_hard_splits_an_overlong_single_line():
    chunks = chunk_message("z" * (CHUNK_LIMIT * 2 + 10), limit=CHUNK_LIMIT)
    assert all(len(c) <= CHUNK_LIMIT for c in chunks)
    assert "".join(chunks) == "z" * (CHUNK_LIMIT * 2 + 10)


def test_chunk_never_splits_an_html_tag_or_entity():
    """A long line of ``<a href>`` deep links (with an ``&amp;`` entity in each
    URL) must chunk on the spaces between links, never mid-tag or mid-entity —
    otherwise HTML parse mode 400s the send."""
    links = [
        f'<a href="https://x.example/a?b=1&amp;c={i}">link{i}</a>' for i in range(40)
    ]
    line = " ".join(links)
    chunks = chunk_message(line, limit=120)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 120
        # balanced angle brackets => no tag was cut in half
        assert c.count("<") == c.count(">")
        # every '&' is a whole '&amp;' => no entity was cut
        assert c.count("&") == c.count("&amp;")


def test_chunk_splits_a_single_overlong_anchor_without_corrupting_it():
    """A single ``<a href>`` anchor with no internal spaces (a real deep link
    with a long query string) can itself exceed the limit. The fallback split
    must never land mid-tag or mid-entity — every produced chunk must have
    balanced ``<``/``>`` and never end inside an ``&...;`` entity."""
    huge_query = "x" * (CHUNK_LIMIT * 2) + "&amp;tail=1"
    line = f'<a href="https://x.example/a?{huge_query}">anchor</a>'
    # The only space is inside the opening tag itself (`<a href=`), so it is
    # never a valid split point — the whole anchor is one unbreakable token.
    assert " " not in line[line.index(">"):]
    chunks = chunk_message(line, limit=CHUNK_LIMIT)
    assert len(chunks) > 1
    assert "".join(chunks) == line
    for c in chunks:
        assert c.count("<") == c.count(">")
        # no chunk ends mid-entity: every '&' still present is part of a whole
        # '&...;' entity fully contained in this chunk.
        pos = 0
        while True:
            amp = c.find("&", pos)
            if amp == -1:
                break
            semi = c.find(";", amp)
            assert semi != -1, f"chunk ends inside an entity: {c!r}"
            pos = semi + 1


def test_dry_run_prints_chunks_and_does_not_send(capsys):
    n = TelegramNotifier(token="t", chat_id="c")
    assert n.send("hello\nworld", dry_run=True) is True
    out = capsys.readouterr().out
    assert "telegram chunk 1/1" in out and "hello" in out


def test_unconfigured_send_returns_false(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = TelegramNotifier()
    assert n.configured is False
    assert n.send("hi") is False


@responses.activate
def test_send_success_uses_html_parse_mode():
    responses.add(responses.POST, "https://api.telegram.org/botTOK/sendMessage", json={"ok": True}, status=200)
    n = TelegramNotifier(token="TOK", chat_id="42")
    assert n.send("<b>hi</b>") is True
    sent = responses.calls[0].request
    import json as _json
    body = _json.loads(sent.body)
    assert body["parse_mode"] == "HTML" and body["chat_id"] == "42"


@responses.activate
def test_failed_send_returns_false_and_logs_body(caplog):
    responses.add(responses.POST, "https://api.telegram.org/botTOK/sendMessage",
                  json={"ok": False, "description": "Bad Request: can't parse entities"}, status=400)
    n = TelegramNotifier(token="TOK", chat_id="42")
    assert n.send("oops") is False
    assert "can't parse entities" in caplog.text


def test_telegram_html_from_envelope_escapes_and_links():
    deal = output.build_deal(
        shape="S2", origin="BUD", destination="CFU", out_date="2026-08-22",
        return_date="2026-08-27", price_eur=89.0, price_confidence="exact",
        carriers=["ryanair"], legs=[], why="cheap",
    )
    env = output.envelope(results=[deal], summary="Found 1 deal <BUD>", sources={}, next=[])
    html = output.telegram_text(env, html=True)
    assert "&lt;BUD&gt;" in html  # summary escaped
    assert "<b>" in html and "<a href=" in html  # bold summary + booking link
