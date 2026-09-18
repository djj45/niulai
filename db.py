# -*- coding: utf-8 -*-
"""
约牛聊天室本地存储（SQLite）
===========================
- `messages`      消息（REST 历史 + IM 实时，同一 pk 去重）
- `users`         用户当前昵称/头像
- `user_nicknames` 曾用名（昵称变更历史）
- `user_avatars`   头像历史（含本地缓存文件名）
- `media`          图片本地缓存索引
- `rooms`          房间信息
- `settings`       键值配置（centraltoken / userSig / 房间选择等）
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "niulai.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    pk            TEXT PRIMARY KEY,
    id            INTEGER DEFAULT 0,
    room_id       INTEGER DEFAULT 0,
    msg_type      INTEGER DEFAULT 0,
    msg_content   TEXT,
    msg_date      TEXT,
    ts            INTEGER DEFAULT 0,
    user_id       INTEGER DEFAULT 0,
    nick_name     TEXT,
    yn_no         TEXT,
    user_type     INTEGER DEFAULT 1,
    avatar_url    TEXT,
    real_name     TEXT,
    certificate_num TEXT,
    quote_id      INTEGER DEFAULT 0,
    quote_json    TEXT,
    private_message_flag INTEGER DEFAULT 0,
    teacher_id    INTEGER DEFAULT 0,
    to_user_id    INTEGER DEFAULT 0,
    fee_status    INTEGER DEFAULT 0,
    vip_user      INTEGER DEFAULT 0,
    source_type   INTEGER DEFAULT 0,
    audit_status  INTEGER DEFAULT 1,
    im_group_id   TEXT,
    im_msg_seq    INTEGER DEFAULT 0,
    origin        TEXT,
    raw           TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_room_ts   ON messages(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_ts        ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_msg_user      ON messages(user_id);
CREATE INDEX IF NOT EXISTS idx_msg_type      ON messages(msg_type);
CREATE INDEX IF NOT EXISTS idx_msg_teacher   ON messages(user_type);
CREATE INDEX IF NOT EXISTS idx_msg_seq       ON messages(room_id, im_msg_seq);

CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    nick_name  TEXT,
    yn_no      TEXT,
    avatar_url TEXT,
    user_type  INTEGER DEFAULT 1,
    first_seen TEXT,
    last_seen  TEXT
);
CREATE TABLE IF NOT EXISTS user_nicknames (
    user_id   INTEGER,
    nick_name TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    PRIMARY KEY (user_id, nick_name)
);
CREATE TABLE IF NOT EXISTS user_avatars (
    user_id     INTEGER,
    avatar_url  TEXT,
    avatar_local TEXT,
    first_seen  TEXT,
    PRIMARY KEY (user_id, avatar_url)
);
CREATE TABLE IF NOT EXISTS media (
    key        TEXT PRIMARY KEY,
    url        TEXT,
    local      TEXT,
    kind       TEXT,
    size       INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS rooms (
    room_id      INTEGER PRIMARY KEY,
    teacher_id   INTEGER,
    name         TEXT,
    teacher_name TEXT,
    real_name    TEXT,
    im_group_id  TEXT,
    certificate_num TEXT,
    enable_dm    INTEGER DEFAULT 0,
    visible_days INTEGER DEFAULT 0,
    raw          TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    room_id     INTEGER,
    source_type INTEGER,
    cursor_id   TEXT,
    newest_id   INTEGER DEFAULT 0,
    updated_at  TEXT,
    PRIMARY KEY (room_id, source_type)
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")   # 大量写入时不必每条都 fsync
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-20000")     # 20MB 页缓存
    conn.commit()
    return conn


# ===================== 设置项 =====================
def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_setting(conn, key: str, value: str):
    conn.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    conn.commit()


def get_settings(conn, keys) -> dict:
    out = {}
    for k in keys:
        out[k] = get_setting(conn, k)
    return out


# ===================== 房间 =====================
def save_room(conn, room: dict) -> int:
    conn.execute("""
        INSERT INTO rooms(room_id, teacher_id, name, teacher_name, real_name, im_group_id,
                          certificate_num, enable_dm, visible_days, raw, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(room_id) DO UPDATE SET
          teacher_id=excluded.teacher_id, name=excluded.name, teacher_name=excluded.teacher_name,
          real_name=excluded.real_name, im_group_id=excluded.im_group_id,
          certificate_num=excluded.certificate_num, enable_dm=excluded.enable_dm,
          visible_days=excluded.visible_days, raw=excluded.raw, updated_at=excluded.updated_at
    """, (room.get("id"), room.get("teacherId"), room.get("name"), room.get("teacherName"),
          room.get("realName"), room.get("imGroupId"), room.get("certificateNum"),
          1 if str(room.get("enableDm", "0")) in ("1", "true", "True") else 0,
          room.get("visibleDays", 0), json.dumps(room, ensure_ascii=False), _now()))
    conn.commit()
    return room.get("id")


def get_room(conn, room_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM rooms WHERE room_id=?", (room_id,)).fetchone()
    return dict(row) if row else None


def list_rooms(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM rooms ORDER BY updated_at DESC")]


_INSERT_MSG = """
INSERT INTO messages(pk, id, room_id, msg_type, msg_content, msg_date, ts, user_id,
    nick_name, yn_no, user_type, avatar_url, real_name, certificate_num, quote_id,
    quote_json, private_message_flag, teacher_id, to_user_id, fee_status, vip_user,
    source_type, audit_status, im_group_id, im_msg_seq, origin, raw, created_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""
_UPDATE_MSG = """
UPDATE messages SET msg_content=?, audit_status=?, fee_status=?, im_msg_seq=?,
    avatar_url=?, nick_name=?, private_message_flag=?, vip_user=?, to_user_id=?
WHERE pk=?
"""
_UPSERT_USER = """
INSERT INTO users(user_id, nick_name, yn_no, avatar_url, user_type, first_seen, last_seen)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(user_id) DO UPDATE SET
    nick_name=excluded.nick_name, yn_no=excluded.yn_no, avatar_url=excluded.avatar_url,
    user_type=excluded.user_type, last_seen=excluded.last_seen
"""
_UPSERT_NICK = """
INSERT INTO user_nicknames(user_id, nick_name, first_seen, last_seen) VALUES(?,?,?,?)
ON CONFLICT(user_id, nick_name) DO UPDATE SET last_seen=excluded.last_seen
"""
_UPSERT_AVATAR = """
INSERT INTO user_avatars(user_id, avatar_url, avatar_local, first_seen) VALUES(?,?,?,?)
ON CONFLICT(user_id, avatar_url) DO UPDATE SET first_seen=user_avatars.first_seen
"""


def _msg_row(room_id: int, m: dict, pk: str, mid: int, seq: int) -> tuple:
    quote = m.get("quoteContent") or m.get("quote")
    quote_json = m.get("quote_json")
    if not quote_json and quote:
        quote_json = json.dumps(quote, ensure_ascii=False)
    # 引用目标 id：REST 记录里带在 quoteContent.id 下，以前只读 m['quote_id']
    # 导致引用跳转失效（quote 快照存住了，但 quote_id 总是 0）
    quote_id = int(m.get("quote_id")
                   or (quote.get("id") if isinstance(quote, dict) else 0) or 0)
    return (pk, mid, room_id, int(m.get("msg_type") or m.get("msgType") or 0),
            m.get("msg_content") if m.get("msg_content") is not None else m.get("msgContent"),
            m.get("msg_date") or m.get("msgDate") or "", _ts_of(m),
            int(m.get("user_id") or m.get("userId") or 0),
            m.get("nick_name") or m.get("nickName") or "",
            m.get("yn_no") or m.get("ynNo") or "",
            int(m.get("user_type") or m.get("userType") or 1),
            m.get("avatar_url") or m.get("avatarUrl") or "",
            m.get("real_name") or m.get("realName"),
            m.get("certificate_num") or m.get("certificateNum"),
            quote_id, quote_json,
            1 if (m.get("private_message_flag") or m.get("privateMessageFlag")) else 0,
            int(m.get("teacher_id") or m.get("teacherId") or 0),
            int(m.get("to_user_id") or m.get("toUserId") or 0),
            int(m.get("fee_status") or m.get("feeStatus") or 0),
            1 if (m.get("vip_user") or m.get("vipUser")) else 0,
            int(m.get("source_type") or m.get("sourceType") or 0),
            int(m.get("audit_status") or m.get("auditStatus") or 1),
            m.get("im_group_id") or m.get("imGroupId") or "", seq, "",
            m.get("raw") if isinstance(m.get("raw"), str) else json.dumps(m, ensure_ascii=False),
            _now())


# ===================== 消息 =====================
def _pk(room_id, mid, seq) -> str:
    if mid:
        return f"{room_id}:{mid}"
    if seq:
        return f"{room_id}:seq:{seq}"
    return f"{room_id}:{int(time.time()*1000)}:{os.urandom(4).hex()}"


def message_exists(conn, room_id: int, m: dict) -> bool:
    """该消息是否已在库里（按 pk 判定）。"""
    mid = int(m.get("id") or 0)
    seq = int(m.get("im_msg_seq") or m.get("imMsgSeq") or 0)
    return conn.execute("SELECT 1 FROM messages WHERE pk=?", (_pk(room_id, mid, seq),)).fetchone() is not None


def _ts_of(msg: dict) -> int:
    if msg.get("ts"):
        return int(msg["ts"])
    d = msg.get("msg_date") or msg.get("msgDate") or ""
    try:
        return int(datetime.strptime(d.strip(), "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return 0


def upsert_messages(conn, room_id: int, msgs: list, origin: str = "rest") -> int:
    """批量写入消息（每批一次 SELECT + executemany，不再逐条查/写）。

    以前是逐条 SELECT+INSERT+3 条用户 SQL，1000 条消息要跑 ~5000 条 SQL，
    占锁太久会把 asyncio 事件循环一起冻住（表现为网页要 20-30 秒才响应）。
    现在一整批只跑 6 条语句。

    返回新增条数（已存在的只刷新可变字段）。
    """
    if not msgs:
        return 0
    # 同一批里可能有重复（腾讯云 IM 同一串消息会推两遍）→ 按 pk 去重，保留最后一条
    by_pk: dict = {}
    for m in msgs:
        mid = int(m.get("id") or 0)
        seq = int(m.get("im_msg_seq") or m.get("imMsgSeq") or 0)
        pk = _pk(room_id, mid, seq)
        if pk in by_pk:
            prev = by_pk[pk]
            if not (m.get("msg_content") or m.get("msgContent")) and \
               (prev.get("msg_content") or prev.get("msgContent")):
                continue                       # 保留信息更全的那条
        by_pk[pk] = m
    prepared = [(_pk(room_id, int(m.get("id") or 0), int(m.get("im_msg_seq") or m.get("imMsgSeq") or 0)),
                 int(m.get("id") or 0), int(m.get("im_msg_seq") or m.get("imMsgSeq") or 0), m)
                for m in by_pk.values()]

    pks = [p[0] for p in prepared]
    existing = set()
    for i in range(0, len(pks), 900):                       # 避开 SQLite 变量数上限
        chunk = pks[i:i + 900]
        ph = ",".join("?" * len(chunk))
        existing |= {r[0] for r in conn.execute(
            f"SELECT pk FROM messages WHERE pk IN ({ph})", chunk)}

    new_rows = [p for p in prepared if p[0] not in existing]
    upd_rows = [p for p in prepared if p[0] in existing]
    if new_rows:
        conn.executemany(_INSERT_MSG,
                         [_msg_row(room_id, m, pk, mid, seq)
                          for pk, mid, seq, m in new_rows])
    if upd_rows:
        conn.executemany(_UPDATE_MSG, [
            (m.get("msg_content") if m.get("msg_content") is not None else m.get("msgContent"),
             int(m.get("audit_status") if m.get("audit_status") is not None
                 else (m.get("auditStatus") if m.get("auditStatus") is not None else 1)),
             int(m.get("fee_status") or m.get("feeStatus") or 0),
             seq,
             m.get("avatar_url") or m.get("avatarUrl") or "",
             m.get("nick_name") or m.get("nickName") or "",
             1 if (m.get("private_message_flag") or m.get("privateMessageFlag")) else 0,
             1 if (m.get("vip_user") or m.get("vipUser")) else 0,
             int(m.get("to_user_id") or m.get("toUserId") or 0), pk)
            for pk, mid, seq, m in upd_rows])

    # 用户信息：每批只写「出现过的用户」，而不是每条消息写一遍
    users = {}
    for _, _, _, m in prepared:
        uid = int(m.get("user_id") or m.get("userId") or 0)
        if not uid:
            continue
        users[uid] = (m.get("nick_name") or m.get("nickName") or "",
                      m.get("avatar_url") or m.get("avatarUrl") or "",
                      m.get("yn_no") or m.get("ynNo") or "",
                      int(m.get("user_type") or m.get("userType") or 1))
    if users:
        now = _now()
        conn.executemany(_UPSERT_USER,
                         [(uid, n, y, a, t, now, now) for uid, (n, a, y, t) in users.items()])
        conn.executemany(_UPSERT_NICK,
                         [(uid, n, now, now) for uid, (n, a, y, t) in users.items() if n])
        conn.executemany(_UPSERT_AVATAR,
                         [(uid, a, "", now) for uid, (n, a, y, t) in users.items() if a])
    return len(new_rows)


def upsert_message(conn, room_id: int, m: dict, origin: str = "rest") -> bool:
    """写入单条（内部走批量实现）。返回是否新增。"""
    return upsert_messages(conn, room_id, [m], origin) > 0


def _upsert_single_legacy(conn, room_id: int, m: dict, origin: str = "rest") -> bool:
    """旧实现，保留作为参考：逐条 SELECT + INSERT，慢。"""
    mid = int(m.get("id") or 0)
    seq = int(m.get("im_msg_seq") or m.get("imMsgSeq") or 0)
    pk = _pk(room_id, mid, seq)
    exists = conn.execute("SELECT 1 FROM messages WHERE pk=?", (pk,)).fetchone()
    quote_json = m.get("quote_json")
    if not quote_json and m.get("quoteContent"):
        quote_json = json.dumps(m["quoteContent"], ensure_ascii=False)
    conn.execute("""
        INSERT INTO messages(pk, id, room_id, msg_type, msg_content, msg_date, ts, user_id,
            nick_name, yn_no, user_type, avatar_url, real_name, certificate_num, quote_id,
            quote_json, private_message_flag, teacher_id, to_user_id, fee_status, vip_user,
            source_type, audit_status, im_group_id, im_msg_seq, origin, raw, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pk) DO UPDATE SET
            msg_content=excluded.msg_content, audit_status=excluded.audit_status,
            fee_status=excluded.fee_status, im_msg_seq=excluded.im_msg_seq,
            avatar_url=excluded.avatar_url, nick_name=excluded.nick_name
    """, (pk, mid, room_id, int(m.get("msg_type") or m.get("msgType") or 0),
          m.get("msg_content") if m.get("msg_content") is not None else m.get("msgContent"),
          m.get("msg_date") or m.get("msgDate") or "", _ts_of(m),
          int(m.get("user_id") or m.get("userId") or 0),
          m.get("nick_name") or m.get("nickName") or "",
          m.get("yn_no") or m.get("ynNo") or "",
          int(m.get("user_type") or m.get("userType") or 1),
          m.get("avatar_url") or m.get("avatarUrl") or "",
          m.get("real_name") or m.get("realName"),
          m.get("certificate_num") or m.get("certificateNum"),
          int(m.get("quote_id") or 0), quote_json,
          1 if (m.get("private_message_flag") or m.get("privateMessageFlag")) else 0,
          int(m.get("teacher_id") or m.get("teacherId") or 0),
          int(m.get("to_user_id") or m.get("toUserId") or 0),
          int(m.get("fee_status") or m.get("feeStatus") or 0),
          1 if (m.get("vip_user") or m.get("vipUser")) else 0,
          int(m.get("source_type") or m.get("sourceType") or 0),
          int(m.get("audit_status") or m.get("auditStatus") or 1),
          m.get("im_group_id") or m.get("imGroupId") or "",
          seq, origin,
          m.get("raw") if isinstance(m.get("raw"), str) else json.dumps(m, ensure_ascii=False),
          _now()))
    _touch_user(conn, m)
    return not exists


def _touch_user(conn, m: dict):
    uid = int(m.get("user_id") or m.get("userId") or 0)
    if not uid:
        return
    nick = m.get("nick_name") or m.get("nickName") or ""
    avatar = m.get("avatar_url") or m.get("avatarUrl") or ""
    yn = m.get("yn_no") or m.get("ynNo") or ""
    utype = int(m.get("user_type") or m.get("userType") or 1)
    now = _now()
    conn.execute("""
        INSERT INTO users(user_id, nick_name, yn_no, avatar_url, user_type, first_seen, last_seen)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            nick_name=excluded.nick_name, yn_no=excluded.yn_no,
            avatar_url=excluded.avatar_url, user_type=excluded.user_type,
            last_seen=excluded.last_seen
    """, (uid, nick, yn, avatar, utype, now, now))
    if nick:
        conn.execute("""
            INSERT INTO user_nicknames(user_id, nick_name, first_seen, last_seen)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id, nick_name) DO UPDATE SET last_seen=excluded.last_seen
        """, (uid, nick, now, now))
    if avatar:
        conn.execute("""
            INSERT INTO user_avatars(user_id, avatar_url, avatar_local, first_seen)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id, avatar_url) DO UPDATE SET first_seen=user_avatars.first_seen
        """, (uid, avatar, "", now))


def upsert_messages_legacy(conn, room_id: int, msgs: list, origin: str = "rest") -> int:
    """旧实现，保留作为参考：逐条 upsert，慢但语义相同。"""
    new = 0
    for m in msgs:
        if _upsert_single_legacy(conn, room_id, m, origin):
            new += 1
    conn.commit()
    return new


def newest_id(conn, room_id: int) -> int:
    row = conn.execute("SELECT MAX(id) AS m FROM messages WHERE room_id=?", (room_id,)).fetchone()
    return int(row["m"] or 0)


def oldest_id(conn, room_id: int) -> int:
    row = conn.execute("SELECT MIN(id) AS m FROM messages WHERE room_id=? AND id>0",
                       (room_id,)).fetchone()
    return int(row["m"] or 0)


def count_messages(conn, room_id: int | None = None) -> int:
    if room_id:
        return conn.execute("SELECT COUNT(*) FROM messages WHERE room_id=?",
                            (room_id,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def _msg_to_dict(row) -> dict:
    d = dict(row)
    d["images"] = []
    d["quote"] = None
    if d.get("quote_json"):
        try:
            d["quote"] = json.loads(d["quote_json"])
        except (TypeError, ValueError):
            d["quote"] = None
    if d.get("raw"):
        try:
            raw = json.loads(d["raw"])
            if d["msg_type"] == 1:
                content = d.get("msg_content") or ""
                try:
                    obj = json.loads(content)
                    d["image"] = {"url": obj.get("imgUrl", ""), "width": obj.get("width", 0),
                                  "height": obj.get("height", 0)}
                except (TypeError, ValueError):
                    d["image"] = None
            avatar = raw.get("avatarUrl") or d.get("avatar_url") or ""
            d["avatar_url"] = avatar
        except (TypeError, ValueError):
            pass
    return d


def _build_filters(room_id=None, date="", start_ts=0, end_ts=0, keyword="", user_id=0,
                   only_teacher=False, only_reply=False, before_id=0):
    sql, params = "", []
    if room_id:
        sql += " AND room_id=?"
        params.append(room_id)
    if date:
        sql += " AND msg_date LIKE ?"
        params.append(date + "%")
    if start_ts:
        sql += " AND ts>=?"
        params.append(start_ts)
    if end_ts:
        sql += " AND ts<=?"
        params.append(end_ts)
    if keyword:
        sql += " AND msg_content LIKE ?"
        params.append(f"%{keyword}%")
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    if only_teacher:
        sql += " AND user_type=4"
    if only_reply:
        # 老师回复了某个用户 —— 这才是真正的「回复」信号。
        # （旧的 private_message_flag 在 REST 记录里恒为 0，在 IM 推送里全是占位值）
        sql += " AND user_type=4 AND to_user_id>0"
    if before_id:
        sql += " AND id>0 AND id<?"
        params.append(before_id)
    return sql, params


def count_filtered(conn, **kw) -> int:
    sql, params = _build_filters(**{k: v for k, v in kw.items() if k != "before_id"})
    return conn.execute(f"SELECT COUNT(*) FROM messages WHERE 1=1{sql}", params).fetchone()[0]


def query_messages(conn, room_id: int | None = None, date: str = "", start_ts: int = 0,
                   end_ts: int = 0, keyword: str = "", user_id: int = 0,
                   only_teacher: bool = False, only_reply: bool = False,
                   size: int = 500, offset: int = 0, desc: bool = False,
                   before_id: int = 0, order: str = "") -> list:
    """查询消息。

    order="desc"（或 desc=True）→ 按时间倒序取（用于「先看最新，再往上加载」）；
    调用方拿到结果后自行 reverse() 即可得到正序列表。
    before_id 配合倒序使用：只取 id 小于它的，实现向历史翻页。
    """
    sql, params = _build_filters(room_id=room_id, date=date, start_ts=start_ts, end_ts=end_ts,
                                 keyword=keyword, user_id=user_id, only_teacher=only_teacher,
                                 only_reply=only_reply, before_id=before_id)
    descending = desc or order == "desc"
    sql = ("SELECT * FROM messages WHERE 1=1" + sql + " ORDER BY ts "
           + ("DESC" if descending else "ASC")
           + (", im_msg_seq DESC" if descending else ", im_msg_seq ASC"))
    if size:
        sql += " LIMIT ? OFFSET ?"
        params.extend([size, offset])
    return [_msg_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def search_messages(conn, keyword: str, room_id: int | None = None, user_id: int = 0,
                    start_date: str = "", end_date: str = "", size: int = 300) -> list:
    sql = "SELECT * FROM messages WHERE msg_content LIKE ?"
    params: list = [f"%{keyword}%"]
    if room_id:
        sql += " AND room_id=?"
        params.append(room_id)
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    if start_date:
        sql += " AND msg_date>=?"
        params.append(start_date)
    if end_date:
        sql += " AND msg_date<=?"
        params.append(end_date + " 23:59:59")
    sql += " ORDER BY ts ASC LIMIT ?"
    params.append(size)
    return [_msg_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def stats(conn, room_id: int | None = None) -> dict:
    where, params = "", []
    if room_id:
        where = " WHERE room_id=?"
        params = [room_id]
    total = conn.execute(f"SELECT COUNT(*) FROM messages{where}", params).fetchone()[0]
    teacher = conn.execute(
        f"SELECT COUNT(*) FROM messages{where}{' AND' if where else ' WHERE'} user_type=4",
        params).fetchone()[0]
    img = conn.execute(
        f"SELECT COUNT(*) FROM messages{where}{' AND' if where else ' WHERE'} msg_type=1",
        params).fetchone()[0]
    users = conn.execute(f"SELECT COUNT(DISTINCT user_id) FROM messages{where}", params).fetchone()[0]
    first = conn.execute(f"SELECT MIN(msg_date) FROM messages{where}", params).fetchone()[0]
    last = conn.execute(f"SELECT MAX(msg_date) FROM messages{where}", params).fetchone()[0]
    days = conn.execute(
        f"SELECT substr(msg_date,1,10) AS day, COUNT(*) AS cnt, "
        f"SUM(CASE WHEN user_type=4 THEN 1 ELSE 0 END) AS teacher_cnt "
        f"FROM messages {'WHERE room_id=?' if room_id else ''} "
        f"GROUP BY day ORDER BY day DESC LIMIT 60", params).fetchall()
    return {
        "total": total, "teacher": teacher, "images": img, "users": users,
        "first": first or "", "last": last or "",
        "days": [{"date": r["day"], "count": r["cnt"], "teacher": r["teacher_cnt"]} for r in days],
    }


def get_user_profile(conn, user_id: int) -> dict:
    u = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    nicks = [dict(r) for r in conn.execute(
        "SELECT nick_name, first_seen, last_seen FROM user_nicknames WHERE user_id=? "
        "ORDER BY last_seen DESC", (user_id,)).fetchall()]
    avatars = [dict(r) for r in conn.execute(
        "SELECT avatar_url, avatar_local, first_seen FROM user_avatars WHERE user_id=? "
        "ORDER BY first_seen DESC", (user_id,)).fetchall()]
    return {
        "user_id": user_id,
        "nick_name": u["nick_name"] if u else "",
        "yn_no": u["yn_no"] if u else "",
        "avatar_url": u["avatar_url"] if u else "",
        "user_type": u["user_type"] if u else 1,
        "nicknames": nicks,
        "avatars": avatars,
        "msg_count": conn.execute("SELECT COUNT(*) FROM messages WHERE user_id=?",
                                  (user_id,)).fetchone()[0],
    }


def users_batch(conn, uids: list) -> dict:
    if not uids:
        return {}
    ph = ",".join("?" * len(uids))
    out = {}
    for r in conn.execute(
            f"SELECT user_id, nick_name, yn_no, avatar_url, user_type FROM users "
            f"WHERE user_id IN ({ph})", uids):
        out[str(r["user_id"])] = {
            "nick_name": r["nick_name"], "yn_no": r["yn_no"], "avatar_url": r["avatar_url"],
            "user_type": r["user_type"], "nick_count": 0,
        }
    for r in conn.execute(
            f"SELECT user_id, COUNT(*) c FROM user_nicknames WHERE user_id IN ({ph}) "
            f"GROUP BY user_id", uids):
        k = str(r["user_id"])
        if k in out:
            out[k]["nick_count"] = r["c"]
    return out


# ===================== 媒体缓存 =====================
def media_local(conn, key: str) -> str:
    row = conn.execute("SELECT local FROM media WHERE key=?", (key,)).fetchone()
    return row["local"] if row else ""


def save_media(conn, key: str, url: str, local: str, kind: str, size: int = 0):
    conn.execute("INSERT INTO media(key,url,local,kind,size,created_at) VALUES(?,?,?,?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET local=excluded.local, size=excluded.size",
                 (key, url, local, kind, size, _now()))
    conn.commit()


def set_avatar_local(conn, user_id: int, avatar_url: str, local: str):
    conn.execute("UPDATE user_avatars SET avatar_local=? WHERE user_id=? AND avatar_url=?",
                 (local, user_id, avatar_url))
    conn.commit()


def get_sync_state(conn, room_id: int, source_type: int) -> dict:
    row = conn.execute("SELECT * FROM sync_state WHERE room_id=? AND source_type=?",
                       (room_id, source_type)).fetchone()
    return dict(row) if row else {"cursor_id": "", "newest_id": 0}


def set_sync_state(conn, room_id: int, source_type: int, cursor_id: str = "", newest_id: int = 0):
    conn.execute("""
        INSERT INTO sync_state(room_id, source_type, cursor_id, newest_id, updated_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(room_id, source_type) DO UPDATE SET
            cursor_id=excluded.cursor_id, newest_id=MAX(sync_state.newest_id, excluded.newest_id),
            updated_at=excluded.updated_at
    """, (room_id, source_type, cursor_id, newest_id, _now()))
    conn.commit()
