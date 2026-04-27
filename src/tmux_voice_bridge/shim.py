"""Voice-injection shim for tmux panes over HA Assist.

Tool-agnostic: whatever TUI is running in the target pane (Claude Code,
Codex, aider, a shell) receives the transcript as keystrokes. Exposes an
OpenAI-compatible /v1/chat/completions endpoint. Each incoming user
message is parsed against a small command grammar:

    switch to <target> [<session>]  -> change current target, confirm via TTS
    use <target> [<session>]        -> alias for switch
    go to <target> [<session>]      -> alias for switch
    where am i | current ...        -> speak current target via TTS

Anything else is injected into the current tmux target (local or SSH remote)
using tmux's paste-buffer mechanism, which avoids shell-escaping the message.
The shim returns a single space for injections so HA's TTS stays silent;
the target tool's own Stop hook (or equivalent) handles spoken replies.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / "tmux-voice-bridge"


def _config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "tmux-voice-bridge"


PORT = int(os.environ.get("TMUX_VOICE_PORT", "18790"))
BIND = os.environ.get("TMUX_VOICE_BIND", "127.0.0.1")
TARGET_FILE = Path(os.environ.get(
    "TMUX_VOICE_TARGET_FILE",
    str(_state_dir() / "target"),
))
HOSTS_FILE = Path(os.environ.get(
    "TMUX_VOICE_HOSTS_FILE",
    str(_config_dir() / "hosts.json"),
))
DEFAULT_TARGET = "local main"

# Default host map if no hosts.json is present. Keys are the tokens a user
# can say; values are the ssh config host to use (None = local tmux).
DEFAULT_HOSTS: dict[str, str | None] = {
    "local": None,
    "here": None,
}

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def load_hosts() -> dict[str, str | None]:
    if HOSTS_FILE.exists():
        try:
            raw = json.loads(HOSTS_FILE.read_text())
            return {**DEFAULT_HOSTS, **{k: v for k, v in raw.items()}}
        except json.JSONDecodeError as err:
            print(f"[shim] bad hosts.json ({err}); using defaults", file=sys.stderr)
    return dict(DEFAULT_HOSTS)


def load_target() -> tuple[str | None, str]:
    try:
        raw = TARGET_FILE.read_text().strip()
    except FileNotFoundError:
        raw = DEFAULT_TARGET
    parts = raw.split()
    if len(parts) == 2 and parts[0] == "local":
        return None, parts[1]
    if len(parts) == 3 and parts[0] == "ssh":
        return parts[1], parts[2]
    return None, "main"


def save_target(host: str | None, session: str) -> None:
    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"local {session}" if host is None else f"ssh {host} {session}"
    TARGET_FILE.write_text(line + "\n")


def describe_target(host: str | None, session: str) -> str:
    return f"local session {session}" if host is None else f"{host} session {session}"


SWITCH_RE = re.compile(
    r"^\s*(?:switch\s+to|use|go\s+to)\s+([a-z0-9-]+)(?:\s+(.+?))?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
WHERE_RE = re.compile(
    r"^\s*(?:where\s+am\s+i|current\s+target|what\s+target|where\s+are\s+we)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
LIST_RE = re.compile(
    r"^\s*(?:list\s+sessions?|what\s+sessions?|show\s+sessions?)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
SWITCH_FUZZY_RE = re.compile(
    r"^\s*(?:switch(?:\s+to)?|use|go(?:\s+to)?|jump\s+to|take\s+me\s+to|move\s+to)\s+(.+?)\s*[.!?]?\s*$",
    re.IGNORECASE,
)

LLM_ROUTER = os.environ.get("TMUX_VOICE_LLM_ROUTER", "").strip().lower()
LLM_ROUTER_TIMEOUT = float(os.environ.get("TMUX_VOICE_LLM_ROUTER_TIMEOUT", "15"))


def llm_route(phrase: str, hosts: dict[str, str | None]) -> tuple[str, str] | None:
    """Ask Claude Code (or another LLM) to map a fuzzy switch phrase to (host, session).

    Returns (host_token, session_token) on success, or None.
    Enabled when TMUX_VOICE_LLM_ROUTER is set (currently only "claude" is wired up).
    """
    if LLM_ROUTER != "claude":
        return None
    host_lines = []
    for tok, ssh_target in sorted(hosts.items()):
        sessions = list_sessions(ssh_target)
        sess_str = ", ".join(sessions) if sessions else "(none reachable)"
        host_lines.append(f"  - {tok}: sessions = [{sess_str}]")
    prompt = (
        "You are routing a voice command to a tmux pane.\n"
        "Hosts and their current tmux sessions:\n"
        + "\n".join(host_lines)
        + f"\n\nUser said: {phrase!r}\n\n"
        "Reply with EXACTLY one line: '<host_token>\\t<session_name>'.\n"
        "Use only host_tokens from the list above. Pick the closest matching session\n"
        "or invent a reasonable name if none match. If you cannot route, reply 'none'.\n"
        "No prose, no markdown, no explanation."
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=LLM_ROUTER_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        sys.stderr.write(f"llm_route: claude invocation failed: {exc}\n")
        return None
    if result.returncode != 0:
        sys.stderr.write(f"llm_route: claude exit {result.returncode}: {result.stderr.strip()}\n")
        return None
    line = (result.stdout or "").strip().splitlines()[0:1]
    if not line or line[0].lower() == "none":
        return None
    parts = re.split(r"[\t ]+", line[0].strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    host_tok, session = parts[0].lower(), parts[1].strip()
    if host_tok not in hosts:
        return None
    return host_tok, session



def _normalize(s: str) -> str:
    """Collapse a string for fuzzy comparison: lowercase, strip separators."""
    return re.sub(r"[\s\-_]+", "", s.lower())


def list_sessions(host: str | None) -> list[str]:
    """Return tmux session names on the given host (None = local)."""
    cmd = ["tmux", "list-sessions", "-F", "#{session_name}"]
    try:
        if host is None:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        else:
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host] + cmd,
                capture_output=True, text=True, timeout=10,
            )
        if result.returncode == 0:
            return [s for s in result.stdout.strip().splitlines() if s]
    except (subprocess.SubprocessError, OSError):
        pass
    return []


def match_session(spoken: str, sessions: list[str]) -> str | None:
    """Find the best matching session for a spoken token.

    Tries, in order:
      1. Exact match (case-insensitive)
      2. Normalized match (collapse spaces/hyphens/underscores, case-insensitive)
      3. Spoken words joined with hyphens or underscores
    Returns the actual session name, or None.
    """
    spoken_lower = spoken.lower()
    spoken_norm = _normalize(spoken)

    # Exact case-insensitive
    for s in sessions:
        if s.lower() == spoken_lower:
            return s

    # Normalized (ignore separators)
    for s in sessions:
        if _normalize(s) == spoken_norm:
            return s

    # Try joining spoken words with common separators
    words = spoken_lower.split()
    if len(words) > 1:
        for sep in ("-", "_", ""):
            joined = sep.join(words)
            for s in sessions:
                if s.lower() == joined:
                    return s

    return None


def parse_session_token(tok: str | None, host: str | None) -> tuple[str, str | None]:
    """Parse a spoken session token into (session_name, warning_or_none).

    Looks up real sessions on the target host and fuzzy-matches.
    Returns the matched session name and an optional warning/info message.
    """
    if not tok:
        return "main", None

    tok_lower = tok.lower().strip()

    # Handle number words
    if tok_lower in NUMBER_WORDS:
        tok_lower = str(NUMBER_WORDS[tok_lower])

    sessions = list_sessions(host)
    if not sessions:
        # Can't verify — use the token as-is, normalized with hyphens
        fallback = re.sub(r"\s+", "-", tok_lower)
        return fallback, None

    matched = match_session(tok_lower, sessions)
    if matched:
        return matched, None

    # No match — also try number-word substitution on multi-word input
    words = tok_lower.split()
    subst = []
    for w in words:
        subst.append(str(NUMBER_WORDS[w]) if w in NUMBER_WORDS else w)
    subst_str = "-".join(subst)
    matched = match_session(subst_str, sessions)
    if matched:
        return matched, None

    available = ", ".join(sorted(sessions))
    fallback = re.sub(r"\s+", "-", tok_lower)
    return fallback, f"No session matching '{tok}' found. Available: {available}. Trying '{fallback}'."


def handle_command(text: str, hosts: dict[str, str | None]) -> str | None:
    """Return a spoken response if text matched a command, else None."""
    def _do_switch(token: str, session_tok: str | None) -> str:
        host = hosts[token]
        session, warning = parse_session_token(session_tok, host)
        save_target(host, session)
        msg = f"Switched to {describe_target(host, session)}."
        if warning:
            msg = f"{warning} {msg}"
        return msg

    m = SWITCH_RE.match(text)
    if m:
        token = m.group(1).lower()
        session_tok = m.group(2)
        if token not in hosts and "-" in token:
            # Allow "host-session" as a single hyphenated token, e.g. "homer-aar".
            head, _, tail = token.partition("-")
            if head in hosts and tail:
                token = head
                session_tok = tail if not session_tok else f"{tail} {session_tok}"
        if token in hosts:
            return _do_switch(token, session_tok)
        # Token not in hosts — try LLM fallback before erroring.
        routed = llm_route(text, hosts)
        if routed is not None:
            return _do_switch(routed[0], routed[1])
        known = ", ".join(sorted(hosts)) or "(no hosts configured)"
        return f"Unknown target {token}. Try: {known}."
    fuzzy = SWITCH_FUZZY_RE.match(text)
    if fuzzy:
        routed = llm_route(text, hosts)
        if routed is not None:
            return _do_switch(routed[0], routed[1])
    if WHERE_RE.match(text):
        host, session = load_target()
        return f"Current target is {describe_target(host, session)}."
    if LIST_RE.match(text):
        host, session = load_target()
        sessions = list_sessions(host)
        if sessions:
            names = ", ".join(sorted(sessions))
            where = "locally" if host is None else f"on {host}"
            return f"Sessions {where}: {names}."
        return "No sessions found."
    return None


def inject_local(session: str, text: str) -> None:
    target = f"{session}:1.1"
    buf = f"voice-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "load-buffer", "-b", buf, "-"],
        input=text.encode(), check=True,
    )
    subprocess.run(["tmux", "send-keys", "-t", target, "C-u"], check=True)
    subprocess.run(
        ["tmux", "paste-buffer", "-b", buf, "-d", "-t", target],
        check=True,
    )
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def inject_remote(host: str, session: str, text: str) -> None:
    target = f"{session}:1.1"
    buf = f"voice-{uuid.uuid4().hex[:8]}"
    remote = (
        f"tmux load-buffer -b {buf} - && "
        f"tmux send-keys -t {target} C-u && "
        f"tmux paste-buffer -b {buf} -d -t {target} && "
        f"tmux send-keys -t {target} Enter"
    )
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, remote],
        input=text.encode(), check=True,
    )


def do_inject(text: str) -> None:
    host, session = load_target()
    if host is None:
        inject_local(session, text)
    else:
        inject_remote(host, session, text)


def extract_user_text(body: dict) -> str:
    messages = body.get("messages") or []
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            return "".join(parts).strip()
        return str(content).strip()
    return ""


def build_completion(content: str, body: dict) -> tuple[dict, list[dict]]:
    """Return (non_stream_payload, stream_chunks) for the given assistant content."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = body.get("model") or "tmux-voice-bridge"
    non_stream = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    chunks = [
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": content},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    return non_stream, chunks


def make_handler(hosts: dict[str, str | None]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            print(f"[shim] {fmt % args}", file=sys.stderr, flush=True)

        def _send_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = self.path.rstrip("/")
            if path in ("", "/"):
                self._send_json({"status": "ok", "service": "tmux-voice-bridge"})
                return
            if path == "/v1/models":
                self._send_json({
                    "object": "list",
                    "data": [
                        {
                            "id": "tmux-voice-bridge",
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "local",
                        }
                    ],
                })
                return
            self.send_error(404, "not found")

        def do_POST(self) -> None:
            if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
                self.send_error(404, "not found")
                return
            length = int(self.headers.get("content-length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self.send_error(400, "invalid json")
                return

            user_text = extract_user_text(body)
            print(f"[shim] received: {user_text!r}", file=sys.stderr, flush=True)

            response_text = " "
            if user_text:
                command_response = handle_command(user_text, hosts)
                if command_response is not None:
                    response_text = command_response
                    print(f"[shim] command -> {response_text!r}",
                          file=sys.stderr, flush=True)
                else:
                    try:
                        do_inject(user_text)
                        print("[shim] injected", file=sys.stderr, flush=True)
                    except subprocess.CalledProcessError as err:
                        response_text = f"Injection failed: {err}"
                        print(f"[shim] {response_text}",
                              file=sys.stderr, flush=True)

            stream = bool(body.get("stream"))
            non_stream, chunks = build_completion(response_text, body)

            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                data = json.dumps(non_stream).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    return Handler


def main() -> None:
    hosts = load_hosts()
    handler = make_handler(hosts)
    server = ThreadingHTTPServer((BIND, PORT), handler)
    host, session = load_target()
    print(
        f"[shim] listening {BIND}:{PORT}  initial target: "
        f"{describe_target(host, session)}  hosts: {list(hosts)}",
        file=sys.stderr, flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[shim] shutting down", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
