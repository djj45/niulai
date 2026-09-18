# -*- coding: utf-8 -*-
"""
约牛（niulai / zx093）协议客户端
================================
按 protocol.md 实现三条链路：

1. **sign 算法**：盐 `asdasdsadfg`，`k=v` 字典序拼接后 `&key=盐`，MD5 大写
2. **SSO 登录链**（account.zx093.cn/ssoserver）
   getAuthToken → bind/getUnionidAndStatus → estr → bindPhone → authTokenExchangeToken
   → centraltoken（业务会话令牌）
3. **业务 REST**（touguapi.zx093.com/touguServer/app）
   房间信息 / 历史消息游标分页 / 发消息 / 用户资料 / OSS 上传凭证 + 图片直传

腾讯云 IM 实时通道见 niulai_im.py。

注意：`getUnionidAndStatus` / `bindPhone` 需要微信 `wx.login` 的一次性 `loginCode`，
只能在微信小程序内取得；因此本地工具的主用鉴权方式是直接提供 `centraltoken`
（`TouguClient(central_token=...)`），登录链作为可选能力保留。
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from datetime import datetime

import requests

# ===================== 常量 =====================
SALT = "asdasdsadfg"                       # 小程序源码内的固定盐
SSO_BASE = "https://account.zx093.cn/ssoserver"
API_BASE = "https://touguapi.zx093.com/touguServer/app"
STAT_BASE = "https://stat.zx093.cn/smartlog"
OSS_UPLOAD_HOST = "https://filecdn-prod.oss-cn-beijing.aliyuncs.com"
OSS_READ_BASE = "https://fileoss.zx093.com"     # 图片/头像 CDN（无鉴权，拿到 URL 即可读）
MP_APPID = "wx0c0819078db5e3e1"
DEVICE_ID = "110110"                            # 桌面端固定设备号
LOGIN_SOURCE = 7

REFERER = f"https://servicewechat.com/{MP_APPID}/13/page-frame.html"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
      "MiniProgramEnv/Mac MacWechat/WMPF MacWechat/3.8.7(0x13080712) UnifiedPCMacWechat(0xf2641d3f) XWEB/25561")

DEFAULT_TIMEOUT = 20

# 消息类型
MSG_TEXT = 0
MSG_IMAGE = 1
# 历史消息来源流
SRC_ROOM = 1        # 房间消息流
SRC_TEACHER_DM = 4  # 老师私聊回复流
# 用户类型
USER_TYPE_TEACHER = 4


# ===================== 异常 =====================
class NiuLaiError(Exception):
    """协议/业务异常基类。"""


class ApiError(NiuLaiError):
    def __init__(self, status, message, path=""):
        self.status = status
        self.message = message
        self.path = path
        super().__init__(f"[{status}] {message} ({path})")


class AuthExpired(NiuLaiError):
    """登录态失效（status=200001），需要重新获取 centraltoken。"""

    def __init__(self, path=""):
        super().__init__(f"登录失效，请更新 centraltoken（{path}）")
        self.path = path


class LoginError(NiuLaiError):
    pass


# ===================== sign 算法 =====================
def get_sign(params: dict) -> str:
    """按 protocol.md §2 计算 sign。

    规则：丢弃 channel 字段 → `k=v` 按字典序排序 → `&` 连接 → 追加 `&key=盐`
          → MD5 → 大写十六进制。
    """
    p = {k: v for k, v in params.items() if k != "channel" and v is not None}
    s = "&".join(f"{k}={p[k]}" for k in sorted(p))
    s += f"&key={SALT}"
    return hashlib.md5(s.encode()).hexdigest().upper()


def now_ms() -> str:
    return str(int(time.time() * 1000))


# ===================== 时间/媒体工具 =====================
def parse_msg_time(msg_date: str):
    """'2026-09-17 21:27:39' → unix 秒；解析失败返回 0。"""
    if not msg_date:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(msg_date.strip(), fmt).timestamp())
        except ValueError:
            continue
    return 0


def media_url(path: str) -> str:
    """把 `imgUrl` / `avatarUrl`（形如 /tougu/imgchat/xxx.png）拼成可直读的 CDN 地址。"""
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return OSS_READ_BASE + ("" if path.startswith("/") else "/") + path


def image_size(data: bytes):
    """返回 (width, height)，失败返回 (0, 0)。"""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


# ===================== SSO 登录链 =====================
class SsoClient:
    """小程序 SSO 登录链（需要微信 wx.login 提供的一次性 loginCode）。"""

    def __init__(self, session: requests.Session | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.s = session or requests.Session()
        self.timeout = timeout

    def _post(self, path: str, data: dict) -> dict:
        payload = dict(data)
        payload["sign"] = get_sign({k: v for k, v in payload.items() if k != "sign"})
        r = self.s.post(f"{SSO_BASE}{path}", data=payload,
                        headers={"content-type": "application/x-www-form-urlencoded",
                                 "user-agent": UA, "referer": REFERER},
                        timeout=self.timeout)
        r.raise_for_status()
        try:
            out = r.json()
        except ValueError:
            raise LoginError(f"SSO 返回非 JSON：{r.text[:200]}")
        return out

    def _get(self, path: str) -> dict:
        r = self.s.get(f"{SSO_BASE}{path}",
                       headers={"user-agent": UA, "referer": REFERER}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_auth_token(self, device_id: str = DEVICE_ID) -> str:
        """① 取长效中间票据 authToken（不需要 loginCode）。"""
        out = self._post("/login/xcx/getAuthToken.htm",
                         {"timestamp": now_ms(), "deviceId": device_id})
        if out.get("status") != 1:
            raise LoginError(f"getAuthToken 失败：{out.get('message')}")
        return out["data"]

    def get_unionid_and_status(self, auth_token: str, login_code: str,
                               event_key_id: str = "", event_page: str = "chat",
                               event_page_url: str = "") -> dict:
        """② 用 wx.login 的一次性 loginCode 绑定微信身份。"""
        event_key_id = event_key_id or uuid.uuid4().hex
        event_page_url = event_page_url or "/pages/chat/index/index"
        return self._post("/login/xcx/bind/getUnionidAndStatus.htm", {
            "timestamp": now_ms(), "loginCode": login_code, "loginSource": LOGIN_SOURCE,
            "authToken": auth_token, "loginFunction": LOGIN_SOURCE, "os": 0,
            "eventKeyId": event_key_id, "eventPage": event_page,
            "eventPageUrl": event_page_url, "eventExtra": json.dumps(
                {"eventPage": event_page, "pageName": "聊天室", "pageUrl": event_page_url,
                 "referPage": "", "eventKeyId": event_key_id}, ensure_ascii=False),
        })

    def get_estr(self, auth_token: str) -> str:
        """③ estr（登录附加串）。"""
        out = self._post("/login/xcx/estr.htm", {"authToken": auth_token, "timestamp": now_ms()})
        return (out.get("data") or {}).get("estr", "")

    def bind_phone(self, auth_token: str, login_code: str, estr: str = "") -> dict:
        """④ 手机号绑定状态（新用户会走绑定流程）。"""
        return self._post("/login/xcx/bindPhone.htm", {
            "timestamp": now_ms(), "loginCode": login_code, "authToken": auth_token, "os": 0,
            "loginSource": LOGIN_SOURCE, "loginVersion": "1.0.10",
            "loginFunction": LOGIN_SOURCE, "invitationChannel": "", "invitationCode": "0000",
            "estr": estr,
        })

    def exchange_token(self, auth_token: str) -> str:
        """⑤ authToken → centraltoken（业务会话令牌）。"""
        out = self._post("/login/xcx/authTokenExchangeToken.htm",
                         {"timestamp": now_ms(), "authToken": auth_token})
        if out.get("status") != 1:
            raise LoginError(f"authTokenExchangeToken 失败：{out.get('message')}")
        return out["data"]

    def login_with_code(self, login_code: str) -> str:
        """完整登录链：loginCode → centraltoken。"""
        auth = self.get_auth_token()
        self.get_unionid_and_status(auth, login_code)
        estr = self.get_estr(auth)
        self.bind_phone(auth, login_code, estr)
        return self.exchange_token(auth)


# ===================== 业务 REST 客户端 =====================
class TouguClient:
    """touguapi 业务接口客户端。

    `central_token` 为空时只读接口（图片 CDN）仍可用，业务接口会抛 AuthExpired。
    """

    def __init__(self, central_token: str = "", timeout: int = DEFAULT_TIMEOUT,
                 on_token_expired=None):
        self.central_token = central_token or ""
        self.timeout = timeout
        self.session = requests.Session()
        self.on_token_expired = on_token_expired

    # ---------- 基础请求 ----------
    def _headers(self) -> dict:
        h = {"accesssource": "6", "content-type": "application/json",
             "user-agent": UA, "referer": REFERER}
        if self.central_token:
            h["centraltoken"] = self.central_token
        return h

    def _request(self, method: str, path: str, body=None, params=None) -> dict:
        url = f"{API_BASE}{path}"
        r = self.session.request(method, url, headers=self._headers(),
                                 json=body if method != "GET" else None, params=params,
                                 timeout=self.timeout)
        r.raise_for_status()
        try:
            out = r.json()
        except ValueError:
            raise ApiError(-1, f"返回非 JSON：{r.text[:200]}", path)
        status = out.get("status")
        if status == 200001:
            if self.on_token_expired:
                try:
                    self.on_token_expired()
                except Exception:
                    pass
            raise AuthExpired(path)
        if status != 1:
            raise ApiError(status, out.get("message", ""), path)
        return out.get("data")

    def _get(self, path: str, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path: str, body=None):
        return self._request("POST", path, body=body or {})

    # ---------- 房间 / 老师 ----------
    def get_room_by_teacher(self, teacher_id: int) -> dict:
        """房间信息：id(=chatRoomId)、imGroupId、name、teacherId、enableDm、visibleDays…"""
        return self._post("/chatroom/getByTeacherId.htm", {"teacherId": teacher_id})

    def is_has_permission(self, teacher_id: int) -> dict:
        """订单/权限：orderStatus=2 表示无有效订单。"""
        return self._post("/order/isHasPermission.htm", {"teacherId": teacher_id})

    def get_sdk_app_id(self) -> str:
        return (self._get("/chatroom/getSdkAppId.htm") or {}).get("sdkAppId", "")

    def get_user_sig(self) -> dict:
        """腾讯 IM 凭证：{'sdkUserId': 'cu_xxx', 'userSig': '...'}"""
        return self._get("/chatroom/getUserSig.htm") or {}

    def get_user_info(self) -> dict:
        """当前登录用户资料（走 SSO 域，需 centraltoken）。"""
        r = self.session.get(f"{SSO_BASE}/user/getInfo.htm", headers=self._headers(),
                            timeout=self.timeout)
        r.raise_for_status()
        out = r.json()
        if out.get("status") == 200001:
            raise AuthExpired("/ssoserver/user/getInfo.htm")
        return out.get("data") or {}

    # ---------- 历史消息 ----------
    def get_chat_records(self, room_id: int, cursor_id="", direction: int = 1,
                         page_size: int = 15, source_type: int = SRC_ROOM) -> list:
        """游标分页拉历史消息。

        cursor_id 为空 → 最新一页；否则传当前页最旧一条的 id，direction=1 向前翻。
        source_type: 1=房间消息流, 4=老师私聊回复流。
        """
        data = self._post("/chatrecord/getChatRecordList.htm", {
            "chatRoomId": room_id,
            "cursorId": "" if cursor_id is None else str(cursor_id),
            "userType": "", "direction": direction,
            "pageSize": page_size, "sourceType": source_type,
        })
        return data or []

    def iter_records(self, room_id: int, source_type: int = SRC_ROOM, page_size: int = 15,
                     max_pages: int = 0, start_cursor: str = "", on_page=None):
        """从最新往历史翻页迭代。max_pages=0 表示不限制。"""
        cursor = start_cursor
        page = 0
        while True:
            items = self.get_chat_records(room_id, cursor, 1, page_size, source_type)
            if not items:
                return
            page += 1
            if on_page:
                on_page(items)
            yield items
            if max_pages and page >= max_pages:
                return
            cursor = items[-1].get("id")   # 服务端按时间正序返回，最后一条最旧
            if not cursor:
                return
            time.sleep(0.35)               # 轻量退避，避免触发风控

    # ---------- 发消息 ----------
    def send_text(self, room_id: int, text: str) -> dict:
        return self._post("/chatrecord/sendMessage.htm",
                          {"chatRoomId": room_id, "msgType": MSG_TEXT, "msgContent": text})

    def send_image(self, room_id: int, img_url: str, width: int, height: int) -> dict:
        content = json.dumps({"width": int(width), "height": int(height), "imgUrl": img_url},
                             separators=(",", ":"))
        return self._post("/chatrecord/sendMessage.htm",
                          {"chatRoomId": room_id, "msgType": MSG_IMAGE, "msgContent": content})

    # ---------- 图片上传（OSS 直传） ----------
    def get_oss_policy(self, directory: str = "imgchat") -> dict:
        """上传凭证：accessId / dir / expire / host / policy / signature。"""
        return self._get("/oss/common/getPolicy.htm", params={"directory": directory})

    def upload_image(self, data: bytes, ext: str = "png", directory: str = "imgchat") -> str:
        """上传图片到 OSS，返回 `imgUrl`（形如 /tougu/imgchat/<date>/<uuid>.png）。

        客户端自行生成 UUID 对象名（隐私：本地文件名不会成为 URL 一部分）。
        """
        pol = self.get_oss_policy(directory)
        day = datetime.now().strftime("%Y%m%d")
        name = f"{uuid.uuid4().hex}.{ext.lstrip('.').lower()}"
        key = f"{pol['dir']}{day}/{name}"
        files = {
            "name": (None, name),
            "policy": (None, pol["policy"]),
            "OSSAccessKeyId": (None, pol["accessId"]),
            "success_action_status": (None, "200"),
            "signature": (None, pol["signature"]),
            "key": (None, key),
            "file": (name, data, f"image/{ext.lstrip('.').lower()}"),
        }
        r = self.session.post(pol["host"], files=files, timeout=max(self.timeout, 60))
        if r.status_code not in (200, 201, 204):
            raise ApiError(r.status_code, f"OSS 上传失败：{r.text[:200]}", "oss")
        return "/" + key

    def upload_and_send_image(self, room_id: int, data: bytes, ext: str = "png") -> dict:
        """上传 + 发送图片消息，一步到位。"""
        w, h = image_size(data)
        img_url = self.upload_image(data, ext)
        return {"imgUrl": img_url, "width": w, "height": h,
                "result": self.send_image(room_id, img_url, w, h)}


# ===================== 便捷入口 =====================
def probe_room(central_token: str, teacher_id: int = 328) -> dict:
    """连通性自检：用给定的 token 拉一次房间信息。"""
    c = TouguClient(central_token)
    return c.get_room_by_teacher(teacher_id)


def download_media(url: str, timeout: int = 30) -> bytes:
    """下载图片/头像（CDN 无鉴权）。"""
    r = requests.get(url, headers={"user-agent": UA, "referer": REFERER}, timeout=timeout)
    r.raise_for_status()
    return r.content
