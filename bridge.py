#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridge.py — 微信官方 iLink / ClawBot Bot 通道桥接（owner <-> WeChat 1:1 chat）

协议来源：Tencent/openclaw-weixin 官方仓库 docs/protocol.md + src/api/api.ts + src/auth/login-qr.ts
实现约束：只用 Python 标准库 urllib；二维码图片生成用 pip 包 qrcode。

命令：
  login        拿登录二维码，存 qrcode.png + state/pending_qrcode.json
  wait-login   轮询扫码状态直到 confirmed，把凭据原子写入 state/session.json
  serve        常驻：getupdates 长轮询收消息写 inbox/；outbox/pending/ 的回复经 sendmessage 发出
  send --text  用缓存的 context_token 发一条消息
  status       查看登录与运行状态（不打印 token）

安全红线：state/session.json 含 bot_token，只落盘（0600，原子写入），不打印、不进仓库。
token 失效的唯一信号是 ret/errcode == -14，没有 refresh 机制；失效后写 state/NEEDS_RELOGIN。
"""

import argparse
import base64
import json
import os
import random
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量（按官方协议规格，不要随意改）
# ---------------------------------------------------------------------------
FIXED_BASE_URL = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
CLIENT_VERSION = "132102"          # 0x00020406 -> channel 2.4.6
CHANNEL_VERSION = "2.4.6"
BOT_AGENT = "wechat-ilink-bridge/1.0"
BOT_TYPE = "3"

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
INBOX_DIR = ROOT / "inbox"
OUTBOX_PENDING = ROOT / "outbox" / "pending"
OUTBOX_SENT = ROOT / "outbox" / "sent"
SESSION_FILE = STATE_DIR / "session.json"
PENDING_QR_FILE = STATE_DIR / "pending_qrcode.json"
CURSOR_FILE = STATE_DIR / "cursor.json"
CONTEXT_FILE = STATE_DIR / "context.json"
PID_FILE = STATE_DIR / "serve.pid"
NEEDS_RELOGIN_FILE = STATE_DIR / "NEEDS_RELOGIN"
QRCODE_PNG = ROOT / "qrcode.png"

SESSION_EXPIRED = -14


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def ensure_dirs() -> None:
    for d in (STATE_DIR, INBOX_DIR, OUTBOX_PENDING, OUTBOX_SENT):
        d.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, obj, mode: int = 0o600) -> None:
    """原子写 JSON：先写同目录临时文件，再 os.replace，避免写一半损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    data = json.dumps(obj, ensure_ascii=False, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def load_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def random_wechat_uin() -> str:
    """X-WECHAT-UIN：随机 uint32 -> 十进制字符串 -> base64（与官方 TS 实现一致）。"""
    uint32 = random.SystemRandom().getrandbits(32)
    return base64.b64encode(str(uint32).encode("utf-8")).decode("ascii")


def base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def mark_needs_relogin(reason: str) -> None:
    ensure_dirs()
    NEEDS_RELOGIN_FILE.write_text(
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n", encoding="utf-8"
    )
    log("!! bot_token 已失效（errcode -14），已写 state/NEEDS_RELOGIN，需要重新扫码登录")


# ---------------------------------------------------------------------------
# HTTP 层（urllib）
# ---------------------------------------------------------------------------
def _request(method: str, base: str, endpoint: str, headers: dict,
             body: dict | None, timeout: float) -> dict:
    url = base.rstrip("/") + "/" + endpoint.lstrip("/")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"HTTP {e.code}: {raw[:300]}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise TimeoutError(str(e)) from e
    if not raw.strip():
        return {}
    return json.loads(raw)


def app_headers() -> dict:
    """iLink-App-Id + iLink-App-ClientVersion 两个头。"""
    return {
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
    }


def qr_post_headers() -> dict:
    """取二维码 POST 用：带 AuthorizationType + 随机 X-WECHAT-UIN，不带 Authorization。"""
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_wechat_uin(),
    }
    h.update(app_headers())
    return h


def authed_headers(token: str) -> dict:
    """已登录 API 用：再加 Authorization: Bearer <bot_token>，每次请求 UIN 重新随机。"""
    h = qr_post_headers()
    h["Authorization"] = f"Bearer {token}"
    return h


# ---------------------------------------------------------------------------
# 登录：login / wait-login
# ---------------------------------------------------------------------------
def cmd_login(args) -> int:
    ensure_dirs()
    # 请求体只有 local_token_list，不带 base_info（官方实现如此）
    resp = _request(
        "POST",
        FIXED_BASE_URL,
        f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
        qr_post_headers(),
        {"local_token_list": []},
        timeout=20,
    )
    qrcode = resp.get("qrcode")
    img_content = resp.get("qrcode_img_content")
    if not qrcode or not img_content:
        log(f"拿二维码失败，服务端返回异常：{json.dumps(resp, ensure_ascii=False)[:300]}")
        return 1

    atomic_write_json(PENDING_QR_FILE, {
        "qrcode": qrcode,
        "qrcode_img_content": img_content,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_ts": int(time.time()),
    }, mode=0o600)

    # qrcode_img_content 是个 HTTPS 链接，把它编码成二维码图片存下来
    try:
        import qrcode  # pip 包，需要 pillow 才能出 PNG：pip install "qrcode[pil]"
        img = qrcode.make(img_content)
        img.save(str(QRCODE_PNG))
        log(f"二维码已生成：{QRCODE_PNG}")
    except Exception as e:  # noqa: BLE001
        log(f"警告：二维码图片未生成（{e}）；请确认已 pip install \"qrcode[pil]\"。二维码链接已存在 state/pending_qrcode.json")
        return 1
    log("请用手机微信扫这个二维码（扫完后跑 wait-login）。二维码有时效，尽快扫。")
    return 0


def cmd_wait_login(args) -> int:
    ensure_dirs()
    pending = load_json(PENDING_QR_FILE)
    if not pending or not pending.get("qrcode"):
        log("没有进行中的登录：请先跑 login 拿二维码。")
        return 1
    qrcode = args.qrcode or pending["qrcode"]
    verify_code = args.verify_code or pending.get("pending_verify_code")
    poll_base = FIXED_BASE_URL
    deadline = time.time() + args.timeout
    scanned_printed = False

    log("开始轮询扫码状态（长轮询 35 秒）……")
    while time.time() < deadline:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={urllib.parse.quote(str(verify_code), safe='')}"
        try:
            # 轮询只带 iLink-App-Id 和 iLink-App-ClientVersion 两个头（官方实现如此）
            resp = _request("GET", poll_base, endpoint, app_headers(), None, timeout=35)
        except TimeoutError:
            resp = {"status": "wait"}   # 客户端超时按官方实现视为 wait，继续轮询
        except RuntimeError as e:
            log(f"轮询遇到网络/网关错误，继续重试：{e}")
            time.sleep(1)
            resp = {"status": "wait"}

        status = resp.get("status", "wait")
        if status == "wait":
            continue
        if status == "scaned":
            if not scanned_printed:
                log("已扫码，正在等你在手机上确认……")
                scanned_printed = True
            if verify_code:
                verify_code = None  # 验证码已被接受，清掉避免重复带
                pending.pop("pending_verify_code", None)
                atomic_write_json(PENDING_QR_FILE, pending, mode=0o600)
            continue
        if status == "scaned_but_redirect":
            host = resp.get("redirect_host")
            if host:
                poll_base = host if host.startswith("http") else f"https://{host}"
                log(f"服务端要求换节点继续轮询：{poll_base}")
            continue
        if status == "need_verifycode":
            if args.verify_code:
                log("服务端还要验证码且刚提交的没通过。请把手机微信上显示的数字告诉我，再重新跑 wait-login --verify-code <数字>。")
            else:
                log("服务端需要验证码：请把手机微信上显示的数字告诉我，我用 wait-login --verify-code <数字> 继续。")
            pending["pending_verify_code_needed"] = True
            atomic_write_json(PENDING_QR_FILE, pending, mode=0o600)
            return 3
        if status == "verify_code_blocked":
            log("验证码错太多次被拦了，请重新跑 login 拿新二维码。")
            return 1
        if status == "expired":
            log("二维码已过期，请重新跑 login 拿新二维码。")
            return 2
        if status == "binded_redirect":
            log("这个 Bot 已经绑定过（binded_redirect）。如果 state/session.json 已有凭据，可直接用；否则请重新 login。")
            return 4
        if status == "confirmed":
            session = {
                "bot_token": resp.get("bot_token"),
                "baseurl": resp.get("baseurl") or FIXED_BASE_URL,
                "ilink_bot_id": resp.get("ilink_bot_id"),
                "ilink_user_id": resp.get("ilink_user_id"),
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if not session["bot_token"]:
                log("confirmed 但没返回 bot_token，异常，请重试 login。")
                return 1
            atomic_write_json(SESSION_FILE, session, mode=0o600)
            if NEEDS_RELOGIN_FILE.exists():
                NEEDS_RELOGIN_FILE.unlink()
            try:
                PENDING_QR_FILE.unlink()
            except OSError:
                pass
            log("登录成功！凭据已保存到 state/session.json（0600，不显示内容）。")
            log(f"ilink_bot_id / ilink_user_id 已保存。baseurl={session['baseurl']}")
            return 0
        log(f"未知状态：{status}，继续轮询")
        time.sleep(1)

    log("等待超时（还没 confirmed），二维码可能已过期，请重新跑 login。")
    return 2


def load_session() -> dict | None:
    s = load_json(SESSION_FILE)
    if not s or not s.get("bot_token"):
        return None
    return s


def session_base(session: dict) -> str:
    return (session.get("baseurl") or FIXED_BASE_URL).rstrip("/")


# ---------------------------------------------------------------------------
# 发消息（sendmessage）
# ---------------------------------------------------------------------------
class SessionExpired(Exception):
    pass


def check_expired(resp: dict) -> None:
    if resp.get("ret") == SESSION_EXPIRED or resp.get("errcode") == SESSION_EXPIRED:
        raise SessionExpired(resp.get("errmsg") or "session expired (-14)")


def send_message(session: dict, to_user_id: str, text: str, context_token: str | None) -> dict:
    """sendmessage 关键细节（照官方 builder）：
    msg.from_user_id 必须是空字符串 ""；client_id 每次随机唯一；
    message_type=2、message_state=2；context_token 原样回传；顶层带 base_info。
    """
    msg = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": f"ilink-{uuid.uuid4().hex}",
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    body = {"msg": msg, "base_info": base_info()}
    resp = _request(
        "POST", session_base(session), "ilink/bot/sendmessage",
        authed_headers(session["bot_token"]), body, timeout=20,
    )
    check_expired(resp)
    if resp.get("ret") not in (None, 0):
        raise RuntimeError(f"sendmessage 返回 ret={resp.get('ret')} errmsg={resp.get('errmsg')}")
    return resp


def notify(session: dict, start: bool) -> None:
    """notifyStart/notifyStop：官方客户端生命周期通知，失败只记日志不阻断。"""
    try:
        endpoint = "ilink/bot/msg/notifystart" if start else "ilink/bot/msg/notifystop"
        _request("POST", session_base(session), endpoint,
                 authed_headers(session["bot_token"]), {"base_info": base_info()}, timeout=10)
    except Exception as e:  # noqa: BLE001
        log(f"notify{'Start' if start else 'Stop'} 失败（忽略）：{e}")


def cmd_send(args) -> int:
    ensure_dirs()
    session = load_session()
    if not session:
        log("还没登录：请先 login + wait-login。")
        return 1
    ctx = load_json(CONTEXT_FILE, {}) or {}
    to_user_id = args.to or ctx.get("to_user_id") or session.get("ilink_user_id")
    context_token = ctx.get("context_token")
    if not context_token:
        log("没有缓存的 context_token：官方限制——必须先由你在微信里给 Bot 发一条消息，我才能回复你。先发一条，我这边 serve 收到后就会缓存。")
        return 2
    try:
        resp = send_message(session, to_user_id, args.text, context_token)
    except SessionExpired:
        mark_needs_relogin("send 时收到 -14")
        return 5
    log(f"已发送。服务端返回 ret={resp.get('ret')}")
    return 0


# ---------------------------------------------------------------------------
# serve：常驻收发
# ---------------------------------------------------------------------------
def extract_text(msg: dict) -> str:
    parts = []
    for item in msg.get("item_list") or []:
        t = item.get("type")
        if t == 1:
            txt = (item.get("text_item") or {}).get("text")
            if txt:
                parts.append(txt)
        elif t == 3:
            # 语音消息若带转写文本，一并当文本处理
            txt = (item.get("voice_item") or {}).get("text")
            if txt:
                parts.append(txt)
    return "\n".join(parts).strip()


def handle_inbound(session: dict, msg: dict) -> None:
    if msg.get("group_id"):
        log("忽略群消息（iLink 只支持一对一私聊）")
        return
    from_user = msg.get("from_user_id") or ""
    owner = session.get("ilink_user_id") or ""
    if owner and from_user != owner:
        log(f"忽略非本人消息（from_user_id 不是扫码登录的那个微信）")
        return
    if msg.get("message_type") not in (None, 1):
        return  # 只处理用户消息（1），Bot 自己发的（2）不回写 inbox
    text = extract_text(msg)
    context_token = msg.get("context_token")
    if context_token:
        atomic_write_json(CONTEXT_FILE, {
            "to_user_id": from_user,
            "context_token": context_token,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_message_id": msg.get("message_id"),
        }, mode=0o600)
    if not text:
        log("收到一条非文本消息（图片/文件等），本桥接当前只处理文本，已跳过写 inbox")
        return
    mid = msg.get("message_id")
    fname = f"{mid}.json" if mid is not None else f"{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}.json"
    dest = INBOX_DIR / fname
    if dest.exists():
        return  # 去重：同一 message_id 不重复写
    record = {
        "message_id": mid,
        "text": text,
        "from_user_id": from_user,
        "context_token": context_token,
        "create_time_ms": msg.get("create_time_ms"),
        "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_write_json(dest, record, mode=0o644)
    log(f"收到新消息 -> inbox/{fname}（{len(text)} 字）")


def get_updates_once(session: dict, buf: str, timeout: float = 38) -> dict:
    body = {"get_updates_buf": buf or "", "base_info": base_info()}
    resp = _request("POST", session_base(session), "ilink/bot/getupdates",
                    authed_headers(session["bot_token"]), body, timeout=timeout)
    check_expired(resp)
    return resp


def serve_loop(args) -> int:
    ensure_dirs()
    session = load_session()
    if not session:
        log("还没登录：请先 login + wait-login。")
        return 1
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    stop = threading.Event()

    def _sig(_signum, _frame):
        stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    notify(session, start=True)
    log(f"serve 启动（pid={os.getpid()}），开始长轮询收消息……")

    def poll_loop():
        buf = (load_json(CURSOR_FILE, {}) or {}).get("get_updates_buf", "")
        backoff = 2
        while not stop.is_set():
            try:
                resp = get_updates_once(session, buf)
                backoff = 2
            except SessionExpired:
                mark_needs_relogin("getupdates 收到 -14")
                stop.set()
                return
            except TimeoutError:
                continue  # 长轮询超时是常态，直接下一轮
            except Exception as e:  # noqa: BLE001
                log(f"getupdates 出错，{backoff}s 后重试：{e}")
                stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            if resp.get("ret") not in (None, 0):
                log(f"getupdates 返回 ret={resp.get('ret')} errmsg={resp.get('errmsg')}，稍后重试")
                stop.wait(3)
                continue
            new_buf = resp.get("get_updates_buf")
            if new_buf:
                buf = new_buf
                atomic_write_json(CURSOR_FILE, {"get_updates_buf": buf,
                                                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, mode=0o600)
            for msg in resp.get("msgs") or []:
                try:
                    handle_inbound(session, msg)
                except Exception as e:  # noqa: BLE001
                    log(f"处理入站消息出错（忽略这一条）：{e}")

    def outbox_loop():
        while not stop.is_set():
            try:
                files = sorted(OUTBOX_PENDING.glob("*.json"), key=lambda p: p.stat().st_mtime)
            except OSError:
                files = []
            for f in files:
                if stop.is_set():
                    break
                payload = load_json(f)
                if not payload:
                    log(f"outbox 文件损坏，移到 sent 旁置：{f.name}")
                    os.replace(f, OUTBOX_SENT / (f.name + ".bad"))
                    continue
                to_user = payload.get("to_user_id") or session.get("ilink_user_id")
                text = payload.get("text") or ""
                ctx_token = payload.get("context_token")
                if not ctx_token:
                    ctx = load_json(CONTEXT_FILE, {}) or {}
                    ctx_token = ctx.get("context_token")
                if not text or not to_user:
                    log(f"outbox 文件缺 text/to_user_id，跳过：{f.name}")
                    continue
                try:
                    send_message(session, to_user, text, ctx_token)
                    os.replace(f, OUTBOX_SENT / f.name)
                    log(f"已发出一条回复：{f.name} -> outbox/sent/")
                except SessionExpired:
                    mark_needs_relogin("sendmessage 收到 -14")
                    stop.set()
                    return
                except Exception as e:  # noqa: BLE001
                    log(f"发出失败（留在 pending 下轮重试）：{f.name}：{e}")
                    stop.wait(3)
            stop.wait(2)

    t1 = threading.Thread(target=poll_loop, daemon=True, name="getupdates")
    t2 = threading.Thread(target=outbox_loop, daemon=True, name="outbox")
    t1.start()
    t2.start()
    while not stop.is_set():
        time.sleep(1)
    notify(session, start=False)
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass
    log("serve 已退出")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def _mask(s: str | None) -> str:
    if not s:
        return "-"
    if len(s) <= 8:
        return s[:2] + "***"
    return s[:4] + "***" + s[-4:]


def cmd_status(args) -> int:
    ensure_dirs()
    session = load_session()
    print("== 微信 iLink 桥接状态 ==")
    if session:
        print(f"登录：已登录（{session.get('saved_at', '-')} 保存）")
        print(f"  ilink_bot_id : {_mask(session.get('ilink_bot_id'))}")
        print(f"  ilink_user_id: {_mask(session.get('ilink_user_id'))}")
        print(f"  baseurl      : {session.get('baseurl')}")
        print("  bot_token    : 已保存（不显示）")
    else:
        print("登录：未登录（没有 state/session.json）")
    pending = load_json(PENDING_QR_FILE)
    if pending:
        age = int(time.time()) - int(pending.get("created_ts") or 0)
        print(f"待扫二维码：有（{age} 秒前生成，可能已过期）")
    print(f"需要重新登录：{'是！state/NEEDS_RELOGIN 存在' if NEEDS_RELOGIN_FILE.exists() else '否'}")
    ctx = load_json(CONTEXT_FILE, {}) or {}
    print(f"context_token 缓存：{'有' if ctx.get('context_token') else '无（等你在微信先发一条消息）'}")
    running = False
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            running = True
            print(f"serve 进程：运行中（pid={pid}）")
        except (ValueError, OSError):
            print("serve 进程：未运行（pid 文件残留）")
    else:
        print("serve 进程：未运行")
    inbox_n = len(list(INBOX_DIR.glob("*.json")))
    pend_n = len(list(OUTBOX_PENDING.glob("*.json")))
    sent_n = len(list(OUTBOX_SENT.glob("*.json")))
    print(f"消息：inbox {inbox_n} 条 / 待发 {pend_n} 条 / 已发 {sent_n} 条")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="微信 iLink / ClawBot 桥接")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="拿登录二维码")

    p_wait = sub.add_parser("wait-login", help="轮询扫码状态直到 confirmed")
    p_wait.add_argument("--qrcode", default=None, help="指定 qrcode（默认读 state/pending_qrcode.json）")
    p_wait.add_argument("--verify-code", default=None, help="手机微信上显示的验证码")
    p_wait.add_argument("--timeout", type=int, default=480, help="最长等待秒数（默认 480）")

    sub.add_parser("serve", help="常驻收发消息")

    p_send = sub.add_parser("send", help="发一条消息（用缓存的 context_token）")
    p_send.add_argument("--text", required=True, help="要发的文本")
    p_send.add_argument("--to", default=None, help="目标 user id（默认用缓存/本人）")

    sub.add_parser("status", help="查看登录与运行状态")

    args = parser.parse_args(argv)
    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "wait-login":
        return cmd_wait_login(args)
    if args.cmd == "serve":
        return serve_loop(args)
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "status":
        return cmd_status(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
