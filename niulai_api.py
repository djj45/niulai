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
SALT = "asdasdsadfg"                       # 固定盐（小程序/网页版同源）
SSO_BASE = "https://account.zx093.cn/ssoserver"
API_ROOT = "https://touguapi.zx093.com/touguServer"
API_BASE = f"{API_ROOT}/app"                      # 仍留在 /app 前缀下的老接口
COMMUNITY_BASE = f"{API_ROOT}/client/community"   # 网页版（2026-09 起）聊天室接口前缀
STAT_BASE = "https://stat.zx093.cn/smartlog"
OSS_UPLOAD_HOST = "https://filecdn-prod.oss-cn-beijing.aliyuncs.com"
OSS_READ_BASE = "https://fileoss.zx093.com"     # 图片/头像 CDN（无鉴权，拿到 URL 即可读）
MP_APPID = "wx0c0819078db5e3e1"
DEVICE_ID = "110110"                            # 桌面端固定设备号
LOGIN_SOURCE = 7

# 2026-09-24 起小程序接口下线，客户端全面迁到网页版（tougu.zx093.cn/webChatRoom）。
# UA/Referer 对齐网页版真实流量（HAR 实测可用），accesssource 头网页版不发、回放验证不需要。
REFERER = "https://tougu.zx093.cn/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

DEFAULT_TIMEOUT = 20

# 消息类型
MSG_TEXT = 0
MSG_IMAGE = 1
# 历史消息来源流
SRC_ROOM = 1        # 房间消息流
SRC_TEACHER_DM = 4  # 老师私聊回复流
# 用户类型（2026-09-24 起网页版把老师从 4 改成 3，判定一律 3/4 兼容）
USER_TYPE_TEACHER = (3, 4)


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


# ===================== 账号密码登录（DES 加密账密） =====================
# 源码 `app-service.js` 里的原实现（注意密钥是**硬编码的默认参数**，不是服务端密钥）：
#
#   function g(e, t = "T137SRpGil0=") {
#     var r = CryptoJS.enc.Base64.parse(t),
#         n = CryptoJS.DES.encrypt(e, r, {mode: ECB, padding: Pkcs7});
#     return n.ciphertext.toString(CryptoJS.enc.Base64)
#             .replace(/\//g, ",").replace(/=/g, "_").replace(/\+/g, ".");
#   }
#
# 所以本地完全可复现。验证方式：与 crypto-js 的输出逐字节对比（已做，6/6 一致）。
DES_KEY_B64 = "T137SRpGil0="                  # base64 解码＝ 8 字节 4f5dfb491a468a5d
PWD_LOGIN_URL = "https://account.zx093.cn/stoneserver/v1/account/accountPwdVerifyLogin.htm"
CAPTCHA_SCENE_ID = "17n9bhbp"                 # 网页版（webChatRoom）阿里云验证码 sceneId；小程序时代是 f374igpl
_TO_JS = (("/", ","), ("=", "_"), ("+", "."))
_FROM_JS = ((".", "+"), (",", "/"), ("_", "="))


def _des():
    from Crypto.Cipher import DES
    return DES


def encrypt_by_des(text: str) -> str:
    """复刻小程序 `encryptByDES`：DES/ECB/PKCS7，输出 Base64 后再把 `/` `=` `+` 换成 `,` `_` `.`。"""
    import base64
    from Crypto.Util.Padding import pad
    raw = _des().new(base64.b64decode(DES_KEY_B64), _des().MODE_ECB).encrypt(
        pad(str(text).encode(), 8))
    out = base64.b64encode(raw).decode()
    for a, b in _TO_JS:
        out = out.replace(a, b)
    return out


def decrypt_by_des(cipher: str) -> str:
    """反向（用于验证抓包样本）：`,` `_` `.` 换回 `/` `=` `+` 再解密。"""
    import base64
    from Crypto.Util.Padding import unpad
    s = str(cipher)
    for a, b in _FROM_JS:
        s = s.replace(a, b)
    return unpad(_des().new(base64.b64decode(DES_KEY_B64), _des().MODE_ECB).decrypt(
        base64.b64decode(s)), 8).decode()


def password_login_params(account: str, password: str, captcha_verify_param: str,
                          scene_id: str = CAPTCHA_SCENE_ID) -> dict:
    """拼出 `accountPwdVerifyLogin` 的请求体（含 sign）。

    对齐网页版 navtarLogin 的字段（HAR 抓包逐字段核对）：不带 deviceId，
    `loginVersion` 是**空串且要参与签名**（getSign 只删了 channel，不过滤空值）。
    """
    body = {
        "accountName": encrypt_by_des(account),
        "pwd": encrypt_by_des(password),
        "loginVersion": "",
        "loginSource": LOGIN_SOURCE,
        "captchaVerifyParam": captcha_verify_param,
        "sceneId": scene_id,
        "timestamp": now_ms(),
    }
    body["sign"] = get_sign(body)          # 必须在不含 sign 时算
    return body


def login_by_password(account: str, password: str, captcha_verify_param: str,
                      scene_id: str = CAPTCHA_SCENE_ID) -> str:
    """账号密码登录 → 直接返回 **centraltoken**。

    源码 `accountPwdVerifyLogin().then(e => wx.setStorageSync("token", e.data))`
    而请求层会把 storage 里的 `token` 当作 `centralToken` 请求头带上 ——
    所以返回值就是业务的 centraltoken，不需要再走 §2 那五步 SSO 链。

    唯一前置：`captchaVerifyParam`（阿里云验证码通过后的票据，一次性、短时效）。
    """
    if not (account and password):
        raise NiuLaiError("账号和密码都要填")
    if not captcha_verify_param:
        raise NiuLaiError("缺少 captchaVerifyParam（先过滑块，或直接粘贴票据）")
    body = password_login_params(account, password, captcha_verify_param, scene_id)
    r = requests.post(PWD_LOGIN_URL, data=body,
                      headers={"user-agent": UA, "referer": REFERER}, timeout=DEFAULT_TIMEOUT,
                      proxies={"http": None, "https": None})   # 国内服务直连，不吃环境代理
    try:
        j = r.json()
    except ValueError:
        raise NiuLaiError(f"登录返回不是 JSON（HTTP {r.status_code}）：{r.text[:200]}")
    status = str(j.get("status"))
    if status not in ("0", "1", "100"):
        msg = j.get("message") or j.get("msg") or str(j)[:200]
        raise NiuLaiError(f"登录失败 [{status}] {msg}")
    token = j.get("data")
    if not token or not isinstance(token, str):
        raise NiuLaiError(f"登录成功但没拿到 token：{str(j)[:200]}")
    return token


# ===================== 微信扫码登录（2026-09-24 网页版链路，全部离线验证） =====================
# 流程：getAuthToken(deviceId=12332) → qrcode.htm(带邀请码, multipart) → 二维码 jpg
#       → 每 2.5s 轮询 authTokenExchangeToken?c=md5(authToken)&ts=live3 → data=centraltoken
QR_DEVICE_ID = "12332"      # 网页版 getAuthToken 的固定 deviceId（HAR 实测）


def _sso_post(path: str, data: dict, params: dict | None = None, timeout: int = DEFAULT_TIMEOUT):
    r = requests.post(f"{SSO_BASE}{path}", data=data, params=params,
                      headers={"user-agent": UA, "referer": REFERER,
                               "content-type": "application/x-www-form-urlencoded"},
                      timeout=timeout, proxies={"http": None, "https": None})
    r.raise_for_status()
    return r.json()


def get_auth_token(device_id: str = QR_DEVICE_ID) -> str:
    """扫码登录第一步：authToken 是二维码与轮询的会话句柄。"""
    body = {"deviceId": device_id, "timestamp": now_ms()}
    body["sign"] = get_sign(body)
    j = _sso_post("/login/xcx/getAuthToken.htm", body)
    if str(j.get("status")) not in ("0", "1", "100") or not j.get("data"):
        raise NiuLaiError(f"getAuthToken 失败 [{j.get('status')}] {j.get('message')}")
    return j["data"]


def create_login_qrcode(auth_token: str, invite_code: str) -> bytes:
    """生成扫码登录二维码，返回 jpg 字节（约 100KB，前端直接 <img src=blob>）。"""
    body = {"authToken": auth_token, "invitationCode": invite_code,
            "invitationChannel": "h5_live", "os": "0", "loginVersion": "11",
            "loginSource": LOGIN_SOURCE, "loginFunction": "1", "timestamp": now_ms()}
    body["sign"] = get_sign(body)
    files = {k: (None, str(v)) for k, v in body.items()}
    r = requests.post(f"{SSO_BASE}/login/xcx/qrcode.htm", files=files,
                      headers={"user-agent": UA, "referer": REFERER},
                      timeout=DEFAULT_TIMEOUT, proxies={"http": None, "https": None})
    if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):
        raise NiuLaiError(f"二维码生成失败（HTTP {r.status_code}，{len(r.content)}B 非 jpg）")
    return r.content


def exchange_qrcode_token(auth_token: str) -> str:
    """轮询扫码状态：手机上确认后返回 centraltoken，未确认返回空串。

    c = md5(authToken)（网页版 timerFunc 实测，sign/参数均已离线逐字符对拍）。
    """
    c = hashlib.md5(auth_token.encode()).hexdigest()
    body = {"authToken": auth_token, "timestamp": now_ms(), "c": c, "ts": "live3"}
    body["sign"] = get_sign(body)
    j = _sso_post("/login/xcx/authTokenExchangeToken.htm", body, params={"c": c, "ts": "live3"})
    status = str(j.get("status"))
    if status == "200001":
        raise AuthExpired("/ssoserver/login/xcx/authTokenExchangeToken.htm（二维码过期）")
    if status == "101005":
        return ""                    # 「小程序没有完成登陆操作」= 还没扫码确认，继续等
    if status not in ("0", "1", "100"):
        raise NiuLaiError(f"轮询失败 [{status}] {j.get('message')}")
    return j.get("data") or ""


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
        # 约牛是国内服务，别跟着 shell 里的 http(s)_proxy 走——
        # 抓包用的 sing-box(127.0.0.1:20122) 一关，同步/IM 全线 ConnectionRefused
        self.session.trust_env = False
        self.on_token_expired = on_token_expired

    # ---------- 基础请求 ----------
    def _headers(self) -> dict:
        h = {"content-type": "application/json",
             "user-agent": UA, "referer": REFERER, "origin": REFERER.rstrip("/")}
        if self.central_token:
            h["centraltoken"] = self.central_token
        return h

    def _request(self, method: str, path: str, body=None, params=None) -> dict:
        # /client/... 是网页版前缀，其余沿用 /app 老前缀（见常量区说明）
        base = API_ROOT if path.startswith(("/client/", "/app/")) else API_BASE
        url = f"{base}{path}"
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
        return self._post("/client/community/chatroom/getByTeacherId.htm", {"teacherId": teacher_id})

    def is_has_permission(self, teacher_id: int) -> dict:
        """订单/权限：orderStatus=2 表示无有效订单。"""
        return self._post("/order/isHasPermission.htm", {"teacherId": teacher_id})

    def get_sdk_app_id(self) -> str:
        return (self._get("/chatroom/getSdkAppId.htm") or {}).get("sdkAppId", "")

    def get_user_sig(self) -> dict:
        """腾讯 IM 凭证：{'sdkUserId': 'cu_xxx', 'userSig': '...'}"""
        return self._get("/client/community/chatroom/getUserSig.htm") or {}

    def get_invite_code(self, teacher_id: int) -> str:
        """老师邀请码（扫码登录要拼进二维码，如 328 → H8X9）。"""
        return str(self._post("/teacher/getInviteCodeByTeacherId.htm",
                              {"teacherId": teacher_id}) or "")

    def get_user_info(self) -> dict:
        """当前登录用户资料（网页版为 POST 表单 {centralToken}，走 SSO 域）。"""
        r = self.session.post(f"{SSO_BASE}/user/getInfo.htm", data={"centralToken": self.central_token},
                              headers={**self._headers(), "content-type": "application/x-www-form-urlencoded"},
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
        字段对齐网页版（HAR 核对）：cursorId 为空时**不传**；房间流多带 bizType=1。
        """
        data = {"chatRoomId": room_id, "userType": "", "direction": direction,
                "pageSize": page_size, "sourceType": source_type}
        if cursor_id:
            data["cursorId"] = str(cursor_id)
        if source_type == SRC_ROOM:
            data["bizType"] = 1
        return self._post("/client/community/chatrecord/getChatRecordList.htm", data) or []

    def get_pinned(self, room_id: int) -> list:
        """置顶消息（网页版新接口，GET chatRoomId=…）。"""
        return self._get("/client/community/chatrecord/pinnedListById.htm",
                         params={"chatRoomId": room_id}) or []

    # ---------- 研选文章（msgType=2 卡片的正文） ----------
    def get_articles(self, teacher_id: int, page: int = 1, page_size: int = 15) -> dict:
        """文章列表（网页版「研选」列表页同款参数）：
        分页结构 {list, total, hasNextPage}，条目含 id/articleTitle/createTime/feeStatus。"""
        return self._post("/client/article/queryArticleListByPage.htm",
                          {"pageNum": page, "pageSize": page_size, "teacherId": teacher_id,
                           "airticleStatus": 2, "bizType": 1}) or {}

    def get_article(self, article_id: int, teacher_id: int = 0) -> dict:
        """文章详情。articleContent 是富文本 HTML（图片为 fileoss 绝对地址）；
        articleType=2 时是 PDF：articleContent 为 JSON 字符串 {filePath}，
        需再调 get_article_preview(filePath) 换可读 URL。"""
        return self._post("/client/article/queryArticleDetail.htm",
                          {"articleId": article_id, "teacherId": teacher_id})

    def get_article_preview(self, path: str) -> str:
        """PDF 文章的预览地址（articleType=2 时用）。"""
        return self._get("/client/article/preview.htm", params={"path": path}) or ""

    # ---------- 视频（网页版「视频」标签：栏目 + 回放） ----------
    def get_video_columns(self, teacher_id: int) -> list:
        """视频栏目：[{columnId, name, coverImg, feeStatus, number}]。"""
        return self._post("/client/live/column/queryListByTeacherId.htm",
                          {"teacherId": teacher_id}) or []

    def get_video_playbacks(self, teacher_id: int, column_id: int,
                            page: int = 1, page_size: int = 10) -> list:
        """栏目回放列表：[{id, title, coverImg, pubTime, feeStatus, teacherId}]，
        10 条/页。播放页（用户实测）：client.zx093.com/webktc/tougu/index.html
        ?path=/video&id=<id>&teacherId=<tid>"""
        return self._post("/client/live/playback/queryList.htm",
                          {"teacherId": teacher_id, "columnId": column_id,
                           "pageNum": page, "pageSize": page_size}) or []

    def get_video_play(self, video_id: int) -> dict:
        """回放详情（可直接播放）：adaptiveUrl 就是免签 m3u8（三档清晰度，
        CORS 全开）；playbackLink 是腾讯云 VOD FileId，playerSign 是 psign。"""
        return self._get("/app/live/playback/queryById.htm", params={"id": video_id}) or {}

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
        return self._post("/client/community/chatrecord/sendMessage.htm",
                          {"chatRoomId": room_id, "msgType": MSG_TEXT, "msgContent": text})

    def send_image(self, room_id: int, img_url: str, width: int, height: int) -> dict:
        content = json.dumps({"width": int(width), "height": int(height), "imgUrl": img_url},
                             separators=(",", ":"))
        return self._post("/client/community/chatrecord/sendMessage.htm",
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
    """下载图片/头像（CDN 无鉴权）。同 TouguClient：不吃环境代理，直连。"""
    r = requests.get(url, headers={"user-agent": UA, "referer": REFERER},
                     timeout=timeout, proxies={"http": None, "https": None})
    r.raise_for_status()
    return r.content
