# -*- coding: utf-8 -*-
"""
端到端自检：IM 推送 → 入库 → WebSocket 广播 → 浏览器结构
=======================================================
用抓包里的真实 msg_push 帧驱动，不联网。
起一个内存里的 Quart 服务，连上 /ws，注入推送，校验收到的广播。
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NIULAI_TEST", "1")

import db  # noqa: E402
import app as A  # noqa: E402
from niulai_im import parse_push  # noqa: E402

CAP = os.path.expanduser("~/.zcode/workspace/default/data/capture/ws_decoded_v3.txt")


def real_push_frames():
    out = []
    for line in open(CAP, encoding="utf-8"):
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


async def main():
    # 用测试库，避免污染正式库
    test_db = os.path.join(A.DATA_DIR, "test_e2e.db")
    if os.path.exists(test_db):
        os.remove(test_db)
    A.conn = db.connect(test_db)
    db.save_room(A.conn, {"id": 298, "name": "测试房间", "imGroupId": "@TGS#_@TGS#x"})

    frames = real_push_frames()
    msgs = [m for f in frames for m in parse_push(f)[0]]
    print(f"驱动数据：{len(frames)} 个推送帧 / {len(msgs)} 条消息")

    received = []

    async def ws_client():
        await asyncio.sleep(0.3)
        import websockets
        async with websockets.connect("ws://127.0.0.1:5199/ws") as ws:
            # 第一条是握手状态
            first = json.loads(await ws.recv())
            print("WS 首帧:", first)
            # 注入真实推送（等价于 IM 线程收到消息）
            for f in frames:
                A._im_on_message(None) if False else None
                fm, _ = parse_push(f)
                for mm in fm:
                    A.ingest(298, [mm], origin="im")
            # 收广播
            end = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                except asyncio.TimeoutError:
                    break
                received.append(json.loads(raw))

    task = asyncio.create_task(A.app.run_task("127.0.0.1", 5199))
    await asyncio.sleep(1.2)
    A._loop = asyncio.get_running_loop()
    asyncio.create_task(A._broadcast_loop())
    await ws_client()
    task.cancel()

    msgs_bcast = [r for r in received if r.get("type") == "message"]
    print(f"收到广播 {len(received)} 条，其中 message {len(msgs_bcast)} 条")
    if msgs_bcast:
        sample = msgs_bcast[0]["data"]
        need = ["id", "msg_type", "msg_content", "msg_date", "user_id", "nick_name",
                "yn_no", "user_type", "avatar_url", "ts", "image", "quote", "images"]
        missing = [k for k in need if k not in sample]
        print("广播字段完整:", not missing, "缺失:", missing)
        print("样本:", json.dumps({k: sample[k] for k in ("id", "nick_name", "yn_no",
                                                          "msg_type", "msg_date")},
                                  ensure_ascii=False))
    with A._db_lock:
        total = db.count_messages(A.conn, 298)
        st = db.stats(A.conn, 298)
    print(f"入库总数 {total} 条（去重后），老师 {st['teacher']} 条，用户 {st['users']} 人")
    os.remove(test_db)
    for suffix in ("-wal", "-shm"):
        p = test_db + suffix
        if os.path.exists(p):
            os.remove(p)
    ok = msgs_bcast and len(msgs_bcast) == total
    print("端到端自检:", "✅ 通过" if ok else "❌ 失败")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
