# tmux-voice-bridge

Hands-free, eyes-free voice interface to any TUI running in a tmux pane —
Claude Code, Codex, aider, a shell, whatever. Earbuds speak → Home Assistant
Assist transcribes → local bridge types the words into the target pane.
Replies come back through the target tool's own TTS hook, not HA's.

It exposes an OpenAI-compatible `/v1/chat/completions` endpoint that HA's
conversation agent can point at. Transcripts matching a small command
grammar change the current target; anything else is injected as keystrokes
into the current tmux pane (local or over SSH).

## Install

```sh
pipx install tmux-voice-bridge
# or
pip install --user tmux-voice-bridge
```

Then wire it up as a systemd user service:

```sh
mkdir -p ~/.config/systemd/user
curl -fsSL https://raw.githubusercontent.com/davidj4tech/tmux-voice-bridge/main/systemd/tmux-voice-bridge.service \
  -o ~/.config/systemd/user/tmux-voice-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now tmux-voice-bridge
```

## Configure hosts

By default only `local` (and alias `here`) are known — both map to local
tmux. To add remote SSH targets, drop a `hosts.json` at
`$XDG_CONFIG_HOME/tmux-voice-bridge/hosts.json` (default
`~/.config/tmux-voice-bridge/hosts.json`):

```json
{
  "homer": "homer",
  "devbox": "devbox",
  "eleven": "11"
}
```

Keys are the tokens you say; values are the SSH config host name (or
`null` for local tmux). The SSH alias must work non-interactively (key
auth; ControlMaster recommended).

## Voice commands

Speak these through HA Assist. They're parsed before injection — anything
not matching a command is sent into the current target pane.

### Change target

```
switch to <host> [<session>]
use <host> [<session>]
go to <host> [<session>]
```

Session token is any alphanumeric name plus `-` or `_` (e.g. `main`, `5`,
`myproject`, `foo-bar`). Word-numbers `one` through `ten` are converted
to digits. If omitted, session defaults to `main`.

As a shorthand, the host and session can be joined with a hyphen — useful
when speech recognition runs them together: `switch to homer-myproject`
parses identically to `switch to homer myproject`.

**Examples**

- "switch to local" → local session `main`
- "switch to local three" → local session `3`
- "switch to homer 5" → session `5` on homer
- "switch to homer myproject" → session `myproject` on homer
- "switch to homer-myproject" → same as above (hyphenated form)

### Check current target

```
where am I
current target
what target
where are we
```

Spoken reply: "Current target is &lt;host&gt; session &lt;name&gt;."

An utterance that *starts* with a switch verb but whose first token isn't
a known host (e.g. "use my GTD inbox", "go to line 42") is **not** treated
as a malformed switch command — it falls through to plain text injection,
so it just gets typed into the current pane. This avoids the verb-prefix
grammar swallowing natural speech.

### Anything else

Is injected as keystrokes into the current target pane: clear line
(`C-u`), type the text, press Enter. The HTTP response is a one-line
confirmation — `Sent to <host> session <name>.` — so HA's UI always
shows where the message went (otherwise the chat just shows a blank
reply). Substantive replies come from the target tool's own TTS hook,
not from this confirmation.

## HA wiring

Point HA's conversation agent at `http://127.0.0.1:18790/v1/chat/completions`
(or wherever you bound it). Any integration that speaks OpenAI-compatible
chat completions works — the
[OpenClaw](https://github.com/openclaw/openclaw) custom integration is
one option.

Typical pipeline:

```
earbuds ─(BT)─► phone (HA Assist app)
                  │
                  ▼  (HTTPS)
               Home Assistant
                  │  STT: Whisper / OpenAI
                  │  Conversation: pipeline -> OpenAI-compatible integration
                  │                       -> http://127.0.0.1:18790/v1/chat/completions
                  ▼
               tmux-voice-bridge (systemd user service)
                  │  parse command OR
                  │  tmux load-buffer / paste-buffer / send-keys Enter
                  ▼
               target pane  (local tmux OR ssh <host> ...)
                  │
                  ▼
               Claude Code / Codex / aider / shell
                  │
                  ▼  (tool's own Stop hook → TTS → phone)
               earbuds hear reply
```

Replies are the target tool's responsibility. For Claude Code, a Stop
hook writes audio to a watched dir and a separate process ships it to
the phone. Codex and others need their own equivalent.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TMUX_VOICE_PORT` | `18790` | HTTP port |
| `TMUX_VOICE_BIND` | `127.0.0.1` | Bind address |
| `TMUX_VOICE_TARGET_FILE` | `$XDG_STATE_HOME/tmux-voice-bridge/target` | Persisted current target |
| `TMUX_VOICE_HOSTS_FILE` | `$XDG_CONFIG_HOME/tmux-voice-bridge/hosts.json` | Host map |
| `TMUX_VOICE_ENTER_DELAY` | `0.12` | Seconds between paste-buffer and the trailing `Enter`. Avoids a race where TUIs (e.g. Claude Code's Ink renderer) absorb `Enter` into the pasted text instead of submitting. Set to `0` to disable. |

## Prerequisites for a new session

The tmux session has to exist on the target host before voice injection
will work. Create it ahead of time:

- Local: `tmux new-session -d -s myproject`
- Remote: `ssh homer tmux new-session -d -s myproject`

Or rename the current one on the fly from inside: `Ctrl+B $`.

## SSH tip: numbered sessions

One nice convention: `homer` = bare shell (no tmux), `homerN` = attach
or create tmux session `N` on homer. SSH `Host` patterns don't support
`[0-9]` character classes, so enumerate explicitly:

```sshconfig
Host homer
  HostName 100.125.48.108
  User mel

Host homer1 homer2 homer3 homer4 homer5 homer6 homer7 homer8 homer9 homer10
  HostName 100.125.48.108
  User mel
  RequestTTY yes
  RemoteCommand tmux -u new -A -D -s "$(echo %n | sed 's/^homer//')"
```

## How the injection works

Each injection does:

```
tmux load-buffer -b <tmpbuf> -   (text via stdin, no shell escaping)
tmux send-keys   -t <session>:1.1 C-u
tmux paste-buffer -b <tmpbuf> -d -t <session>:1.1
tmux send-keys   -t <session>:1.1 Enter
```

For SSH targets the same four commands run remotely over a single ssh
invocation, with the transcript piped in on stdin. Target pane is
`:1.1` (window 1, pane 1) by default.

## License

MIT
