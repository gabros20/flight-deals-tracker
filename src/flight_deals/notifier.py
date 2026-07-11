"""Telegram notifier v2 (UPGRADE-PLAN §6, brief req 5).

A deliberate rewrite of the legacy notifier, which used Markdown parse mode
(silently 400s on an unescaped ``_`` in a booking URL) and swallowed every
failure. This version:

* uses **HTML** parse mode (robust for the deep links in a digest);
* **chunks** a long message at ~3500 chars (under Telegram's 4096 cap) on line
  boundaries, sending each chunk as its own message;
* sources credentials **only** from the environment (``TELEGRAM_BOT_TOKEN`` /
  ``TELEGRAM_CHAT_ID``) — never a config file (Global Constraint 8);
* on a failed send **logs the response body** and returns ``False`` so the
  caller (``brief``) exits non-zero and the cron log surfaces it;
* supports ``dry_run``: prints the chunks that *would* be sent, no network.

The message body itself is built by ``output.telegram_text`` from the frozen
envelope — the notifier never formats deals itself (one renderer rule).
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
CHUNK_LIMIT = 3500  # under the 4096 hard cap, room for HTML entity expansion


def _split_long_line(line: str, limit: int) -> List[str]:
    """Split a single over-``limit`` line, breaking at the last space BEFORE the
    limit that is NOT inside an HTML tag (``<...>``) or entity (``&...;``) — a
    naive ``line[:limit]`` could cut through a ``<a href>`` deep link and 400 the
    send under HTML parse mode.

    When a single token (no safe space) exceeds the limit — e.g. one long
    ``<a href=...>`` anchor — fall back to ``safe_cut``: the last position
    scanned that is outside any tag/entity, so the split still never lands
    mid-tag/mid-entity. Only in the fully degenerate case (a tag/entity opens
    at index 0 and doesn't close within ``limit`` chars — a single token that
    IS one giant tag) do we extend the scan past ``limit`` to that token's own
    close, keeping the chunk balanced even though it now exceeds ``limit``. A
    raw hard split at ``limit`` is used only if the tag/entity never closes at
    all. Every branch strictly shrinks ``line``, so the loop terminates."""
    pieces: List[str] = []
    while len(line) > limit:
        inside_tag = inside_ent = False
        ent_start = 0
        last_space = -1
        safe_cut = 0  # last index i where line[:i] is tag/entity-balanced
        for i, ch in enumerate(line[:limit]):
            if inside_tag:
                if ch == ">":
                    inside_tag = False
                    safe_cut = i + 1
                continue
            if inside_ent:
                if ch == ";":
                    inside_ent = False
                    safe_cut = i + 1
                    continue
                if ch != " " and (i - ent_start) <= 12:
                    continue
                inside_ent = False  # a bare '&', not an entity — fall through
            if ch == "<":
                inside_tag = True
            elif ch == "&":
                inside_ent = True
                ent_start = i
            else:
                if ch == " ":
                    last_space = i
                safe_cut = i + 1
        if last_space > 0:
            pieces.append(line[:last_space])
            line = line[last_space + 1:]  # drop the break space
        elif safe_cut > 0:  # no space, but a non-tag/entity boundary exists
            pieces.append(line[:safe_cut])
            line = line[safe_cut:]
        else:  # degenerate: a tag/entity opens at index 0 (safe_cut never
            # advanced) and doesn't close within `limit` chars — extend the
            # scan to THAT construct's own close (not just any '>' or ';',
            # which could be a nested entity inside the still-open tag) so
            # the chunk stays balanced, even though it now exceeds `limit`.
            close_at = line.find(">" if inside_tag else ";", limit)
            if close_at == -1:  # never closes at all: unavoidable hard split
                pieces.append(line[:limit])
                line = line[limit:]
            else:
                pieces.append(line[:close_at + 1])
                line = line[close_at + 1:]
    if line:
        pieces.append(line)
    return pieces


def chunk_message(text: str, limit: int = CHUNK_LIMIT) -> List[str]:
    """Split ``text`` into <=``limit``-char chunks on line boundaries. A single
    line longer than ``limit`` is split by :func:`_split_long_line` so a chunk
    never cuts through an HTML tag/entity (falling back to a hard split only for
    a single over-long token)."""
    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            pieces = _split_long_line(line, limit)
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(pieces[:-1])  # all but the last are complete chunks
            line = pieces[-1]
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


class TelegramNotifier:
    """Env-credentialed Telegram sender. Construct with explicit token/chat_id
    only in tests; production always reads the environment."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, *, dry_run: bool = False, parse_mode: str = "HTML") -> bool:
        """Send ``text`` (chunked) to the configured chat. Returns ``True`` on
        full success, ``False`` on any failure (logged). ``dry_run`` prints the
        chunks instead of sending and always returns ``True``."""
        chunks = chunk_message(text)

        if dry_run:
            for i, c in enumerate(chunks, 1):
                print(f"--- telegram chunk {i}/{len(chunks)} ({len(c)} chars) ---")
                print(c)
            return True

        if not self.configured:
            logger.error(
                "telegram: not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
            )
            return False

        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        for i, c in enumerate(chunks, 1):
            payload = {
                "chat_id": self.chat_id,
                "text": c,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            try:
                resp = requests.post(url, json=payload, timeout=15)
            except requests.RequestException as e:
                logger.error("telegram: send failed (chunk %d/%d): %s", i, len(chunks), e)
                return False
            if resp.status_code != 200:
                logger.error(
                    "telegram: send failed (chunk %d/%d) HTTP %s: %s",
                    i, len(chunks), resp.status_code, resp.text,
                )
                return False
        logger.info("telegram: sent %d chunk(s) to %s", len(chunks), self.chat_id)
        return True
