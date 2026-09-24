# -*- coding: utf-8 -*-
"""
约牛聊天室 Web 服务
==================
Quart 后端 + 原生 WebSocket + 腾讯云 IM 常驻监听。

功能：
  - 历史消息（游标分页增量同步，落 SQLite）
  - 实时消息（腾讯云 IM WebSocket → 存库 → WS 广播到浏览器）
  - 发文本 / 图片（REST + 阿里云 OSS 直传）
  - 图片、头像本地缓存代理
  - centraltoken / IM 凭证管理

启动：uv run python run.py     浏览器：https://127.0.0.1:5002
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from quart import Quart, abort, jsonify, render_template, request, send_file, websocket

import db
import niulai_api as api
from niulai_api import AuthExpired, NiuLaiError, TouguClient
from niulai_im import TencentImClient

# .env 可选覆盖（首次启动时把环境变量写进数据库设置）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

# ===================== 基础路径 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
IMAGE_DIR = os.path.join(CACHE_DIR, "images")
AVATAR_DIR = os.path.join(CACHE_DIR, "avatars")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
for d in (DATA_DIR, CACHE_DIR, IMAGE_DIR, AVATAR_DIR, BACKUP_DIR):
    os.makedirs(d, exist_ok=True)

DB_PATH = db.DB_PATH

# 允许代理下载的 CDN 域名（防 SSRF）
MEDIA_HOSTS = ("fileoss.zx093.com", "zx093.cn", "zx093.com", "aliyuncs.com",
               "qcloud.com", "my-imcloud.com")

app = Quart(__name__, template_folder="templates")

conn = db.connect(DB_PATH)
_db_lock = threading.RLock()

# ===================== WebSocket 广播 =====================
_ws_clients: set = set()
_ws_lock = threading.Lock()
_broadcast_queue: asyncio.Queue = asyncio.Queue()
_loop: asyncio.AbstractEventLoop | None = None


def broadcast(payload: dict):
    """线程安全地投递广播（IM 线程/MQTT 风格回调里调用）。"""
    try:
        msg = json.dumps(payload, ensure_ascii=False)
        if _loop and _loop.is_running():
            _loop.call_soon_threadsafe(_broadcast_queue.put_nowait, msg)
        else:
            _broadcast_queue.put_nowait(msg)
    except Exception:
        pass


async def _broadcast_loop():
    while True:
        payload = await _broadcast_queue.get()
        with _ws_lock:
            clients = list(_ws_clients)
        dead = []
        for ws in clients:
            try:
                await ws.send(payload)
            except Exception:
                dead.append(ws)
        if dead:
            with _ws_lock:
                for ws in dead:
                    _ws_clients.discard(ws)


# ===================== 配置 =====================
CFG_KEYS = ("centraltoken", "teacher_id", "room_id", "im_sdk_app_id", "im_identifier",
            "im_user_sig", "im_group_id", "im_enabled", "my_user_id", "my_nick_name",
            "my_avatar", "token_saved_at", "captcha_scene_id", "captcha_scene_from")


def cfg() -> dict:
    with _db_lock:
        return db.get_settings(conn, CFG_KEYS)


ENV_SEED = {
    "centraltoken": "NIULAI_CENTRALTOKEN",
    "teacher_id": "NIULAI_TEACHER_ID",
    "room_id": "NIULAI_ROOM_ID",
    "im_sdk_app_id": "NIULAI_IM_SDK_APP_ID",
    "im_identifier": "NIULAI_IM_IDENTIFIER",
    "im_user_sig": "NIULAI_IM_USER_SIG",
    "im_group_id": "NIULAI_IM_GROUP_ID",
    "captcha_scene_id": "NIULAI_CAPTCHA_SCENE_ID",
}


def captcha_scene_id() -> str:
    """当前使用的阿里云验证码 sceneId。

    优先级：设置里的值（可由抓包自动同步）> 源码常量。
    它是**构建期写死在小程序里的常量**（线上 f374igpl / 测试 c613ekby），
    运行时不会变，但约牛发新版本换场景时会变 —— 所以做成可覆盖的，
    并由 tools/capture_login.py 抓到真实登录流量时自动写回。
    """
    return (cfg().get("captcha_scene_id") or "").strip() or api.CAPTCHA_SCENE_ID


def seed_from_env():
    """首次启动时用 .env 里的值填补空设置（已存在的不覆盖）。"""
    with _db_lock:
        for key, env in ENV_SEED.items():
            val = os.environ.get(env, "").strip()
            if val and not db.get_setting(conn, key):
                db.set_setting(conn, key, val)
        if db.get_setting(conn, "im_user_sig") and not db.get_setting(conn, "im_enabled"):
            db.set_setting(conn, "im_enabled", "1")


def _client() -> TouguClient:
    c = cfg()
    return TouguClient(c.get("centraltoken", ""), on_token_expired=_on_token_expired)


# ===================== centraltoken 寿命追踪 =====================
def _on_token_expired():
    """任何业务请求遇到 200001 都会走到这里（同步/发消息/上传…）。

    这是“token 真的失效了”最可靠的信号 —— 约牛不会主动告知，一切以业务返回为准。
    """
    marked = False
    with _db_lock:
        try:
            marked = db.token_mark_expired(conn)
        except Exception as e:                              # noqa: BLE001
            print(f"[token] 记录失效失败：{e}")
    if marked:
        st = token_status()
        used = st.get("age_sec") or 0
        print(f"[token] 已失效，本次共用了 {used / 3600:.1f} 小时")
        broadcast({"type": "token", "data": st})
    broadcast({"type": "status", "auth_expired": True})


def record_token_issued(token: str, source: str = ""):
    """记录一次 token 签发（probe / login / settings / seed 都会调）。"""
    if not token:
        return
    with _db_lock:
        try:
            db.token_issued(conn, token[:12] + "…", source)
        except Exception as e:                              # noqa: BLE001
            print(f"[token] 记录签发失败：{e}")


def seed_token_log():
    """老库迁移：当前已有 token 但 token_log 里没记录时，按 token_saved_at 补登一条。

    只用 unix 时间戳作 set_ts，所以寿命从真实起点（而不是本次启动）算。
    """
    c = cfg()
    token = (c.get("centraltoken") or "").strip()
    if not token:
        return
    with _db_lock:
        if db.token_open(conn):
            return
        try:
            ts = int(datetime.strptime(c.get("token_saved_at") or "",
                                      "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            ts = int(time.time())
        db.token_issued(conn, token[:12] + "…", "migrated", now=ts)
    print(f"[token] 已补登寿命记录（签发于 {datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}）")


def token_status() -> dict:
    """token 寿命概览：已用多久、预计还能用多久、历史样本。"""
    import statistics
    c = cfg()
    saved = c.get("token_saved_at") or ""
    with _db_lock:
        op = db.token_open(conn)
        hist = db.token_history(conn, 12)
    # 以记录为准，没有记录（老库/手动改过）就退回 token_saved_at
    set_ts = op["set_ts"] if op and op.get("set_ts") else 0
    if not set_ts and saved:
        try:
            set_ts = int(datetime.strptime(saved, "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            set_ts = 0
    age = max(0, int(time.time()) - set_ts) if set_ts else 0
    # 真寿司只看 expired（replaced 是被手动换掉，不能当过期末尾）
    real = [h["lifetime"] for h in hist if h.get("reason") == "expired" and h.get("lifetime")]
    est = int(statistics.median(real)) if real else 0
    out = {
        "prefix": (op or {}).get("prefix", (c.get("centraltoken") or "")[:13]),
        "source": (op or {}).get("source", ""),
        "set_at": (op or {}).get("set_at", saved),
        "age_sec": age,
        "samples": len(real),
        "lifetime_min": min(real) if real else 0,
        "lifetime_median": est,
        "lifetime_max": max(real) if real else 0,
        "eta_sec": max(0, est - age) if est else 0,
        "warn": bool(est and age > est * 0.8),
        "history": hist[:6],
    }
    return out


# ===================== 媒体缓存代理 =====================
def _cache_name(url: str, kind: str) -> str:
    base = url.split("?")[0].rstrip("/").split("/")[-1]
    base = "".join(ch for ch in base if ch.isalnum() or ch in "._-")[:60] or "file"
    return f"{hashlib.md5(url.encode()).hexdigest()[:16]}_{base}"


def _media_dir(kind: str) -> str:
    return IMAGE_DIR if kind == "image" else AVATAR_DIR


def cache_media(url: str, kind: str = "image") -> str:
    """下载并缓存远端媒体，返回本地文件名（失败返回空串）。"""
    if not url:
        return ""
    if url.startswith("/"):
        url = api.media_url(url)
    if not url.startswith("http"):
        return ""
    host = url.split("/")[2] if "//" in url else ""
    if not any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS):
        return ""
    key = f"{kind}:{url}"
    with _db_lock:
        local = db.media_local(conn, key)
    path = os.path.join(_media_dir(kind), local) if local else ""
    if local and os.path.exists(path):
        return local
    name = _cache_name(url, kind)
    path = os.path.join(_media_dir(kind), name)
    if not os.path.exists(path):
        try:
            data = api.download_media(url)
            if not data:
                return ""
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:
            print(f"[媒体] 下载失败 {url}: {e}")
            return ""
    with _db_lock:
        db.save_media(conn, key, url, name, kind, os.path.getsize(path))
    return name


_prefetch_queue: "list[tuple[str, str]]" = []
_prefetch_lock = threading.Lock()
_prefetch_seen: set = set()


def prefetch(url: str, kind: str = "image"):
    """把远端媒体排进后台下载队列（去重）。"""
    if not url:
        return
    if url.startswith("/"):
        url = api.media_url(url)
    if not url.startswith("http"):
        return
    with _prefetch_lock:
        if url in _prefetch_seen:
            return
        _prefetch_seen.add(url)
        _prefetch_queue.append((url, kind))


def _prefetch_worker():
    while True:
        with _prefetch_lock:
            item = _prefetch_queue.pop(0) if _prefetch_queue else None
        if not item:
            time.sleep(0.5)
            continue
        try:
            cache_media(item[0], item[1])
        except Exception:
            pass


# ===================== 媒体本地化（纯离线查看） =====================
# 上面那套 prefetch 只能处理「同步/新消息时顺手抓一批」，队列在内存里、上限 400，
# 重启就丢 → 图片只补到 107/463、头像 133/410。
# 这里补一套「以数据库为准」的全量补齐：扫出库里引用到的所有图片与头像，
# 把还缺的下载到 data/cache/ 下，可重复执行（已下载的跳过）。
# 补完之后断网也能翻完整历史，体量约 300~500MB。
MEDIA_AUTOFETCH = os.environ.get("MEDIA_AUTOFETCH", "1").lower() not in ("0", "false", "no")
MEDIA_WORKERS = 4                 # 并发下载数（CDN 友好且比串行快很多）
MEDIA_AUTOFETCH_DELAY = 20        # 启动后等首屏加载完再开始补齐

_media_state = {
    "running": False, "finished": False, "task_total": 0, "task_current": 0,
    "ok": 0, "fail": 0, "bytes": 0, "detail": "",
    "started_at": 0.0, "finished_at": 0.0,
}
_media_lock = threading.RLock()
_media_status_cache = {"at": 0.0, "data": None}
MEDIA_STATUS_TTL = 30             # /api/status 会被轮询，算一次就缓存 30 秒


def _norm_media_url(u: str) -> str:
    """与 cache_media 的归一化保持一致（否则 key 对不上就会重复下载）。"""
    if not u or not isinstance(u, str):
        return ""
    u = u.strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("/"):
        return api.media_url(u)
    return ""


def _host_allowed(url: str) -> bool:
    host = url.split("/")[2] if "//" in url else ""
    return any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS)


def media_targets() -> list:
    """库里引用到的全部远端媒体（消息图片 + 用户头像），已去重、已过滤白名单。"""
    out, seen = [], set()

    def add(u, kind: str):
        n = _norm_media_url(u)
        if not n or n in seen or not _host_allowed(n):
            return
        seen.add(n)
        out.append({"url": n, "kind": kind})

    with _db_lock:
        rows = conn.execute(
            "SELECT msg_type, msg_content FROM messages WHERE msg_type IN (1, 2)"
        ).fetchall()
        avatars = [r[0] for r in conn.execute(
            "SELECT DISTINCT avatar_url FROM users "
            "WHERE avatar_url IS NOT NULL AND avatar_url != ''")]
    for mt, content in rows:
        try:
            obj = json.loads(content or "{}")
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if int(mt or 0) == 1:
            add(obj.get("imgUrl"), "image")
        elif int(mt or 0) == 2:
            add(obj.get("mainImageUrl"), "image")   # 内参卡片封面（样本里为 null）
    for a in avatars:
        add(a, "avatar")
    return out


def media_local_name(url: str, kind: str) -> str:
    """已落盘则返回本地文件名，否则空串。"""
    key = f"{kind}:{url}"
    with _db_lock:
        local = db.media_local(conn, key)
    if local and os.path.exists(os.path.join(_media_dir(kind), local)):
        return local
    return ""


def _dir_size(path: str) -> int:
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_file():
                    n += e.stat().st_size
    except OSError:
        pass
    return n


def media_status(refresh: bool = False) -> dict:
    """本地化进度：还差几张、已占多少磁盘。

    只缓存「数据库统计」那部分（算一次要遍历 662 个目标 + os.stat）；
    任务实时状态必须每次从 _media_state 现取，否则新任务启动后 30 秒内
    会返回上一次的 finished=True，UI 会显示错状态。
    """
    now = time.time()
    cached = _media_status_cache["data"]
    if refresh or not cached or now - _media_status_cache["at"] >= MEDIA_STATUS_TTL:
        cached = _media_status_counts()
        _media_status_cache.update(at=now, data=dict(cached))
    data = dict(cached)
    with _media_lock:
        data.update({k: _media_state[k] for k in
                     ("running", "finished", "ok", "fail", "task_total",
                      "task_current", "detail")})
    return data


def _media_status_counts() -> dict:
    """扫库计算本地化计数与磁盘占用（较慢，结果会被缓存）。"""
    targets = media_targets()
    # 一次查出所有索引，再逐个确认文件还在（662 次单条 SELECT 太浪费）
    with _db_lock:
        rows = conn.execute("SELECT key, local, kind FROM media").fetchall()
    have = set()
    for k, local, kind in rows:
        if local and os.path.exists(os.path.join(_media_dir(kind or "image"), local)):
            have.add(k)
    counts = {"image": [0, 0], "avatar": [0, 0]}      # [已缓存, 总数]
    for t in targets:
        counts[t["kind"]][1] += 1
        if f"{t['kind']}:{t['url']}" in have:
            counts[t["kind"]][0] += 1
    cached, total = counts["image"][0] + counts["avatar"][0], len(targets)
    return {
        "images": counts["image"][0], "images_total": counts["image"][1],
        "avatars": counts["avatar"][0], "avatars_total": counts["avatar"][1],
        "cached": cached, "total": total, "missing": total - cached,
        "bytes": _dir_size(IMAGE_DIR) + _dir_size(AVATAR_DIR),
        "dir": os.path.relpath(CACHE_DIR, BASE_DIR),
    }


def _media_progress():
    with _media_lock:
        snap = {k: _media_state[k] for k in
                ("running", "finished", "task_total", "task_current",
                 "ok", "fail", "bytes", "detail")}
    broadcast({"type": "media_progress", **snap})


def media_sync(limit: int = 0) -> dict:
    """把还未本地化的媒体全部下载到本地。幂等，可反复执行。"""
    with _media_lock:
        if _media_state["running"]:
            return {"error": "已有一个媒体补齐任务在跑"}
        _media_state.update(running=True, finished=False, ok=0, fail=0, bytes=0,
                            task_current=0, task_total=0, detail="扫描待下载媒体…",
                            started_at=time.time(), finished_at=0.0)
        _media_status_cache["data"] = None          # 计数可能已变，别拿旧的
    _media_progress()
    try:
        pend = [t for t in media_targets() if not media_local_name(t["url"], t["kind"])]
        if limit > 0:
            pend = pend[:limit]
        total = len(pend)
        with _media_lock:
            _media_state.update(task_total=total, detail=f"待下载 {total} 个")
        _media_progress()
        if not total:
            with _media_lock:
                _media_state.update(running=False, finished=True, detail="媒体已全部本地化",
                                    finished_at=time.time())
            _media_progress()
            return dict(_media_state)
        done = ok = fail = nbytes = 0
        last_push = 0.0
        with ThreadPoolExecutor(max_workers=MEDIA_WORKERS) as ex:
            futs = {ex.submit(cache_media, t["url"], t["kind"]): t for t in pend}
            for fut in as_completed(futs):
                t = futs[fut]
                done += 1
                try:
                    name = fut.result() or ""
                except Exception:                       # noqa: BLE001
                    name = ""
                if name:
                    ok += 1
                    try:
                        nbytes += os.path.getsize(os.path.join(_media_dir(t["kind"]), name))
                    except OSError:
                        pass
                else:
                    fail += 1
                with _media_lock:
                    _media_state.update(task_current=done, ok=ok, fail=fail, bytes=nbytes,
                                        detail=f"下载中 {done}/{total}（失败 {fail}）")
                now = time.time()
                if now - last_push > 0.8 or done == total:
                    last_push = now
                    _media_progress()
        detail = f"完成：成功 {ok}，失败 {fail}，共 {total} 个"
        with _media_lock:
            _media_state.update(running=False, finished=True, detail=detail,
                                finished_at=time.time())
        _media_status_cache["data"] = None            # 让下次查询重新统计
        _media_progress()
        print(f"[媒体] {detail}，缓存目录 {os.path.relpath(CACHE_DIR, BASE_DIR)}")
        return dict(_media_state)
    except Exception as e:                              # noqa: BLE001
        with _media_lock:
            _media_state.update(running=False, finished=True, detail=f"异常：{e}")
        _media_progress()
        print(f"[媒体] 补齐异常：{e}")
        return dict(_media_state)


def _media_autofetch():
    """启动后自动补齐（想让首屏先加载完再跑）。可用 MEDIA_AUTOFETCH=0 关掉。"""
    time.sleep(MEDIA_AUTOFETCH_DELAY)
    try:
        st = media_status(refresh=True)
        if st["missing"]:
            print(f"[媒体] 自动补齐启动：待下载 {st['missing']} 个"
                  f"（图片 {st['images_total'] - st['images']}、头像 {st['avatars_total'] - st['avatars']}）")
            media_sync()
        else:
            print(f"[媒体] 本地已完整：{st['cached']}/{st['total']} 个"
                  f"，{st['bytes'] / 1048576:.1f} MB，可离线查看")
    except Exception as e:                              # noqa: BLE001
        print(f"[媒体] 自动补齐异常：{e}")


# ===================== 消息入库 =====================
# 写入分批：每批只占一小会儿锁，中间让出 GIL，
# 否则全量同步时网页请求会被锁住（实测要 20-30 秒才响应，TLS 握手都超时）
INGEST_CHUNK = 250
PREFETCH_LIMIT = 400        # 待下载队列上限，避免全量同步时积压上万张图


def ingest(room_id: int, msgs: list, origin: str = "rest") -> int:
    """入库 + 触发媒体预取 + 广播。返回新增条数。"""
    if not msgs:
        return 0
    new_count = 0
    new_im: list = []
    for i in range(0, len(msgs), INGEST_CHUNK):
        chunk = msgs[i:i + INGEST_CHUNK]
        with _db_lock:
            if origin == "im":
                # 实时消息只有 1~2 条，逐条判重（要拿到「哪些是新的」才能广播）
                for m in chunk:
                    if db.message_exists(conn, room_id, m):
                        continue
                    db.upsert_messages(conn, room_id, [m], origin)
                    new_im.append(m)
            else:
                new_count += db.upsert_messages(conn, room_id, chunk, origin)
            conn.commit()
        if origin == "im":
            new_count = len(new_im)
        time.sleep(0.005)                     # 让出 GIL / 事件循环
    _schedule_prefetch(msgs)
    for m in new_im:
        broadcast({"type": "message", "data": _msg_payload_from_dict(room_id, m)})
    return new_count


def _schedule_prefetch(msgs: list):
    """媒体预取（限量，避免全量同步时把 CDN 打爆）。"""
    with _prefetch_lock:
        budget = PREFETCH_LIMIT - len(_prefetch_queue)
    if budget <= 0:
        return
    for m in msgs:
        if budget <= 0:
            return
        av = m.get("avatar_url") or m.get("avatarUrl")
        if av:
            prefetch(av, "avatar")
        if int(m.get("msg_type") or m.get("msgType") or 0) == 1:
            content = m.get("msg_content") or m.get("msgContent") or ""
            try:
                prefetch(json.loads(content).get("imgUrl", ""), "image")
            except Exception:
                pass
        budget -= 1


def _msg_payload_from_dict(room_id: int, m: dict) -> dict:
    """把归一化 dict 转成前端结构（与 db._msg_to_dict 输出一致）。"""
    d = {
        "id": int(m.get("id") or 0),
        "room_id": room_id,
        "msg_type": int(m.get("msg_type") or m.get("msgType") or 0),
        "msg_content": m.get("msg_content") if m.get("msg_content") is not None else m.get("msgContent"),
        "msg_date": m.get("msg_date") or m.get("msgDate") or "",
        "user_id": int(m.get("user_id") or m.get("userId") or 0),
        "nick_name": m.get("nick_name") or m.get("nickName") or "",
        "yn_no": m.get("yn_no") or m.get("ynNo") or "",
        "user_type": int(m.get("user_type") or m.get("userType") or 1),
        "avatar_url": m.get("avatar_url") or m.get("avatarUrl") or "",
        "real_name": m.get("real_name") or m.get("realName"),
        "certificate_num": m.get("certificate_num") or m.get("certificateNum"),
        "private_message_flag": 1 if (m.get("private_message_flag") or m.get("privateMessageFlag")) else 0,
        "teacher_id": int(m.get("teacher_id") or m.get("teacherId") or 0),
        "to_user_id": int(m.get("to_user_id") or m.get("toUserId") or 0),
        "vip_user": 1 if (m.get("vip_user") or m.get("vipUser")) else 0,
        "audit_status": int(m.get("audit_status") or m.get("auditStatus") or 1),
        "im_msg_seq": int(m.get("im_msg_seq") or m.get("imMsgSeq") or 0),
        "images": [],
        "quote": None,
        "image": None,
    }
    if d["msg_type"] == 1:
        try:
            obj = json.loads(d["msg_content"] or "{}")
            d["image"] = {"url": obj.get("imgUrl", ""), "width": obj.get("width", 0),
                          "height": obj.get("height", 0)}
        except Exception:
            d["image"] = None
    q = m.get("quote_json")
    if q and isinstance(q, str):
        try:
            d["quote"] = json.loads(q)
        except Exception:
            pass
    d["ts"] = m.get("ts") or api.parse_msg_time(d["msg_date"])
    return d


# ===================== 腾讯云 IM 常驻监听 =====================
_im_state = {"state": "off", "detail": "", "identifier": "", "group": "",
             "last_msg_time": "", "last_push_at": 0.0, "messages": 0}
_im_reload = threading.Event()
_im_status_lock = threading.Lock()
# 「IM 重连成功」时用它把同步线程立刻叫醒（那一刻最需要补漏，见 _sync_worker）
_sync_wake = threading.Event()


def im_config() -> dict | None:
    c = cfg()
    if c.get("im_enabled") not in ("1", "true", "True", ""):
        return None
    sdk = c.get("im_sdk_app_id") or ""
    ident = c.get("im_identifier") or ""
    sig = c.get("im_user_sig") or ""
    group = c.get("im_group_id") or ""
    if not (sdk and ident and sig):
        return None
    return {"sdk_app_id": int(sdk), "identifier": ident, "user_sig": sig,
            "group_ids": [group] if group else [], "room_id": int(c.get("room_id") or 0)}


def _im_on_message(m: dict):
    c = cfg()
    room_id = int(c.get("room_id") or 0)
    with _im_status_lock:
        _im_state["last_msg_time"] = m.get("msg_date", "")
        _im_state["last_push_at"] = time.time()
        _im_state["messages"] += 1
    ingest(room_id, [m], origin="im")


def _im_on_event(e: dict):
    broadcast({"type": "event", "data": e})


_im_sig_refresh_at = 0.0


def _im_on_status(state: str, detail: str):
    global _im_sig_refresh_at
    with _im_status_lock:
        was = _im_state["state"]
        _im_state["state"] = state
        _im_state["detail"] = detail
    broadcast({"type": "status", "im_state": state, "im_detail": detail})
    # 刚进入 online（首次连上 / 断线重连）→ 立即叫醒同步线程补一次：
    # IM 客户端没实现离线补拉，断开窗口里的消息只能靠 REST 找回来。
    if state == "online" and was != "online":
        _sync_wake.set()
    # 70402 = userSig 过期（约牛只签几十分钟，小程序每次启动都重新拿）：
    # 用 centraltoken 现换一张并立刻重连。30s 冷却，centraltoken 也死了时不刷屏。
    if state == "login_failed" and time.time() >= _im_sig_refresh_at:
        _im_sig_refresh_at = time.time() + 30
        threading.Thread(target=_auto_relogin, daemon=True).start()


def _refresh_im_sig() -> bool:
    """用 centraltoken 换一张新 userSig（含 sdkAppId / sdkUserId 一并刷新）。"""
    c = cfg()
    if not c.get("centraltoken"):
        return False
    try:
        client = _client()
        sdk = client.get_sdk_app_id()
        sig = client.get_user_sig()
    except Exception as e:                              # noqa: BLE001
        print(f"[IM] userSig 续签失败：{e}", flush=True)
        return False
    if not (sig.get("userSig") and sig.get("sdkUserId")):
        print(f"[IM] userSig 续签失败：返回缺字段 {sig}", flush=True)
        return False
    with _db_lock:
        if sdk:
            db.set_setting(conn, "im_sdk_app_id", str(sdk))
        db.set_setting(conn, "im_identifier", sig["sdkUserId"])
        db.set_setting(conn, "im_user_sig", sig["userSig"])
    print(f"[IM] userSig 已续签（{sig['sdkUserId']}）", flush=True)
    return True


def _auto_relogin():
    """login_failed 回调的续签线程：换到新 sig 就叫醒 supervisor 重连。"""
    if _refresh_im_sig():
        _im_reload.set()


async def _im_session(c: dict):
    client = TencentImClient(c["sdk_app_id"], c["identifier"], c["user_sig"], c["group_ids"],
                             on_message=_im_on_message, on_event=_im_on_event,
                             on_status=_im_on_status,
                             logger=lambda *a: print(time.strftime("[%H:%M:%S]"), *a))
    with _im_status_lock:
        _im_state["identifier"] = c["identifier"]
        _im_state["group"] = (c["group_ids"] or [""])[0]
    task = asyncio.create_task(client.run_forever())
    try:
        while not task.done():
            if _im_reload.is_set():
                break
            await asyncio.sleep(0.5)
    finally:
        await client.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _im_supervisor():
    while True:
        c = im_config()
        if not c:
            with _im_status_lock:
                _im_state.update(state="off", detail="未配置 IM 凭证（userSig）")
            _im_reload.wait(5)
            _im_reload.clear()
            continue
        _im_reload.clear()
        try:
            asyncio.run(_im_session(c))
        except Exception as e:
            print(f"[IM] supervisor 异常：{e}")
        if not _im_reload.is_set():
            # login_failed（sig/token 失效）时放慢到 60s，别拿废票每 5s 砸登录口；
            # 续签成功会 set(_im_reload)，走不到这里，立即重连。
            with _im_status_lock:
                failed = _im_state.get("state") == "login_failed"
            time.sleep(60 if failed else 5)


def restart_im():
    _im_reload.set()


def reset_sync_backoff():
    """拿到新 centraltoken 后清掉失效标记，否则同步会被退避挡着（最长 10 分钟）。"""
    _sync_state["auth_failed"] = False
    _sync_state["next_try"] = 0.0
    _sync_state["detail"] = ""


# ===================== 历史同步 =====================
_sync_state = {"running": False, "detail": "", "added": 0, "auth_failed": False,
              "next_try": 0.0, "mode": "", "pages": 0, "fetched": 0,
              "oldest": "", "newest": "", "last_page_ms": 0, "done": False,
              "interval": 0, "interval_reason": ""}

# pageSize 实测无上限：1000 条 0.95s、500 条 0.78s。
# 取不到时逐级降级，避免某个尺寸被服务端拒绝就卡死。
PAGE_SIZE_LADDER = (1000, 500, 200, 50, 15)
SYNC_SLEEP = 0.3          # 页间隔，别把人家打挂


def _sync_progress(extra: dict | None = None):
    snap = {k: _sync_state[k] for k in ("running", "mode", "pages", "fetched", "added",
                                        "oldest", "newest", "done", "detail")}
    if extra:
        snap.update(extra)
    broadcast({"type": "sync_progress", **snap})


def sync_history(pages: int = 3, source_type: int = api.SRC_ROOM, stop_at_known: bool = True,
                 full: bool = False, page_size: int = 1000, max_messages: int = 200000,
                 since_date: str = "", until_id: int = 0):
    """从最新往历史翻页拉消息。

    full=True  → 一直翻到服务端返回空数组（不限页数，遇到已有消息也继续翻，用于补齐空档）
    full=False → 最多 pages 页，且在第 2 页起遇到库里已有 id 就停（日常增量）
    since_date → 只同步比该日期（YYYY-MM-DD）新的消息，到了就停
    """
    c = cfg()
    room_id = int(c.get("room_id") or 0)
    if not room_id:
        return {"error": "尚未确定房间，请先设置 centraltoken 或房间号"}
    client = _client()

    _sync_state.update(running=True, mode="full" if full else "incremental", pages=0,
                       fetched=0, added=0, oldest="", newest="", done=False,
                       detail="全量同步中…" if full else "同步最新中…")
    _sync_progress()
    mode = "全量" if full else "增量"
    print(f"[同步] {time.strftime('%H:%M:%S')} {mode}开始 room={room_id} page_size={page_size}",
          flush=True)

    added_total = fetched = 0
    cursor = ""
    sizes = [page_size] + [s for s in PAGE_SIZE_LADDER if s < page_size]
    i = 0
    error = ""
    while True:
        if not full and i >= max(1, pages):
            break
        if fetched >= max_messages:
            _sync_state["detail"] = f"已达上限 {max_messages} 条，暂停"
            break
        items, last_err = None, ""
        for ps in sizes:                      # 逐级降级重试
            try:
                items = client.get_chat_records(room_id, cursor, 1, ps, source_type)
                break
            except AuthExpired:
                _sync_state.update(detail="登录失效，请更新 centraltoken", auth_failed=True,
                                   next_try=time.time() + 600, running=False, done=True)
                print(f"[同步] {time.strftime('%H:%M:%S')} {mode}中止：登录失效（centraltoken 过期）",
                      flush=True)
                _on_token_expired()
                return {"error": "登录失效，请更新 centraltoken"}
            except NiuLaiError as e:
                last_err = str(e)
                continue
            except Exception as e:            # noqa: BLE001 —— 网络/解码异常也要降级重试
                last_err = f"{type(e).__name__}: {e}"
                continue
        if items is None:
            error = last_err or "请求失败"
            _sync_state.update(detail=f"失败：{error}", running=False, done=True)
            print(f"[同步] {time.strftime('%H:%M:%S')} {mode}失败（第 {i + 1} 次请求）：{error}",
                  flush=True)
            return {"error": error}
        if not items:
            _sync_state["detail"] = f"已到最早（共 {fetched} 条）"
            break

        i += 1
        t0 = time.time()
        fetched += len(items)
        # 两个都记上：stop_at_known 分支会直接 break，后面那段 update 不会再跑到
        _sync_state.update(pages=i, fetched=fetched)
        # 消息按时间倒序回，id 也随之递减：第 1 页里一旦出现「库里已有」的 id，
        # 说明已经追上（边界就在本页），更旧的页必然全是已有的 —— 不必再白跑一页。
        # 以前写的是 i > 1，于是每轮固定浪费 1 个请求。真正的补空档交给「全量同步」。
        known = _known_ids(room_id, items)
        added_before = added_total
        if stop_at_known and not full and known:
            added_total += ingest(room_id, [x for x in items
                                            if int(x.get("id") or 0) not in known], "rest")
            _sync_state["detail"] = "已追上"
            print(f"[同步] {time.strftime('%H:%M:%S')} 增量 第{i}页 追上（本页新增 {added_total - added_before} 条，"
                  f"本页 {int((time.time() - t0) * 1000)}ms）", flush=True)
            break
        added_total += ingest(room_id, items, "rest")

        oldest = (items[-1].get("msgDate") or "")[:10]
        newest = (items[0].get("msgDate") or "")[:10]
        _sync_state.update(pages=i, fetched=fetched, added=added_total,
                           oldest=oldest, newest=newest, last_page_ms=int((time.time() - t0) * 1000),
                           detail=f"已取 {fetched} 条（+{added_total}），最早到 {oldest}")
        _sync_progress()
        print(f"[同步] {time.strftime('%H:%M:%S')} {mode} 第{i}页 取{len(items)}条 新增{added_total - added_before}条"
              f" 累计{fetched}条(+{added_total}) 最早到{oldest or '?'}"
              f" 本页{int((time.time() - t0) * 1000)}ms", flush=True)

        cursor = str(items[-1].get("id") or "")
        with _db_lock:
            db.set_sync_state(conn, room_id, source_type, cursor,
                              max(int(x.get("id") or 0) for x in items))
        if not cursor:
            break
        if until_id and int(cursor) <= until_id:      # 已经翻到指定界线
            _sync_state["detail"] = f"已到指定位置（共 {fetched} 条）"
            break
        if since_date and oldest and oldest < since_date:
            _sync_state["detail"] = f"已到 {oldest}（不早于 {since_date}，共 {fetched} 条）"
            break
        time.sleep(SYNC_SLEEP)

    # 收尾：把「停在哪 / 跑了几次请求 / 新增多少」讲清楚。
    # pages 必须用本地 i —— 以前读的是 _sync_state['pages']，而 stop_at_known
    # 分支从来没更新过它，fetched 也在 break 之前没加，于是明明拉了 1 页
    # 却显示「完成：0 页 0 条」，看上去像什么都没干。
    head = _sync_state["detail"] or "完成"
    _sync_state.update(auth_failed=False, running=False, done=True, pages=i,
                       detail=(_sync_state["detail"] if error
                               else f"{head} · {i} 次请求，新增 {added_total} 条"))
    _sync_progress()
    print(f"[同步] {time.strftime('%H:%M:%S')} {mode}完成：{head}（{i} 次请求，取回 {fetched} 条，新增 {added_total} 条）",
          flush=True)
    return {"added": added_total, "fetched": fetched, "pages": _sync_state["pages"],
            "oldest": _sync_state["oldest"]}


def _known_ids(room_id: int, items: list) -> set:
    if not items:
        return set()
    ids = [int(x.get("id") or 0) for x in items if int(x.get("id") or 0)]
    if not ids:
        return set()
    ph = ",".join("?" * len(ids))
    with _db_lock:
        rows = conn.execute(
            f"SELECT id FROM messages WHERE room_id=? AND id IN ({ph})", [room_id] + ids).fetchall()
    return {int(r["id"]) for r in rows}


# ===================== 同步节奏（自适应） =====================
# IM 是主通道（秒级），REST 只负责「补漏 + 校正」。间隔按 IM 健康度来定：
SYNC_IV_IM_UNSTABLE = 30    # IM 正在连 / 刚断：30 秒（很可能刚漏消息）
SYNC_IV_NO_IM = 60          # 没配 IM 或凭证失效：REST 是唯一通道，1 分钟一次


def _sync_interval() -> tuple[int, str]:
    """下次轮询间隔（秒）与原因。**IM 在线时返回 0 = 不定时轮询**：
    推送已经是主通道，同步改成纯事件驱动（启动时一次 + IM 重连成功时一次）。
    只有 IM 不可用（没有推送可收）才保留定时轮询兜底。"""
    with _im_status_lock:
        state = _im_state.get("state") or "off"
    if state == "online":
        return 0, "IM 在线，不轮询（启动/重连时自动拉）"
    if state in ("off", "login_failed"):
        return SYNC_IV_NO_IM, "IM 不可用，REST 为主（1 分钟一次）"
    return SYNC_IV_IM_UNSTABLE, f"IM {state}，积极补漏"


def _sync_worker():
    """后台增量同步（**事件驱动为主**）。

    IM 推送是主通道（秒级），这里只做「补漏」：
      - 启动时拉一次当天（startup 里 _sync_wake.set()）
      - IM 重连成功立刻补一次（断线窗口里的消息 IM 不补发，只能靠 REST 找回）
      - IM 不可用（off/login_failed）时退回 1 分钟轮询——那会儿没有推送可收
    登录失效后 10 分钟内不再重试（避免无意义的错误日志），用户更新 token 后立即恢复。
    """
    while True:
        iv, why = _sync_interval()
        _sync_state["interval"] = int(iv)
        _sync_state["interval_reason"] = why
        if iv:
            _sync_wake.wait(iv)                # IM 不可用：定时兜底
        else:
            _sync_wake.wait()                  # IM 在线：不定时，纯等事件（启动/重连）
        _sync_wake.clear()
        c = cfg()
        if not c.get("centraltoken") or not c.get("room_id"):
            continue
        if _sync_state["auth_failed"] and time.time() < _sync_state["next_try"]:
            continue
        with _sync_claim_lock:               # 和 /api/sync 同一把锁，避免双开
            if _sync_state["running"]:
                continue
            _sync_state["running"] = True
        try:
            r = sync_history(pages=2)        # 追上就停在第 1 页，见 sync_history 的 stop_at_known
            if r.get("added"):
                broadcast({"type": "synced", "added": r["added"], "mode": "incremental"})
        except Exception as e:
            print(f"[同步] 失败：{e}")
        finally:
            _sync_state["running"] = False

# ===================== 页面 =====================
@app.route("/")
async def index():
    c = cfg()
    return await render_template("index.html",
                                 room_id=c.get("room_id") or "",
                                 teacher_id=c.get("teacher_id") or "328")


# ===================== 状态 / 配置 API =====================
@app.route("/api/status")
async def api_status():
    c = cfg()
    with _im_status_lock:
        ims = dict(_im_state)
    with _db_lock:
        st = db.stats(conn, int(c.get("room_id") or 0) or None)
        rooms = db.list_rooms(conn)
    return jsonify({
        "has_token": bool(c.get("centraltoken")),
        "token_saved_at": c.get("token_saved_at", ""),
        "token": token_status(),
        "captcha_scene_id": captcha_scene_id(),
        "captcha_scene_from": c.get("captcha_scene_from", ""),
        "teacher_id": c.get("teacher_id", ""),
        "room_id": c.get("room_id", ""),
        "my_user_id": c.get("my_user_id", ""),
        "my_nick_name": c.get("my_nick_name", ""),
        "im": ims,
        "sync": dict(_sync_state),
        "media": media_status(),
        "stats": {"total": st["total"], "teacher": st["teacher"], "images": st["images"],
                  "users": st["users"], "first": st["first"], "last": st["last"]},
        "rooms": rooms,
    })


@app.route("/api/settings", methods=["POST"])
async def api_settings():
    """写入配置。支持的键：centraltoken / teacher_id / im_sdk_app_id / im_identifier /
    im_user_sig / im_group_id / im_enabled。"""
    data = await request.get_json(force=True) or {}
    allowed = {"centraltoken", "teacher_id", "room_id", "im_sdk_app_id", "im_identifier",
               "im_user_sig", "im_group_id", "im_enabled", "my_user_id", "my_nick_name",
               "captcha_scene_id", "captcha_scene_from"}
    changed_im = False
    with _db_lock:
        for k, v in data.items():
            if k not in allowed:
                continue
            db.set_setting(conn, k, str(v))
            if k.startswith("im_"):
                changed_im = True
        if "centraltoken" in data:
            db.set_setting(conn, "token_saved_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if data.get("centraltoken"):
        record_token_issued(str(data["centraltoken"]).strip(), "settings")
    if changed_im:
        restart_im()
    if data.get("centraltoken"):
        reset_sync_backoff()
    return jsonify({"status": "ok"})


def apply_centraltoken(token: str, teacher_id: int, source_hint: str = "probe") -> tuple[dict, str]:
    """校验 centraltoken 并落库：房间信息 + IM 凭证 + 我的资料。

    返回 (结果, 错误串)。/api/probe（粘贴 token）与 /api/login/password
    （账密登录后自动接上）共用。内部全是同步 HTTP，调用方要丢线程池。
    """
    client = TouguClient(token)
    try:
        room = client.get_room_by_teacher(teacher_id)
    except AuthExpired:
        return {}, "centraltoken 已失效（返回 200001），请重新抓取"
    except NiuLaiError as e:
        return {}, str(e)

    out = {"room": {"id": room.get("id"), "name": room.get("name"),
                    "teacher": room.get("realName") or room.get("teacherName"),
                    "imGroupId": room.get("imGroupId"),
                    "certificateNum": room.get("certificateNum")}}
    with _db_lock:
        db.save_room(conn, room)
        db.set_setting(conn, "centraltoken", token)
        db.set_setting(conn, "teacher_id", str(teacher_id))
        db.set_setting(conn, "room_id", str(room.get("id") or ""))
        db.set_setting(conn, "im_group_id", room.get("imGroupId") or "")
        db.set_setting(conn, "token_saved_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    record_token_issued(token, "probe" if source_hint == "probe" else "login")

    # 顺带刷新 IM 凭证 + 我的资料
    im_ready = False
    try:
        sdk = client.get_sdk_app_id()
        sig = client.get_user_sig()
        info = client.get_user_info()
        with _db_lock:
            db.set_setting(conn, "im_sdk_app_id", str(sdk or ""))
            db.set_setting(conn, "im_identifier", sig.get("sdkUserId", ""))
            db.set_setting(conn, "im_user_sig", sig.get("userSig", ""))
            db.set_setting(conn, "im_enabled", "1")
            db.set_setting(conn, "my_user_id", str(info.get("userId", "")))
            db.set_setting(conn, "my_nick_name", info.get("nickName", ""))
            db.set_setting(conn, "my_avatar", info.get("photo", ""))
        im_ready = bool(sdk and sig.get("userSig"))
        out["user"] = {"userId": info.get("userId"), "nickName": info.get("nickName"),
                       "ynNo": info.get("ynNo")}
        out["im"] = {"sdkAppId": sdk, "sdkUserId": sig.get("sdkUserId")}
    except NiuLaiError as e:
        out["im_error"] = str(e)
    restart_im()
    reset_sync_backoff()      # 新 token 生效，同步退避立即解除
    out["im_ready"] = im_ready
    return out, ""


@app.route("/api/probe", methods=["POST"])
async def api_probe():
    """验证 centraltoken，并自动带出房间信息 + IM 凭证。"""
    data = await request.get_json(force=True) or {}
    token = (data.get("centraltoken") or "").strip()
    teacher_id = int(data.get("teacher_id") or cfg().get("teacher_id") or 328)
    if not token:
        return jsonify({"error": "请填写 centraltoken"}), 400
    # 同步 HTTP，别卡着事件循环（同 /api/media 那个坑）
    out, err = await asyncio.to_thread(apply_centraltoken, token, teacher_id)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(out)


# ===================== 账号密码登录 =====================
# 链路（源码逆向 + 已用真实 crypto-js 交叉验证）：
#   accountName/pwd = encryptByDES(...)   DES/ECB/PKCS7，密钥硬编码在小程序里
#   + captchaVerifyParam（阿里云验证码票据，一次性、短时效）
#   → POST account.zx093.cn/stoneserver/v1/account/accountPwdVerifyLogin.htm
#   → data 就是 centraltoken（小程序把它 setStorageSync("token") 后当 centralToken 头用）
@app.route("/api/logout", methods=["POST"])
async def api_logout():
    """退出登录：清凭证（centraltoken / IM / 我的资料），消息库与房间配置保留。"""
    keys = ("centraltoken", "im_user_sig", "im_identifier", "im_sdk_app_id",
            "im_group_id", "im_enabled", "my_user_id", "my_nick_name", "my_avatar")
    with _db_lock:
        for k in keys:
            db.set_setting(conn, k, "")
        try:
            db.token_mark_expired(conn, reason="logout")
        except Exception as e:                              # noqa: BLE001
            print(f"[token] 退出收尾失败：{e}")
    reset_sync_backoff()
    restart_im()          # im_config 拿不到凭证 → IM 自动下线
    broadcast({"type": "status", "im_state": "off", "im_detail": "已退出登录"})
    print("[token] 已退出登录（凭证清空，消息库保留）", flush=True)
    return jsonify({"ok": True})


@app.route("/api/login/qrcode", methods=["POST"])
async def api_login_qrcode():
    """生成微信扫码登录二维码。body: {teacher_id?}（默认用设置里的）。
    返回 {qrcode: base64 jpg, auth_token}——前端持 auth_token 轮询 poll。"""
    data = await request.get_json(force=True) or {}
    teacher_id = int(data.get("teacher_id") or cfg().get("teacher_id") or 328)

    def _gen() -> tuple[str, str, bytes]:
        invite = (TouguClient(cfg().get("centraltoken", "")).get_invite_code(teacher_id)
                  or "").strip() or "H8X9"
        at = api.get_auth_token()
        img = api.create_login_qrcode(at, invite)
        return invite, at, img

    try:
        invite, at, img = await asyncio.to_thread(_gen)
    except NiuLaiError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:                                    # noqa: BLE001
        return jsonify({"error": f"二维码生成失败：{e}"}), 500
    import base64
    return jsonify({"qrcode": base64.b64encode(img).decode(), "auth_token": at,
                    "invite": invite, "teacher_id": teacher_id})


@app.route("/api/login/qrcode/poll", methods=["POST"])
async def api_login_qrcode_poll():
    """轮询扫码状态：未扫 {status:waiting}；确认后走 apply_centraltoken 并 {status:ok}。"""
    data = await request.get_json(force=True) or {}
    at = (data.get("auth_token") or "").strip()
    teacher_id = int(data.get("teacher_id") or cfg().get("teacher_id") or 328)
    if not at:
        return jsonify({"error": "缺少 auth_token"}), 400
    try:
        token = await asyncio.to_thread(api.exchange_qrcode_token, at)
    except AuthExpired:
        return jsonify({"status": "expired", "error": "二维码已过期，请重新生成"})
    except NiuLaiError as e:
        return jsonify({"status": "error", "error": str(e)})
    if not token:
        return jsonify({"status": "waiting"})
    out, err = await asyncio.to_thread(apply_centraltoken, token, teacher_id, "login")
    if err:
        return jsonify({"status": "error", "error": err})
    return jsonify({"status": "ok", "user": out.get("user"),
                    "room": (out.get("room") or {}).get("name", "")})


# ===================== 已存账号（.env 的 NIULAI_SAVED_LOGINS，多账户） =====================
SAVED_LOGINS_KEY = "NIULAI_SAVED_LOGINS"
_ENV_PATH = os.path.join(BASE_DIR, ".env")


def _read_saved_logins() -> list:
    """[{account, password}]——直接读 .env 文件（不走 os.environ，改完即生效）。"""
    if not os.path.exists(_ENV_PATH):
        return []
    for line in open(_ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line.startswith(SAVED_LOGINS_KEY + "="):
            raw = line[len(SAVED_LOGINS_KEY) + 1:].strip().strip('"').strip("'")
            try:
                data = json.loads(raw)
                return [x for x in data if isinstance(x, dict) and x.get("account")]
            except Exception:                              # noqa: BLE001
                return []
    return []


def _write_saved_logins(items: list):
    """整键重写 NIULAI_SAVED_LOGINS=<json 单行>，.env 里其它行原样保留。"""
    lines = []
    if os.path.exists(_ENV_PATH):
        lines = open(_ENV_PATH, encoding="utf-8").read().splitlines()
    new = f"{SAVED_LOGINS_KEY}={json.dumps(items, ensure_ascii=False)}"
    for i, l in enumerate(lines):
        if l.strip().startswith(SAVED_LOGINS_KEY + "="):
            lines[i] = new
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(new)
    with open(_ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@app.route("/api/login/saved")
async def api_saved_logins():
    """历史账号列表（本机 .env，含已存密码——本机自用所以不脱敏）。"""
    return jsonify({"logins": _read_saved_logins()})


@app.route("/api/login/saved", methods=["POST"])
async def api_save_login():
    """登录成功后落盘：账号总是保存（多账户去重），密码按 save_password 勾选。"""
    data = await request.get_json(force=True) or {}
    account = (data.get("account") or "").strip()
    password = data.get("password") or ""
    save_password = bool(data.get("save_password", True))
    if not account:
        return jsonify({"error": "账号为空"}), 400
    pwd = password if (save_password and password) else ""
    now = int(time.time())
    items = _read_saved_logins()
    for it in items:
        if it.get("account") == account:
            it["password"] = pwd
            it["used_at"] = now            # 记录最近一次登录时间，前端据此默认选中
            break
    else:
        items.append({"account": account, "password": pwd, "used_at": now})
    _write_saved_logins(items)
    return jsonify({"ok": True, "saved_password": bool(pwd)})


@app.route("/api/login/saved", methods=["DELETE"])
async def api_delete_login():
    data = await request.get_json(force=True) or {}
    account = (data.get("account") or "").strip()
    _write_saved_logins([x for x in _read_saved_logins() if x.get("account") != account])
    return jsonify({"ok": True})


@app.route("/api/login/password", methods=["POST"])
async def api_login_password():
    """账密登录（网页版链路）。成功即写 centraltoken。"""
    """账号密码登录 → 自动拿到 centraltoken 并完成配置。

    body: {account, password, captcha_verify_param, scene_id?}
    captcha_verify_param 是阿里云验证码通过后的票据（在 /login 页面过滑块取）。
    """
    data = await request.get_json(force=True) or {}
    account = (data.get("account") or "").strip()
    password = data.get("password") or ""
    param = (data.get("captcha_verify_param") or "").strip()
    scene = (data.get("scene_id") or captcha_scene_id()).strip()
    if not account or not password:
        return jsonify({"error": "账号和密码都要填"}), 400
    if not param:
        return jsonify({"error": "缺少 captchaVerifyParam：先在页面上过滑块，或粘贴票据"}), 400
    try:
        token = await asyncio.to_thread(api.login_by_password, account, password, param, scene)
    except NiuLaiError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                  # noqa: BLE001
        return jsonify({"error": f"请求异常：{e}"}), 500
    out, err = await asyncio.to_thread(apply_centraltoken, token,
                                       int(cfg().get("teacher_id") or 328), "login")
    if err:
        return jsonify({"error": f"登录成功但校验失败：{err}", "token": token}), 400
    out["token_len"] = len(token)
    out["token_prefix"] = token[:12] + "…"
    return jsonify(out)


@app.route("/api/login/verify-sample", methods=["POST"])
async def api_login_verify_sample():
    """逆向自检：拿抓包抓到的 accountPwdVerifyLogin 请求体，
    解出账密、重算 sign，验证我们对源码的复刻是否逐字节正确。

    body: 直接给抓到的表单字段（accountName/pwd/sign/...），或 {body: {...}}
    """
    data = await request.get_json(force=True) or {}
    body = data.get("body") or data

    def _run():
        out = {"fields": sorted(k for k in body if k != "sign")}
        try:
            out["account"] = api.decrypt_by_des(body.get("accountName", ""))
            out["pwd_len"] = len(api.decrypt_by_des(body.get("pwd", "")))
            out["decrypt_ok"] = True
        except Exception as e:                              # noqa: BLE001
            out["decrypt_ok"] = False
            out["decrypt_error"] = str(e)
        got = body.get("sign")
        if got:
            without = {k: v for k, v in body.items() if k != "sign"}
            out["sign_expected"] = api.get_sign(without)
            out["sign_got"] = str(got)
            out["sign_ok"] = out["sign_expected"].upper() == str(got).upper()
        return out

    return jsonify(await asyncio.to_thread(_run))


@app.route("/login")
async def login_page():
    """账号密码登录页：本地页面 + 阿里云验证码 Web SDK，过滑块后自动登录。"""
    c = cfg()
    scene = captcha_scene_id()
    src = c.get("captcha_scene_from") or ""
    return await render_template("login.html", scene_id=scene, scene_from=src)


@app.route("/api/probe-im", methods=["POST"])
async def api_probe_im():
    """只验证 IM 凭证是否还能用（连一次腾讯云 IM 做 wslogin）。"""
    data = await request.get_json(force=True) or {}
    with _db_lock:
        c = cfg()
        sdk = int(data.get("im_sdk_app_id") or c.get("im_sdk_app_id") or 0)
        ident = data.get("im_identifier") or c.get("im_identifier") or ""
        sig = data.get("im_user_sig") or c.get("im_user_sig") or ""
    if not (sdk and ident and sig):
        return jsonify({"ok": False, "error": "缺少 sdkAppId / identifier / userSig"}), 400

    import websockets
    from niulai_im import CMD_HEARTBEAT, CMD_LOGIN, decode_frame, encode_frame, \
        WEBSDK_APPID, WEBSDK_VERSION, SDK_ABILITY
    import secrets as _secrets
    import random as _random

    url = (f"wss://{sdk}w4c.my-imcloud.com/binfo?sdkappid={sdk}"
           f"&instanceid={_secrets.token_hex(16)}&random={_random.random()}&platform=8"
           f"&host=mac&version=-1&sdkversion=4.4.3&compress=gzip")
    result = {"ok": False}
    try:
        async with websockets.connect(
                url, origin=f"https://{sdk}w4c.my-imcloud.com",
                additional_headers={"User-Agent": api.UA, "content-type": "application/json"},
                max_size=None, open_timeout=12, ping_interval=None) as ws:
            seq = [1000]

            def frame(cmd, extra=None, body=None):
                h = {"servcmd": cmd, "ver": "v4", "platform": 8, "websdkappid": WEBSDK_APPID,
                     "websdkversion": WEBSDK_VERSION, "status_instid": 0, "sdkappid": sdk,
                     "contenttype": "json", "reqtime": int(time.time()),
                     "sdkability": SDK_ABILITY, "sdkability_ext": "", "cappid": 0,
                     "tjgID": "", "seq": seq[0], "cs": 0}
                seq[0] += 1
                if extra:
                    h.update(extra)
                return encode_frame({"head": h, "body": body or {}})

            await ws.send(frame(CMD_HEARTBEAT))
            await ws.send(frame(CMD_LOGIN, {"identifier": ident, "usersig": sig},
                                {"State": "Online", "is_web_uniapp": 0, "InstType": 0,
                                 "CustomInfo": ""}))
            end = time.time() + 12
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    break
                try:
                    o = json.loads(decode_frame(raw))
                except Exception:
                    continue
                if o.get("head", {}).get("servcmd") == CMD_LOGIN:
                    body = o.get("body") or {}
                    result = {"ok": bool(body.get("A2Key")),
                              "errorCode": body.get("ErrorCode"),
                              "errorInfo": body.get("ErrorInfo"),
                              "tinyId": body.get("TinyId")}
                    break
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return jsonify(result)


# ===================== 消息 API =====================
@app.route("/api/messages")
async def api_messages():
    """查询本地已同步的消息。

    两种读法：
      * **最新优先**（默认，适合 3000+/天 的房间）：`order=desc`，取最新的 size 条，
        同时返回 `oldest_id`，下次带 `before_id=<oldest_id>` 往历史翻页
        （前端滚到顶部自动加载）。返回的 messages 已反转为时间正序，直接渲染。
      * **按天通读**：`date=YYYY-MM-DD&order=asc`，从当天最早开始读。
    """
    c = cfg()
    room_id = int(request.args.get("room_id") or c.get("room_id") or 0)
    kind = request.args.get("type", "all")
    date = request.args.get("date", "")
    keyword = request.args.get("keyword", "").strip()
    user_id = int(request.args.get("user_id") or 0)
    days = int(request.args.get("days") or 0)
    size = min(int(request.args.get("size") or 800), 20000)
    before_id = int(request.args.get("before_id") or 0)
    order = (request.args.get("order") or "").lower()
    if order not in ("asc", "desc"):
        order = "asc" if date else "desc"
    start_ts = end_ts = 0
    if days:
        # 从「今天 00:00」往前数 days 天（以前写成 now+86400-86400=now，
        # 导致 days=1 只匹配未来时间戳，今天一条也查不到）
        t0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        day0 = int(t0.timestamp())
        end_ts = day0 + 86399
        start_ts = day0 - (max(1, days) - 1) * 86400

    flt = dict(room_id=room_id or None, date=date, start_ts=start_ts, end_ts=end_ts,
               keyword=keyword, user_id=user_id, only_teacher=(kind == "teacher"),
               only_reply=(kind in ("reply", "private")))
    with _db_lock:
        msgs = db.query_messages(conn, size=size, before_id=before_id, order=order, **flt)
        total = db.count_filtered(conn, **flt)
    has_more = len(msgs) == size
    if order == "desc":
        msgs.reverse()                      # 倒序取、正序给，前端直接 append
    out = {"messages": msgs, "count": len(msgs), "total": total,
           "has_more": has_more, "order": order, "date": date,
           "oldest_id": (msgs[0].get("id") if msgs else 0),
           "newest_id": (msgs[-1].get("id") if msgs else 0)}
    # 选了某天但没数据（比如过了午夜选“今天”、或选了非交易日）→ 告诉前端最近有数据的那天
    if date and not msgs:
        with _db_lock:
            row = conn.execute(
                "SELECT MAX(substr(msg_date,1,10)) d FROM messages "
                "WHERE room_id=? AND substr(msg_date,1,10)<=?",
                (room_id or 0, date)).fetchone()
        out["nearest_date"] = (row and row["d"]) or ""
    # 请求日志（排查“看到的是哪天”这类问题只能靠它）
    print(f"[消息] date={date or '-'} days={days or '-'} order={order} "
          f"before_id={before_id or '-'} → {len(msgs)} 条"
          + (f"（{msgs[0]['msg_date'][:10]}~{msgs[-1]['msg_date'][:10]}）" if msgs else ""),
          flush=True)
    return jsonify(out)


@app.route("/api/messages/search")
async def api_search():
    c = cfg()
    keyword = request.args.get("keyword", "").strip()
    user_id = int(request.args.get("user_id") or 0)
    if not keyword and not user_id:
        return jsonify({"error": "需要 keyword 或 user_id"}), 400
    room_id = int(request.args.get("room_id") or c.get("room_id") or 0)
    size = min(int(request.args.get("size") or 300), 2000)
    with _db_lock:
        msgs = db.search_messages(conn, keyword or "", room_id or None, user_id=user_id,
                                  start_date=request.args.get("start_date", ""),
                                  end_date=request.args.get("end_date", ""), size=size)
    return jsonify({"total": len(msgs), "messages": msgs})


@app.route("/api/stats")
async def api_stats():
    c = cfg()
    with _db_lock:
        st = db.stats(conn, int(c.get("room_id") or 0) or None)
    return jsonify(st)


@app.route("/api/users/batch")
async def api_users_batch():
    uids = [int(x) for x in (request.args.get("uids") or "").split(",") if x.strip().isdigit()]
    with _db_lock:
        out = db.users_batch(conn, uids[:500])
    return jsonify(out)


@app.route("/api/users/<int:user_id>")
async def api_user(user_id: int):
    with _db_lock:
        return jsonify(db.get_user_profile(conn, user_id))


# 同步启动的原子占位：两个 POST 在几十毫秒内同时到达时都读到 running=False，
# 会双开同步线程重复拉同样的页 —— 检查+占位必须在同一把锁里完成。
_sync_claim_lock = threading.Lock()


@app.route("/api/sync", methods=["POST"])
async def api_sync():
    """同步历史。

    body:
      full         true=翻到最早（全量）；false=只拉最新几页（默认）
      pages        增量模式下最多拉几页（默认 5）
      page_size    每页条数，实测无上限，默认 1000
      max_messages 本次最多取多少条，防止失控（默认 200000）
      since_date   只同步不早于该日期 YYYY-MM-DD 的消息
      source_type  1=房间消息流（默认），4=老师私聊回复流
    """
    data = await request.get_json(force=True) or {}
    pages = int(data.get("pages") or 5)
    source = int(data.get("source_type") or api.SRC_ROOM)
    full = bool(data.get("full"))
    page_size = max(15, min(int(data.get("page_size") or 1000), 2000))
    max_messages = max(100, int(data.get("max_messages") or 200000))
    since_date = (data.get("since_date") or "").strip()
    c = cfg()
    if not c.get("room_id"):
        return jsonify({"error": "尚未确定房间，请先在设置里探测 centraltoken"}), 400
    # 已确认失效且还在退避窗口内 → 直接告错，不浪费一次请求
    if _sync_state["auth_failed"] and time.time() < _sync_state["next_try"]:
        return jsonify({"error": "登录失效，请更新 centraltoken"}), 401
    with _sync_claim_lock:
        if _sync_state["running"]:
            return jsonify({"error": "已有一个同步任务在跑，稍等", "state": dict(_sync_state)}), 409
        _sync_state["running"] = True          # 先占位再开线程，并发点击不会双开

    def _bg():
        try:
            r = sync_history(pages=pages, source_type=source, full=full,
                             page_size=page_size, max_messages=max_messages,
                             since_date=since_date)
            broadcast({"type": "synced", **r})
        except Exception as e:                                    # noqa: BLE001
            _sync_state.update(running=False, done=True, detail=f"异常：{e}")
            _sync_progress()
            print(f"[同步] 异常：{e}")
        finally:
            _sync_state["running"] = False
            _sync_progress()

    t = threading.Thread(target=_bg, daemon=True)
    try:
        t.start()
    except Exception:                       # 线程都起不来：释放占位，别把同步永久锁死
        _sync_state["running"] = False
        raise
    return jsonify({"status": "started", "pages": pages})


@app.route("/api/sync/state")
async def api_sync_state():
    """同步进度快照（不碰数据库）。WS 断连时前端靠轮询它保住进度显示。"""
    return jsonify(dict(_sync_state))


@app.route("/api/article/<int:article_id>")
async def api_article(article_id: int):
    """研选文章正文（msgType=2 卡片点开后的内容，网页版接口）。"""
    c = cfg()
    teacher_id = int(c.get("teacher_id") or 0)
    client = _client()
    try:
        d = await asyncio.to_thread(client.get_article, article_id, teacher_id)
    except AuthExpired:
        return jsonify({"error": "登录失效，请更新 centraltoken"}), 401
    except NiuLaiError as e:
        return jsonify({"error": str(e)}), 500
    content = d.get("articleContent") or ""
    pdf_path = ""
    if d.get("articleType") == 2:
        # PDF 型文章：content 是 {filePath} JSON，正文走 preview 接口换 URL
        try:
            pdf_path = json.loads(content).get("filePath", "")
        except Exception:                              # noqa: BLE001
            pdf_path = ""
        if pdf_path:
            try:
                pdf_path = await asyncio.to_thread(client.get_article_preview, pdf_path)
            except NiuLaiError:
                pdf_path = ""
    return jsonify({"articleId": d.get("articleId", article_id),
                    "title": d.get("articleTitle", ""),
                    "content": content if d.get("articleType") != 2 else "",
                    "pdfUrl": pdf_path,
                    "createTime": d.get("createTime", ""),
                    "feeStatus": d.get("feeStatus", 0),
                    "articleType": d.get("articleType", 0)})


@app.route("/api/articles")
async def api_articles():
    """全部研选文章（网页版列表接口，?page= 分页，15/页）。"""
    c = cfg()
    teacher_id = int(c.get("teacher_id") or 0)
    page = max(1, int(request.args.get("page") or 1))
    client = _client()
    try:
        d = await asyncio.to_thread(client.get_articles, teacher_id, page)
    except AuthExpired:
        return jsonify({"error": "登录失效，请重新登录"}), 401
    except NiuLaiError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"page": page, "total": d.get("total", 0),
                    "hasNextPage": bool(d.get("hasNextPage")),
                    "articles": d.get("list") or []})


@app.route("/api/pinned")
async def api_pinned():
    """房间置顶消息（pinnedListById）。拿不到就返回空，不打断主界面。"""
    c = cfg()
    room_id = int(c.get("room_id") or 0)
    if not room_id:
        return jsonify({"pins": []})
    client = _client()
    try:
        pins = await asyncio.to_thread(client.get_pinned, room_id)
    except (AuthExpired, NiuLaiError):
        return jsonify({"pins": []})
    return jsonify({"pins": pins or []})


@app.route("/api/videos")
async def api_videos():
    """视频栏目 + 回放列表（网页版「视频」标签的取数）。
    ?column_id= 省略时用第一个栏目；page 默认 1。"""
    c = cfg()
    teacher_id = int(c.get("teacher_id") or 0)
    client = _client()
    column_id = int(request.args.get("column_id") or 0)
    page = max(1, int(request.args.get("page") or 1))
    try:
        columns = await asyncio.to_thread(client.get_video_columns, teacher_id)
        if not column_id and columns:
            column_id = int(columns[0].get("columnId") or 0)
        playbacks = (await asyncio.to_thread(client.get_video_playbacks,
                                             teacher_id, column_id, page)
                     if column_id else [])
    except AuthExpired:
        return jsonify({"error": "登录失效，请更新 centraltoken"}), 401
    except NiuLaiError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"columns": columns, "columnId": column_id,
                    "page": page, "playbacks": playbacks})


@app.route("/api/video_play/<int:video_id>")
async def api_video_play(video_id: int):
    """回放播放信息：m3u8 直链（免签、CORS 全开，hls.js 可直接拉流）。"""
    client = _client()
    try:
        d = await asyncio.to_thread(client.get_video_play, video_id)
    except AuthExpired:
        return jsonify({"error": "登录失效，请更新 centraltoken"}), 401
    except NiuLaiError as e:
        return jsonify({"error": str(e)}), 500
    if not d.get("adaptiveUrl"):
        return jsonify({"error": f"该视频没有可播地址（{d.get('title') or video_id}）"}), 404
    return jsonify({"id": video_id, "title": d.get("title", ""),
                    "m3u8": d["adaptiveUrl"],
                    "webUrl": f"https://client.zx093.com/webktc/tougu/index.html"
                              f"?path=/video&id={video_id}&teacherId={d.get('teacherId', '')}"})


@app.route("/api/send", methods=["POST"])
async def api_send():
    data = await request.get_json(force=True) or {}
    c = cfg()
    room_id = int(c.get("room_id") or 0)
    if not room_id:
        return jsonify({"error": "尚未确定房间"}), 400
    client = _client()
    try:
        if data.get("image"):
            img = data["image"]
            res = client.send_image(room_id, img.get("imgUrl", ""),
                                    int(img.get("width") or 0), int(img.get("height") or 0))
        else:
            text = (data.get("text") or "").strip()
            if not text:
                return jsonify({"error": "消息不能为空"}), 400
            if len(text) > 2000:
                return jsonify({"error": "消息过长（上限 2000 字）"}), 400
            res = client.send_text(room_id, text)
        return jsonify({"status": "ok", "data": res})
    except AuthExpired:
        return jsonify({"error": "登录失效，请更新 centraltoken"}), 401
    except NiuLaiError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
async def api_upload():
    """上传图片（原始字节），返回 {imgUrl,width,height}。"""
    raw = await request.get_data()
    if not raw:
        return jsonify({"error": "图片数据为空"}), 400
    if len(raw) > 2 * 1024 * 1024:
        return jsonify({"error": "图片超过 2MB 限制"}), 400
    ext = (request.args.get("ext") or "png").lower()
    if ext not in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
        ext = "png"
    try:
        client = _client()
        img_url = client.upload_image(raw, ext)
    except AuthExpired:
        return jsonify({"error": "登录失效，请更新 centraltoken"}), 401
    except NiuLaiError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"上传失败：{e}"}), 500
    w, h = api.image_size(raw)
    # 上传成功即本地缓存原图，发送后可立即显示
    try:
        name = _cache_name(api.media_url(img_url), "image")
        with open(os.path.join(IMAGE_DIR, name), "wb") as f:
            f.write(raw)
        with _db_lock:
            db.save_media(conn, f"image:{api.media_url(img_url)}", api.media_url(img_url),
                          name, "image", len(raw))
    except Exception:
        pass
    return jsonify({"imgUrl": img_url, "width": w, "height": h})


# ===================== 媒体服务 =====================
@app.route("/api/media")
async def api_media():
    """按远端 URL 代理并缓存图片/头像：/api/media?u=<urlencoded>&kind=image|avatar"""
    url = request.args.get("u", "")
    kind = request.args.get("kind", "image")
    if kind not in ("image", "avatar"):
        kind = "image"
    if not url:
        abort(404)
    # cache_media 是同步的（内部 requests 下载，最长 30s 超时）——
    # 直接在 async 处理函数里调会把事件循环卡住，整站跟着卡。丢线程池里跑。
    name = await asyncio.to_thread(cache_media, url, kind)
    if not name:
        abort(404)
    resp = await send_file(os.path.join(_media_dir(kind), name))
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/api/media/local/<kind>/<path:name>")
async def api_media_local(kind: str, name: str):
    d = _media_dir(kind)
    if not os.path.exists(os.path.join(d, name)):
        abort(404)
    resp = await send_file(os.path.join(d, name))
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/api/media/status")
async def api_media_status():
    """媒体本地化进度：还差几张才能纯离线看历史。"""
    return jsonify(await asyncio.to_thread(media_status))


@app.route("/api/media/sync", methods=["POST"])
async def api_media_sync():
    """补齐媒体：把库里引用到的图片与头像全部下载到 data/cache/。

    body: {limit: 0}   limit>0 时只补前 N 个（调试用）
    """
    data = await request.get_json(force=True, silent=True) or {}
    limit = max(0, int(data.get("limit") or 0))
    if _media_state["running"]:
        return jsonify({"error": "已有一个媒体补齐任务在跑",
                        "state": dict(_media_state)}), 409
    threading.Thread(target=media_sync, kwargs={"limit": limit}, daemon=True).start()
    return jsonify({"status": "started", "limit": limit})


# ===================== 备份 =====================
@app.route("/api/backup", methods=["POST"])
async def api_backup():
    """备份 SQLite（图片目录 data/cache/ 不在备份范围内）。"""
    import shutil
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"niulai_{ts}.db")
    try:
        with _db_lock:
            conn.commit()
        shutil.copy2(DB_PATH, path)
        return jsonify({"status": "ok", "file": os.path.basename(path),
                        "size": os.path.getsize(path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===================== WebSocket =====================
@app.websocket("/ws")
async def ws_handler():
    with _im_status_lock:
        ims = dict(_im_state)
    await websocket.send(json.dumps({"type": "status", "im_state": ims["state"],
                                     "im_detail": ims["detail"]}, ensure_ascii=False))
    ws = websocket._get_current_object()
    with _ws_lock:
        _ws_clients.add(ws)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive(), timeout=120)
                if isinstance(msg, str) and msg == "ping":
                    await websocket.send('{"type":"pong"}')
            except asyncio.TimeoutError:
                break
    except Exception:
        pass
    finally:
        with _ws_lock:
            _ws_clients.discard(ws)


# ===================== 启动 =====================
@app.before_serving
async def startup():
    global _loop
    _loop = asyncio.get_running_loop()
    seed_from_env()
    seed_token_log()
    asyncio.create_task(_broadcast_loop())
    threading.Thread(target=_prefetch_worker, daemon=True).start()
    threading.Thread(target=_im_supervisor, daemon=True).start()
    threading.Thread(target=_sync_worker, daemon=True).start()
    _sync_wake.set()        # 启动立刻拉一轮最新消息（原「同步最新」按钮的活，现在自动做）
    if MEDIA_AUTOFETCH:
        threading.Thread(target=_media_autofetch, daemon=True).start()
    c = cfg()
    print("=" * 56)
    print("约牛聊天室 Web 服务")
    print(f"  房间 room_id={c.get('room_id') or '(未设置)'}  teacher_id={c.get('teacher_id') or '(未设置)'}")
    print(f"  鉴权：{'已配置 centraltoken' if c.get('centraltoken') else '未配置（只能浏览本地库）'}")
    print(f"  IM  ：{'已配置 userSig' if c.get('im_user_sig') else '未配置（无实时消息）'}")
    print("=" * 56)
