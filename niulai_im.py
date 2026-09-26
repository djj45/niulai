# -*- coding: utf-8 -*-
"""
腾讯云 IM 实时通道（约牛收消息用）
==================================
按 protocol.md §7 实现：

1. `wss://<sdkAppId>w4c.my-imcloud.com/binfo?sdkappid=…&instanceid=…&random=…&platform=8&host=mac&version=-1&sdkversion=4.4.3&compress=gzip`
2. 帧格式
   - **客户端 → 服务端：必须发二进制帧**（UTF-8 JSON 字节，不能发 text 帧，否则服务端静默丢弃）
   - **服务端 → 客户端：二进制帧**，前 4 字节为 `b"COMP"` 时是流式 gzip
     （Z_SYNC_FLUSH、无标准 trailer，必须用 `decompressobj(31)`，`gzip.decompress` 会 EOFError）
3. 握手：`heartbeat.alive`（status_instid=0）→ `im_open_status.wslogin`（identifier + usersig）
   → 拿到 A2Key / TinyId / InstId，之后所有帧头都要带上
4. `million_group_open_http_svc.apply_join_group` 进群
5. `heartbeat.alive` 心跳；`im_open_push.msg_push` 收推送；`openim.ws_msg_push_ack` 回执
6. 服务端推送的聊天消息在 `CloudCustomData` 里带约牛业务 JSON（与 REST 历史记录同构）

依赖：websockets>=12（asyncio）
"""
from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
import zlib
from typing import Any, Callable, Iterable

import websockets

# ===================== 常量 =====================
WEBSDK_APPID = 537048168
WEBSDK_VERSION = "1.7.3"
SDK_ABILITY = 478343027
PLATFORM_WX_MP = 8
PROTOCOL_VER = "v4"
HEARTBEAT_INTERVAL = 25           # 秒
# 实测：真实客户端心跳间隔中位数 26s（29,28,26,11,11,10,27,14…）。
# 服务端 wslogin 里下发的是 HelloInterval=120，但真按 120s 发会被网关当空闲连接
# 掉线（1006），所以这里以实测为准，只在下发值处于 10~30s 这种正常区间时才采用。
MIN_LOGIN_INTERVAL = 15
CONNECT_TIMEOUT = 15
MAX_FRAME = 8 * 1024 * 1024

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
      "MiniProgramEnv/Mac MacWechat/WMPF MacWechat/3.8.7(0x13080712) UnifiedPCMacWechat(0xf2641d3f) XWEB/25561")

CMD_HELLO = "im_open_status.wshello"
CMD_LOGIN = "im_open_status.wslogin"
CMD_LOGOUT = "im_open_status.wslogout"
CMD_HEARTBEAT = "heartbeat.alive"
CMD_PUSH = "im_open_push.msg_push"
CMD_PUSH_ACK = "openim.ws_msg_push_ack"
CMD_JOIN_GROUP = "million_group_open_http_svc.apply_join_group"
CMD_GROUP_LIST = "million_group_open_http_svc.get_joined_group_list"

# Event 类型
EVENT_C2C_MSG = 1
EVENT_GROUP_MSG = 3
EVENT_GROUP_TIPS = 4


# ===================== 帧编解码 =====================
def decode_frame(data: Any) -> str:
    """腾讯云 IM 服务端帧 → JSON 字符串。"""
    if isinstance(data, str):
        return data
    if data[:4] == b"COMP":
        d = zlib.decompressobj(31)
        out = d.decompress(data[4:]) + d.flush()
        return out.decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


def encode_frame(obj: dict) -> bytes:
    """必须编码成 bytes（二进制帧），text 帧会被服务端丢弃。"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ===================== 消息解析 =====================
def _msg_content_to_text(msg_body: list) -> str:
    """从 MsgBody 提取文本（TIMTextElem / TIMImageElem）。"""
    for el in msg_body or []:
        t = el.get("MsgType")
        c = el.get("MsgContent") or {}
        if t == "TIMTextElem":
            return c.get("Text", "")
        if t == "TIMImageElem":
            arr = c.get("ImageInfoArray") or []
            for info in arr:
                if info.get("URL"):
                    return json.dumps({"width": info.get("Width", 0),
                                       "height": info.get("Height", 0),
                                       "imgUrl": info.get("URL")}, separators=(",", ":"))
    return ""


def _parse_business_payload(group_msg: dict) -> dict:
    """优先用 CloudCustomData（约牛业务 JSON），否则退回 MsgBody。

    返回统一字段（与 REST 历史记录的 snake_case 对齐）。
    """
    raw = {}
    ccd = group_msg.get("CloudCustomData")
    if ccd:
        try:
            raw = json.loads(ccd) if isinstance(ccd, str) else dict(ccd)
        except (TypeError, ValueError):
            raw = {}

    gi = group_msg.get("GroupInfo") or {}
    if not raw:
        # 没有业务数据（例如其它端发的原生 IM 消息），用 IM 字段兜底
        raw = {
            "id": 0,
            "msgType": 1 if (group_msg.get("MsgBody") or [{}])[0].get("MsgType") == "TIMImageElem" else 0,
            "msgContent": _msg_content_to_text(group_msg.get("MsgBody")),
            "nickName": (gi.get("From_AccountNick") or "").split(" (")[0],
            "avatarUrl": gi.get("From_AccountHeadurl", ""),
            "userId": 0,
            "ynNo": "",
            "userType": 1,
            "msgDate": _fmt_ts(group_msg.get("MsgTimeStamp")),
            "imGroupId": gi.get("GroupId", ""),
            "imMsgSeq": group_msg.get("MsgSeq"),
        }
    return raw


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return ""


def normalize_group_message(group_msg: dict) -> dict:
    """把 IM 群消息推送归一化成前端/数据库友好结构。

    ⚠️ **只能信 CloudCustomData 的内容与身份字段，不能信它的标记字段**：
    IM 推送是「审核前」的版本，实测同一条消息：
        IM  CloudCustomData → privateMessageFlag=true  vipUser=true   auditStatus=0
        REST 历史记录       → privateMessageFlag=false vipUser=false  auditStatus=1
    所以这里把 private_message_flag / vip_user / audit_status 写成中立默认值，
    等下一次 REST 同步把真实值覆盖回来（420+ 条 REST 记录里这三个字段
    分别全是 0/0/1，说明 IM 那边就是占位值）。原始值仍存在 raw 里。
    真正可信的「老师回复了谁」信号是 user_type 为 3/4（老师）且 to_user_id>0。
    """
    raw = _parse_business_payload(group_msg)
    gi = group_msg.get("GroupInfo") or {}
    nick = raw.get("nickName") or (gi.get("From_AccountNick") or "").split(" (")[0] or "匿名"
    yn_no = raw.get("ynNo") or ""
    if not yn_no:
        m = gi.get("From_AccountNick") or ""
        if "(" in m and m.rstrip().endswith(")"):
            yn_no = m[m.rfind("(") + 1:-1]
    msg_date = raw.get("msgDate") or _fmt_ts(group_msg.get("MsgTimeStamp"))
    quote = raw.get("quoteContent") or None
    return {
        "id": raw.get("id") or 0,
        "room_id": raw.get("chatRoomId") or 0,
        "msg_type": raw.get("msgType", 0),
        "msg_content": raw.get("msgContent") or "",
        "msg_date": msg_date,
        "nick_name": nick,
        "yn_no": yn_no,
        "user_id": raw.get("userId") or 0,
        "user_type": raw.get("userType", 1),
        "avatar_url": raw.get("avatarUrl") or gi.get("From_AccountHeadurl", ""),
        "real_name": raw.get("realName"),
        "certificate_num": raw.get("certificateNum"),
        # ↓ 以下三个字段在 IM 推送里不可信，用中立值，等 REST 回填
        "private_message_flag": False,
        "vip_user": False,
        "audit_status": 1,
        "teacher_id": raw.get("teacherId"),
        "to_user_id": raw.get("toUserId"),
        "fee_status": raw.get("feeStatus", 0),
        "source_type": raw.get("sourceType", 3),
        "im_group_id": raw.get("imGroupId") or gi.get("GroupId", ""),
        "im_msg_seq": raw.get("imMsgSeq") or group_msg.get("MsgSeq"),
        "quote_id": (quote or {}).get("id"),
        "quote_json": json.dumps(quote, ensure_ascii=False) if quote else "",
        "raw": json.dumps(raw, ensure_ascii=False),
        "from_im": True,
    }


def parse_push(body: dict) -> tuple[list[dict], list[dict]]:
    """解析一次 msg_push。

    返回 (messages, events)：
      messages — 归一化后的聊天消息列表
      events   — 系统/群提示事件（进出场、在线人数等），形如
                 {'kind': 'group_tips'|'system', 'member_num': int, 'text': str}
    """
    messages, events = [], []
    for ea in body.get("EventArray") or []:
        ev = ea.get("Event")
        if ev == EVENT_GROUP_MSG:
            for gm in ea.get("GroupMsgArray") or []:
                messages.append(normalize_group_message(gm))
        elif ev == EVENT_C2C_MSG:
            for cm in ea.get("C2CMsgArray") or []:
                messages.append(normalize_group_message(cm))
        elif ev == EVENT_GROUP_TIPS:
            for tip in ea.get("GroupTips") or []:
                ginfo = tip.get("GroupInfo") or {}
                mb = tip.get("MsgBody") or {}
                op = mb.get("OpType")
                text = {1: "加入群聊", 2: "退出群聊", 3: "被移出群聊", 4: "被禁言"}.get(op, f"群提示({op})")
                events.append({
                    "kind": "group_tips",
                    "text": f"{(ginfo.get('From_AccountNick') or '').split(' (')[0]} {text}",
                    "member_num": mb.get("MemberNum"),
                    "group_id": ginfo.get("GroupId", ""),
                    "msg_seq": tip.get("MsgSeq"),
                })
        else:
            events.append({"kind": "system", "event": ev, "text": f"Event={ev}"})
    return messages, events


# ===================== 客户端 =====================
class TencentImClient:
    """腾讯云 IM WebSocket 客户端（只做收消息 + 进群 + 心跳 + 回执）。"""

    def __init__(self, sdk_app_id, identifier: str, user_sig: str,
                 group_ids: Iterable[str] = (), on_message: Callable | None = None,
                 on_event: Callable | None = None, on_status: Callable | None = None,
                 logger: Callable | None = None, host_suffix: str = "w4c"):
        self.sdk_app_id = int(sdk_app_id)
        self.identifier = identifier
        self.user_sig = user_sig
        self.group_ids = [g for g in (group_ids or []) if g]
        self.on_message = on_message
        self.on_event = on_event
        self.on_status = on_status
        self.log = logger or (lambda *a, **k: None)
        self.host_suffix = host_suffix

        self._seq = random.randint(1_000_000, 9_999_999)
        self._a2 = ""
        self._tinyid = ""
        self._instid = 0
        self._hello_interval = HEARTBEAT_INTERVAL
        self._ws = None
        self._running = False
        self._pending: dict[int, asyncio.Future] = {}
        self._login_sent = False
        self._login_at = 0.0
        self._session_started_at = 0.0
        self.logged_in = False
        self.last_error = ""

    # ---------- 帧构造 ----------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _head(self, servcmd: str, extra: dict | None = None) -> dict:
        h = {
            "servcmd": servcmd, "ver": PROTOCOL_VER, "platform": PLATFORM_WX_MP,
            "websdkappid": WEBSDK_APPID, "websdkversion": WEBSDK_VERSION,
            "status_instid": self._instid, "sdkappid": self.sdk_app_id,
            "contenttype": "json", "reqtime": int(time.time()),
            "sdkability": SDK_ABILITY, "sdkability_ext": "",
            "cappid": 0, "tjgID": "", "seq": self._next_seq(), "cs": 0,
        }
        if self._a2:
            h["a2"] = self._a2
        if self._tinyid:
            h["tinyid"] = self._tinyid
        if extra:
            h.update(extra)
        return h

    async def _send(self, servcmd: str, body: dict | None = None, extra: dict | None = None):
        if not self._ws:
            raise RuntimeError("IM 未连接")
        await self._ws.send(encode_frame({"head": self._head(servcmd, extra), "body": body or {}}))

    def _url(self) -> str:
        return (f"wss://{self.sdk_app_id}{self.host_suffix}.my-imcloud.com/binfo"
                f"?sdkappid={self.sdk_app_id}&instanceid={secrets.token_hex(16)}"
                f"&random={random.random()}&platform={PLATFORM_WX_MP}&host=mac"
                f"&version=-1&sdkversion=4.4.3&compress=gzip")

    # ---------- 状态回调 ----------
    def _status(self, state: str, detail: str = ""):
        self.log(f"[IM] {state} {detail}")
        if self.on_status:
            try:
                self.on_status(state, detail)
            except Exception:
                pass

    # ---------- 单次连接生命周期 ----------
    async def _run_once(self):
        url = self._url()
        self._status("connecting", url.split("?")[0])
        async with websockets.connect(
            url, origin=f"https://{self.sdk_app_id}{self.host_suffix}.my-imcloud.com",
            additional_headers={"User-Agent": UA, "content-type": "application/json"},
            max_size=MAX_FRAME, open_timeout=CONNECT_TIMEOUT, ping_interval=None,
            proxy=None,      # 直连：不吃环境代理，sing-box 关了也不影响收消息
        ) as ws:
            self._ws = ws
            self.logged_in = False
            self._login_sent = False
            self.last_error = ""
            self._session_started_at = time.time()
            await self._send(CMD_HEARTBEAT)          # 先握手（服务端回 ack 后才能真正登录）
            last_hb = time.time()
            while self._running:
                timeout = max(1.0, self._hello_interval - (time.time() - last_hb))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._send(CMD_HEARTBEAT)
                    last_hb = time.time()
                    continue
                except websockets.ConnectionClosed as e:
                    self._status("closed", f"code={e.code} {e.reason}")
                    break
                await self._handle_frame(raw)
                if self.logged_in and self._a2:
                    if time.time() - last_hb >= self._hello_interval:
                        await self._send(CMD_HEARTBEAT)
                        last_hb = time.time()

    async def _handle_frame(self, raw):
        try:
            text = decode_frame(raw)
            obj = json.loads(text)
        except Exception as e:
            self.log(f"[IM] 帧解码失败 {e} raw={str(raw)[:120]}")
            return
        head = obj.get("head") or {}
        body = obj.get("body") or {}
        cmd = head.get("servcmd")

        if cmd == CMD_HEARTBEAT:
            # 服务端对首次心跳的 ack 代表网关就绪 → 此时发起 wslogin
            if not self._login_sent:
                self._login_sent = True
                await self._login()
        elif cmd == CMD_LOGIN:
            if body.get("A2Key"):
                self._a2 = body["A2Key"]
                self._tinyid = str(body.get("TinyId", ""))
                self._instid = body.get("InstId", 0)
                self._hello_interval = self._pick_interval(body.get("HelloInterval"))
                self.logged_in = True
                self.last_error = ""
                self._status("online", f"tinyid={self._tinyid}")
                await self._after_login()
            else:
                self.last_error = f"{body.get('ErrorCode')} {body.get('ErrorInfo')}"
                self._status("login_failed", self.last_error)
        elif cmd == CMD_PUSH:
            await self._handle_push(body)
        elif cmd in (CMD_JOIN_GROUP, CMD_GROUP_LIST):
            ec = body.get("ErrorCode")
            if ec not in (0, None):
                self.log(f"[IM] {cmd} → {ec} {body.get('ErrorInfo')}")
        else:
            fut = self._pending.pop(head.get("seq"), None)
            if fut and not fut.done():
                fut.set_result(body)

    @staticmethod
    def _pick_interval(server_value) -> int:
        """服务端下发的 HelloInterval 只在 10~30s 这种正常区间才采用，其余用实测值。"""
        try:
            v = int(server_value)
        except (TypeError, ValueError):
            v = 0
        return v if 10 <= v <= 30 else HEARTBEAT_INTERVAL

    async def _login(self):
        """发起 wslogin。遵守腾讯云的登录频率限制（≥15s），否则会被判参数错/直接断连。"""
        gap = MIN_LOGIN_INTERVAL - (time.time() - self._login_at)
        if gap > 0:
            self.log(f"[IM] 距上次登录仅 {MIN_LOGIN_INTERVAL - gap:.1f}s（限制 {MIN_LOGIN_INTERVAL}s），"
                     f"等 {gap:.1f}s 再登")
            await asyncio.sleep(gap)
        self._login_at = time.time()
        await self._send(CMD_LOGIN, {"State": "Online", "is_web_uniapp": 0,
                                     "InstType": 0, "CustomInfo": ""},
                         {"identifier": self.identifier, "usersig": self.user_sig})

    async def _after_login(self):
        """登录成功后进群 + 拉一次群列表。"""
        for gid in self.group_ids:
            try:
                await self._send(CMD_JOIN_GROUP, {"GroupId": gid, "HugeGroupHistoryMsgFlag": 1})
                self.log(f"[IM] 已申请进群 {gid}")
            except Exception as e:
                self.log(f"[IM] 进群失败 {e}")

    async def _handle_push(self, body: dict):
        messages, events = parse_push(body)
        for e in events:
            if self.on_event:
                try:
                    self.on_event(e)
                except Exception as ex:
                    self.log(f"[IM] on_event 异常 {ex}")
        for m in messages:
            if self.on_message:
                try:
                    self.on_message(m)
                except Exception as ex:
                    self.log(f"[IM] on_message 异常 {ex}")
        # 回执（服务端要求回传 SessionData，否则会重复推）
        sd = body.get("SessionData")
        if sd and body.get("NeedAck"):
            try:
                await self._send(CMD_PUSH_ACK, {"SessionData": sd})
            except Exception as e:
                self.log(f"[IM] push ack 失败 {e}")

    # ---------- 对外 ----------
    async def run_forever(self):
        """断线自动重连，指数退避。

        退避策略（避免撞腾讯云的保护）：
          * 会话活得够久（>60s）→ 视为正常抖动，回到 2s 快重连
          * 一连上就断 / 登录失败   → 从 30s 起指数升级，最长 5 分钟
        """
        self._running = True
        backoff = 2
        while self._running:
            started = time.time()
            failed_login = False
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self._status("error", self.last_error)
            finally:
                failed_login = bool(self.last_error) and not self.logged_in
                self._ws = None
                self.logged_in = False
                self._login_sent = False
                self._a2 = ""
            if not self._running:
                break
            if time.time() - started > 60:
                backoff = 2
            elif failed_login:
                backoff = max(30, backoff * 2)
            else:
                backoff = max(5, backoff * 2)
            backoff = min(backoff, 300)
            if failed_login:
                self.log(f"[IM] 登录未成功（{self.last_error}），{backoff}s 后重试")
            await asyncio.sleep(backoff)

    async def send_text_to_group(self, group_id: str, text: str):
        """（可选）直接通过 IM 发文本消息 —— 约牛正常走 REST，这里仅作调试。"""
        await self._send("group_open_http_svc.send_group_msg", {
            "GroupId": group_id,
            "MsgBody": [{"MsgType": "TIMTextElem", "MsgContent": {"Text": text}}],
        })

    async def stop(self):
        self._running = False
        if self._ws:
            try:
                await self._send(CMD_LOGOUT, {"wslogout_type": 1, "isWebUniapp": 0})
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
