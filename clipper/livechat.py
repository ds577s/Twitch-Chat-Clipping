"""Read a channel's live chat from Twitch IRC, anonymously (no account or token needed).

Runs in a background thread and puts events on a queue:
  ("msg",  wall_time, login, body, bits)
  ("gift", wall_time, login, count)      # mass gift subs
  ("raid", wall_time, login, viewers)
  ("info", wall_time, text)              # connection status, for the console
Sub/resub/etc. notices are dropped here: live chat tags them, so no text matching is needed.
"""

from __future__ import annotations

import queue
import random
import socket
import ssl
import threading
import time

HOST, PORT = "irc.chat.twitch.tv", 6697
READ_TIMEOUT = 360  # Twitch PINGs about every 5 minutes; silence longer than this = dead link


_TAG_ESCAPES = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}


def _unescape_tag(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\" and i + 1 < len(v):
            out.append(_TAG_ESCAPES.get(v[i + 1], v[i + 1]))
            i += 2
        else:
            if v[i] != "\\":
                out.append(v[i])
            i += 1
    return "".join(out)


def parse_line(line: str) -> tuple[dict, str, str, list[str], str]:
    """Parse one IRC line into (tags, prefix, command, params, trailing)."""
    tags: dict[str, str] = {}
    prefix = trailing = ""
    if line.startswith("@"):
        raw, line = line[1:].split(" ", 1)
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape_tag(v)
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1) if " " in line else (line[1:], "")
    if " :" in line:
        line, trailing = line.split(" :", 1)
    parts = line.split()
    return tags, prefix, (parts[0] if parts else ""), parts[1:], trailing


def event_from_line(line: str, now: float):
    """Turn an IRC line into a queue event, or None if it's not interesting."""
    tags, prefix, cmd, _params, trailing = parse_line(line)
    if cmd == "PRIVMSG":
        login = prefix.split("!", 1)[0].lower()
        body = trailing
        if body.startswith("\x01ACTION ") and body.endswith("\x01"):  # /me messages
            body = body[8:-1]
        try:
            bits = int(tags.get("bits", 0) or 0)
        except ValueError:
            bits = 0
        return ("msg", now, login, body, bits)
    if cmd == "USERNOTICE":
        kind = tags.get("msg-id", "")
        login = tags.get("login", "").lower()
        if kind == "submysterygift":
            return ("gift", now, login, int(tags.get("msg-param-mass-gift-count", 1) or 1))
        if kind == "raid":
            return ("raid", now, login, int(tags.get("msg-param-viewerCount", 0) or 0))
    return None


class LiveChat(threading.Thread):
    def __init__(self, channel: str, events: queue.Queue):
        super().__init__(daemon=True, name=f"chat-{channel}")
        self.channel = channel.lower().lstrip("#")
        self.events = events
        self._halt = threading.Event()  # not "_stop": that name is used inside threading.Thread
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        self._halt.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _info(self, text: str) -> None:
        self.events.put(("info", time.time(), text))

    def run(self) -> None:
        backoff = 1
        while not self._halt.is_set():
            try:
                self._session()
                backoff = 1
            except (OSError, ssl.SSLError) as e:
                if self._halt.is_set():
                    break
                self._info(f"chat connection lost ({e}); reconnecting in {backoff}s")
            if self._halt.wait(backoff):
                break
            backoff = min(backoff * 2, 60)

    def _session(self) -> None:
        raw = socket.create_connection((HOST, PORT), timeout=30)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
        sock.settimeout(READ_TIMEOUT)
        self._sock = sock
        nick = f"justinfan{random.randint(10000, 99999)}"  # Twitch's anonymous read-only login
        sock.sendall(("CAP REQ :twitch.tv/tags twitch.tv/commands\r\n"
                      f"NICK {nick}\r\nJOIN #{self.channel}\r\n").encode())
        buf = b""
        joined = False
        while not self._halt.is_set():
            try:
                data = sock.recv(65536)
            except socket.timeout:
                raise OSError("no data from Twitch chat for 6 minutes")
            if not data:
                raise OSError("Twitch closed the chat connection")
            buf += data
            *lines, buf = buf.split(b"\r\n")
            now = time.time()
            for b in lines:
                line = b.decode("utf-8", errors="replace")
                if line.startswith("PING"):
                    sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                    continue
                try:
                    _, _, cmd, _, _ = parse_line(line)
                    ev = event_from_line(line, now)
                except ValueError:   # a malformed line must not stop the chat reader
                    continue
                if cmd == "RECONNECT":
                    raise OSError("Twitch asked us to reconnect")
                if cmd in ("ROOMSTATE", "366") and not joined:
                    joined = True
                    self._info(f"connected to #{self.channel} chat")
                if ev:
                    self.events.put(ev)
