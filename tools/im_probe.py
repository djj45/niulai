# -*- coding: utf-8 -*-
"""
腾讯云 IM 通道探针（只读：登录 + 进群 + 打印推送，不发消息）
=========================================================
用法：
    uv run python tools/im_probe.py --identifier cu_1234567 --usersig "<sig>" \
        [--group "@TGS#_@TGS#..."] [--sdk-app-id 1600075223] [--seconds 60]

用途：
  - 验证 userSig 是否仍有效（打印 70003 表示过期/非法，0 表示成功）
  - 观察实时推送帧结构与群提示（Event=3 聊天 / Event=4 群提示）
  - 排查「连上了但收不到消息」一类问题

实现要点（protocol.md §7）：
  * 端点就是 `wss://<sdkAppId>w4c.my-imcloud.com/binfo?...`
  * **客户端必须发二进制帧**（同内容发 text 帧服务端会静默丢弃）
  * 服务端帧可能是 `COMP` + 流式 gzip，必须用 decompressobj(31)
  * 顺序：heartbeat.alive → im_open_status.wslogin → apply_join_group
"""
import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from niulai_im import (CMD_GROUP_LIST, CMD_HEARTBEAT, CMD_JOIN_GROUP, CMD_LOGIN,  # noqa: E402
                       CMD_PUSH, SDK_ABILITY, WEBSDK_APPID, WEBSDK_VERSION,
                       decode_frame, encode_frame, parse_push)

DEFAULT_GROUP = "@TGS#_@TGS#XXXXXXXXXXXXXXXX"


async def run(sdk_app_id, identifier, usersig, group, seconds):
    import secrets

    import websockets

    url = (f"wss://{sdk_app_id}w4c.my-imcloud.com/binfo?sdkappid={sdk_app_id}"
           f"&instanceid={secrets.token_hex(16)}&random={__import__('random').random()}"
           f"&platform=8&host=mac&version=-1&sdkversion=4.4.3&compress=gzip")
    seq = [1_000_000]
    state = {"a2": "", "tinyid": "", "instid": 0}
    print(f"连接 {url}")

    def frame(cmd, extra=None, body=None):
        h = {"servcmd": cmd, "ver": "v4", "platform": 8, "websdkappid": WEBSDK_APPID,
             "websdkversion": WEBSDK_VERSION, "status_instid": state["instid"],
             "sdkappid": sdk_app_id, "contenttype": "json", "reqtime": int(time.time()),
             "sdkability": SDK_ABILITY, "sdkability_ext": "", "cappid": 0, "tjgID": "",
             "seq": seq[0], "cs": 0}
        seq[0] += 1
        if state["a2"]:
            h["a2"] = state["a2"]
        if state["tinyid"]:
            h["tinyid"] = state["tinyid"]
        if extra:
            h.update(extra)
        return encode_frame({"head": h, "body": body or {}})

    async with websockets.connect(
            url, origin=f"https://{sdk_app_id}w4c.my-imcloud.com",
            additional_headers={"User-Agent": os.environ.get("NIULAI_UA", ""),
                                "content-type": "application/json"},
            max_size=None, open_timeout=15, ping_interval=None) as ws:
        print("已连接，发送 heartbeat.alive")
        await ws.send(frame(CMD_HEARTBEAT))
        logged = False
        hb_sent = False
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                if time.time() - hb_sent > 25:
                    await ws.send(frame(CMD_HEARTBEAT))
                    hb_sent = time.time()
                continue
            except websockets.ConnectionClosed as e:
                print(f"连接关闭 code={e.code} {e.reason}")
                return 1
            try:
                o = json.loads(decode_frame(raw))
            except Exception as e:
                print(f"[解码失败] {e}")
                continue
            cmd = o.get("head", {}).get("servcmd")
            body = o.get("body") or {}
            if cmd == CMD_HEARTBEAT:
                if not logged:
                    logged = True
                    await ws.send(frame(CMD_LOGIN,
                                        {"identifier": identifier, "usersig": usersig},
                                        {"State": "Online", "is_web_uniapp": 0,
                                         "InstType": 0, "CustomInfo": ""}))
                    print("→ im_open_status.wslogin")
                continue
            if cmd == CMD_LOGIN:
                if body.get("A2Key"):
                    state.update(a2=body["A2Key"], tinyid=body.get("TinyId"),
                                 instid=body.get("InstId"))
                    print(f"✅ 登录成功 TinyId={state['tinyid']} InstId={state['instid']} "
                          f"心跳间隔={body.get('HelloInterval')}s")
                    if group:
                        await ws.send(frame(CMD_JOIN_GROUP, None,
                                            {"GroupId": group,
                                             "HugeGroupHistoryMsgFlag": 1}))
                        await ws.send(frame(CMD_GROUP_LIST, None, {
                            "Type": "Community", "Limit": 200, "Offset": 0,
                            "Member_Account": identifier, "SupportTopic": 0,
                            "NeedAppDefineData": 1}))
                else:
                    print(f"❌ 登录失败 ErrorCode={body.get('ErrorCode')} "
                          f"{body.get('ErrorInfo')}")
                    return 1
                continue
            if cmd == CMD_PUSH:
                msgs, events = parse_push(body)
                for e in events:
                    print(f"  [群事件] {e.get('text')} 在线 {e.get('member_num')}")
                for m in msgs:
                    print(f"  [消息] seq={m['im_msg_seq']} type={m['msg_type']} "
                          f"{m['nick_name']}({m['yn_no']}) user_type={m['user_type']} "
                          f"{str(m['msg_content'])[:80]}")
                sd = body.get("SessionData")
                if sd and body.get("NeedAck"):
                    from niulai_im import CMD_PUSH_ACK
                    await ws.send(frame(CMD_PUSH_ACK, None, {"SessionData": sd}))
                continue
            if cmd in (CMD_JOIN_GROUP, CMD_GROUP_LIST):
                print(f"  [{cmd}] ErrorCode={body.get('ErrorCode')} {body.get('ErrorInfo','')}")
                continue
            print(f"  [{cmd}]")
    print("探针结束")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdk-app-id", default="1600075223")
    ap.add_argument("--identifier", required=True, help="形如 cu_1234567")
    ap.add_argument("--usersig", required=True)
    ap.add_argument("--group", default=DEFAULT_GROUP)
    ap.add_argument("--seconds", type=int, default=60)
    a = ap.parse_args()
    if not os.environ.get("NIULAI_UA"):
        import niulai_api
        os.environ["NIULAI_UA"] = niulai_api.UA
    return asyncio.run(run(int(a.sdk_app_id), a.identifier, a.usersig, a.group, a.seconds))


if __name__ == "__main__":
    sys.exit(main())
