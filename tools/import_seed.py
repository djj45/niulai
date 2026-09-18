# -*- coding: utf-8 -*-
"""
把抓包导出的种子数据导入本地库
==============================
用法：
    uv run python tools/import_seed.py [seed.json]
默认读取 data/seed/capture_seed.json（由 tools/extract_capture.py 生成）。

导入内容：房间信息、历史消息（121 条示例）、centraltoken、
         IM 凭证（sdkAppId / cu_xxx / userSig / imGroupId）、我的资料。
已存在的消息不会覆盖（幂等）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SEED = os.path.join(PROJECT, "data", "seed", "capture_seed.json")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SEED
    if not os.path.exists(path):
        print(f"找不到种子文件：{path}\n"
              f"请先运行：mitmdump -q -nr <flows.mitm> -s tools/extract_capture.py")
        return 1
    seed = json.load(open(path, encoding="utf-8"))
    conn = db.connect(db.DB_PATH)

    room = seed.get("room") or {}
    room_id = room.get("id") or 0
    if room:
        db.save_room(conn, room)
        db.set_setting(conn, "room_id", str(room_id))
        db.set_setting(conn, "teacher_id", str(room.get("teacherId") or ""))
        db.set_setting(conn, "im_group_id", room.get("imGroupId") or "")
    if seed.get("centraltoken"):
        db.set_setting(conn, "centraltoken", seed["centraltoken"])
        db.set_setting(conn, "token_saved_at", seed.get("extracted_at", ""))
    if seed.get("sdk_app_id"):
        db.set_setting(conn, "im_sdk_app_id", seed["sdk_app_id"])
    im = seed.get("im") or {}
    if im.get("user_sig"):
        db.set_setting(conn, "im_identifier", im.get("sdk_user_id", ""))
        db.set_setting(conn, "im_user_sig", im["user_sig"])
        db.set_setting(conn, "im_enabled", "1")
    me = seed.get("me") or {}
    if me:
        db.set_setting(conn, "my_user_id", str(me.get("userId", "")))
        db.set_setting(conn, "my_nick_name", me.get("nickName", ""))
        db.set_setting(conn, "my_avatar", me.get("photo", ""))

    msgs = seed.get("messages") or []
    added = db.upsert_messages(conn, room_id, msgs, origin="seed")

    # 预取头像 / 图片，让界面首屏就有图
    for m in msgs:
        av = m.get("avatarUrl")
        if av:
            try:
                from app import cache_media  # 复用同一套缓存逻辑
                cache_media(av, "avatar")
            except Exception:
                break
    print(f"[导入] 房间 #{room_id} {room.get('name','')}")
    print(f"[导入] 消息 {len(msgs)} 条（新增 {added} 条）")
    print(f"[导入] 库文件：{db.DB_PATH}")
    print(f"[导入] IM 凭证：{'已导入' if im.get('user_sig') else '无'}"
          f"   centraltoken：{'已导入' if seed.get('centraltoken') else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
