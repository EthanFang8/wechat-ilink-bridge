# wechat-ilink-bridge

A lightweight, dependency-minimal bridge for WeChat's official **iLink / ClawBot Bot channel**.
It connects a WeChat 1:1 bot chat to plain local files, so any program — in particular an AI
agent — can receive and reply to WeChat messages without a web framework, a database, or a
heavyweight SDK.

`bridge.py` long-polls the iLink `getupdates` API and writes incoming messages as JSON files
into `inbox/`; replies placed in `outbox/pending/` are sent back through `sendmessage`.
That is the whole integration surface: read a file, write a file.

## 中文说明

微信官方 iLink / ClawBot Bot 通道的轻量桥接实现。`bridge.py` 用长轮询收微信私聊消息，落盘到
`inbox/`（JSON 文件）；写到 `outbox/pending/` 的回复会被自动发出。任何程序——尤其是 AI
Agent——只要会读写文件，就能收发微信消息，不需要 Web 框架、数据库或重型 SDK。除生成登录
二维码需要 `qrcode[pil]` 外，只用 Python 标准库。

## Features

- **Zero runtime dependencies** — Python standard library only (`urllib`); the optional
  `qrcode[pil]` package is used solely to render the login QR code as a PNG.
- **File-based interface** — `inbox/` for incoming messages, `outbox/pending/` for replies,
  `outbox/sent/` as an archive. Easy to wire into scripts, cron jobs, hooks, or agent runtimes.
- **Credential hygiene** — the bot token lives only in `state/session.json` (mode `0600`,
  atomic writes), is never printed, and is covered by `.gitignore` together with all runtime
  state.
- **Honest failure handling** — token expiry (errcode `-14`, the channel has no refresh)
  writes a `state/NEEDS_RELOGIN` marker instead of crash-looping; network errors back off
  and retry.
- **Single-file core** — the whole protocol implementation is one readable `bridge.py`
  (~630 lines): QR login, long-poll receive loop, context-token reply rules, status.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install "qrcode[pil]"

.venv/bin/python bridge.py login        # writes qrcode.png — scan it in WeChat
.venv/bin/python bridge.py wait-login   # wait until the scan is confirmed
.venv/bin/python bridge.py serve        # long-running receive/send loop
# or detached:
.venv/bin/python start_serve.py         # prints the PID, logs to serve.log

.venv/bin/python bridge.py send --text "hello"
.venv/bin/python bridge.py status       # login/runtime status (token never shown)
```

## Message format

Incoming (`inbox/<message_id>.json`):

```json
{"text": "...", "from_user_id": "...", "context_token": "...", "message_id": "..."}
```

Outgoing (`outbox/pending/<name>.json`):

```json
{"to_user_id": "...", "text": "...", "context_token": "..."}
```

## Channel limitations (imposed by the official iLink Bot channel)

- 1:1 private chats only; no group chats.
- The bot cannot initiate a conversation: the user must message first, and replies ride on
  the cached `context_token`.
- Sending roughly 10 messages in a row without a user reply gets throttled until the user
  replies once.
- The bot token has **no refresh mechanism**; on expiry (`-14`) the owner must re-scan the
  login QR code.

## Security

- Never commit `state/`, `inbox/`, `outbox/`, `qrcode.png`, or `*.log` — the shipped
  `.gitignore` already excludes them.
- Run `serve` under an unprivileged user; treat `state/session.json` like a password.

## License

MIT — see [LICENSE](LICENSE).
