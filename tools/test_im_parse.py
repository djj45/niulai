# -*- coding: utf-8 -*-
"""用抓包里的真实 msg_push 帧验证 IM 解析 + 入库全链路。"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from niulai_im import parse_push, normalize_group_message  # noqa: E402

CAP = os.path.expanduser("~/.zcode/workspace/default/data/capture/ws_decoded_v3.txt")


def load_pushes(path):
    out = []
    for line in open(path, encoding="utf-8"):
        m = re.match(r"\[\d+\]\[S->C\] (\{.*)", line.strip())
        if not m:
            continue
        try:
            o = json.loads(m.group(1))
        except Exception:
            continue
        if o.get("head", {}).get("servcmd") == "im_open_push.msg_push":
            out.append(o["body"])
    return out


pushes = load_pushes(CAP)
print(f"抓到 {len(pushes)} 个 msg_push 帧")
msgs, events = [], []
for p in pushes:
    m, e = parse_push(p)
    msgs += m
    events += e
print(f"解析出 {len(msgs)} 条聊天消息 / {len(events)} 个系统事件")

by_type = {}
for m in msgs:
    by_type.setdefault(m["msg_type"], []).append(m)
for t, lst in by_type.items():
    s = lst[0]
    print(f"  msg_type={t} n={len(lst)} 例: id={s['id']} seq={s['im_msg_seq']} "
          f"{s['nick_name']}({s['yn_no']}) user_type={s['user_type']} "
          f"内容={str(s['msg_content'])[:40]}")
quoted = [m for m in msgs if m["quote_id"]]
print(f"带引用的消息: {len(quoted)} 条" +
      (f" → quote_id={quoted[0]['quote_id']} 原昵称="
       f"{json.loads(quoted[0]['quote_json']).get('nickName')}" if quoted else ""))

# 入库验证（写入临时库，不污染正式库）
tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "test_im.db")
os.makedirs(os.path.dirname(tmp), exist_ok=True)
for p in (tmp, tmp + "-wal", tmp + "-shm"):
    if os.path.exists(p):
        os.remove(p)
conn = db.connect(tmp)
added = db.upsert_messages(conn, 298, msgs, origin="im")
again = db.upsert_messages(conn, 298, msgs, origin="im")
print(f"入库：新增 {added} 条，重复写入再增 {again} 条（应为 0，验证幂等）")
rows = db.query_messages(conn, 298, size=3)
print("查询样本：", [(r["nick_name"], r["msg_date"], str(r["msg_content"])[:24]) for r in rows])
prof = db.get_user_profile(conn, msgs[0]["user_id"])
print(f"用户档案：{prof['nick_name']} 共 {prof['msg_count']} 条，曾用名 {len(prof['nicknames'])} 个")
conn.close()
for p in (tmp, tmp + "-wal", tmp + "-shm"):
    if os.path.exists(p):
        os.remove(p)
print("OK")
