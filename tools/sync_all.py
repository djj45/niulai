# -*- coding: utf-8 -*-
"""
命令行全量/按天同步
==================
不依赖 Web 服务，直接读库里存的 centraltoken 拉历史写进同一个 SQLite。
适合首次建库、定时任务（cron）、或不想开浏览器时补数据。

用法：
    uv run python tools/sync_all.py                    # 全量（翻到最早）
    uv run python tools/sync_all.py --days 3           # 只同步近 3 天
    uv run python tools/sync_all.py --source 4         # 老师私聊回复流
    uv run python tools/sync_all.py --status           # 只看库里现状
    uv run python tools/sync_all.py --page-size 500    # 调每页条数（服务端无上限）
    uv run python tools/sync_all.py --room 298         # 指定房间

说明：
  * 约牛只开放最近 7 天（房间信息 visibleDays=-7），翻到第 7 天会返回空数组，属正常到底。
  * 幂等：已有消息不会重复写入，可反复跑。
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db                                    # noqa: E402
import niulai_api as api                     # noqa: E402


def load_client(conn):
    tok = db.get_setting(conn, "centraltoken")
    room = int(db.get_setting(conn, "room_id") or 0)
    if not tok:
        print("❌ 库里没有 centraltoken。先跑 tools/capture_token.py 或在网页「⚙️ 鉴权/设置」填。")
        sys.exit(2)
    return api.TouguClient(tok), room


def show_status(conn):
    st = db.stats(conn)
    print(f"库文件：{db.DB_PATH}")
    print(f"总计 {st['total']} 条 | 老师 {st['teacher']} | 图片 {st['images']} | 用户 {st['users']}")
    print(f"时间跨度 {st['first']} → {st['last']}")
    print("按天：")
    for d in sorted(st["days"], key=lambda x: x["date"]):
        print(f"   {d['date']}  {d['count']:6d} 条（老师 {d['teacher']}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只同步近 N 天（0=全量）")
    ap.add_argument("--source", type=int, default=1, help="1=房间消息流 4=老师私聊回复流")
    ap.add_argument("--page-size", type=int, default=1000, help="每页条数（服务端无上限）")
    ap.add_argument("--max", type=int, default=200000, help="本次最多取多少条")
    ap.add_argument("--room", type=int, default=0, help="指定房间号")
    ap.add_argument("--status", action="store_true", help="只看库里现状")
    ap.add_argument("--sleep", type=float, default=0.3, help="页间隔秒数")
    a = ap.parse_args()

    conn = db.connect(db.DB_PATH)
    if a.status:
        show_status(conn)
        return 0

    client, room = load_client(conn)
    if a.room:
        room = a.room
    if not room:
        print("❌ 不知道房间号。先在网页里「保存并探测」一次，或用 --room 指定。")
        return 2

    since = ""
    if a.days:
        since = (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%d")
    print(f"房间 {room} | source={a.source} | 每页 {a.page_size} | "
          f"{'全量（到最早）' if not since else f'只同步 {since} 之后'}")

    sizes = [a.page_size] + [s for s in (500, 200, 50, 15) if s < a.page_size]
    cursor, pages, fetched, added = "", 0, 0, 0
    t0 = time.time()
    while True:
        items, err = None, ""
        for ps in sizes:
            try:
                items = client.get_chat_records(room, cursor, 1, ps, a.source)
                break
            except api.AuthExpired:
                print("❌ centraltoken 失效，请重新抓取（tools/capture_token.py）")
                return 1
            except Exception as e:                      # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                continue
        if items is None:
            print(f"⚠️  请求失败：{err}")
            break
        if not items:
            print("✅ 服务端返回空数组 → 到底了")
            break

        pages += 1
        n = db.upsert_messages(conn, room, items, origin="rest")
        conn.commit()
        fetched += len(items)
        added += n
        oldest = (items[-1].get("msgDate") or "")[:10]
        rate = fetched / max(0.001, time.time() - t0)
        print(f"  第 {pages:3d} 页 {len(items):5d} 条（新增 {n:5d}）| 最早 {oldest} | "
              f"累计 {fetched} 条 / {rate:.0f} 条每秒")
        cursor = str(items[-1].get("id") or "")
        if not cursor:
            break
        if fetched >= a.max:
            print(f"⚠️  已达 --max {a.max} 条上限")
            break
        if since and oldest and oldest < since:
            print(f"✅ 已越过 {since}")
            break
        time.sleep(a.sleep)

    db.set_sync_state(conn, room, a.source, cursor)
    print(f"\n完成：{pages} 次请求 / 取回 {fetched} 条 / 新增 {added} 条，"
          f"耗时 {time.time() - t0:.0f}s")
    show_status(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
