# -*- coding: utf-8 -*-
"""
从 mitmproxy flow 文件里导出约牛数据种子（房间/历史消息/鉴权凭证）
=================================================================
用法：
    mitmdump -q -nr <flows.mitm> -s tools/extract_capture.py
环境变量：
    SEED_OUT  输出路径（默认 <项目>/data/seed/capture_seed.json）

⚠️ 只读取抓包文件，不修改、不删除任何原始数据。
"""
import json
import os
import time

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("SEED_OUT", os.path.join(_PROJECT, "data", "seed", "capture_seed.json"))

seed = {
    "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "centraltoken": "",
    "room": {},
    "sdk_app_id": "",
    "im": {},
    "me": {},
    "messages": {},
    "ws_login_frame": {},
}


def request(flow) -> None:
    tok = flow.request.headers.get("centraltoken")
    if tok:
        seed["centraltoken"] = tok


def response(flow) -> None:
    path = flow.request.path
    if "zx093" not in flow.request.pretty_host:
        return
    try:
        body = json.loads(flow.response.content)
    except Exception:
        return
    if not isinstance(body, dict):
        return
    data = body.get("data")
    if "getByTeacherId" in path and isinstance(data, dict) and data.get("id"):
        seed["room"] = data
    elif "getSdkAppId" in path and isinstance(data, dict):
        seed["sdk_app_id"] = data.get("sdkAppId", "")
    elif "getUserSig" in path and isinstance(data, dict):
        seed["im"] = {"sdk_user_id": data.get("sdkUserId", ""),
                      "user_sig": data.get("userSig", "")}
    elif "getChatRecordList" in path and isinstance(data, list):
        for m in data:
            mid = m.get("id")
            if mid:
                seed["messages"][str(mid)] = m
    elif "user/getInfo" in path and isinstance(data, dict):
        seed["me"] = data


def websocket_message(flow) -> None:
    if "my-imcloud" not in flow.request.pretty_host:
        return
    msg = flow.websocket.messages[-1]
    if not msg.from_client:
        return
    if not seed["ws_login_frame"]:
        try:
            text = msg.content.decode("utf-8", "replace")
        except Exception:
            return
        if '"im_open_status.wslogin"' in text:
            try:
                seed["ws_login_frame"] = json.loads(text)
            except Exception:
                pass


def done() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    seed["messages"] = list(seed["messages"].values())
    seed["messages"].sort(key=lambda m: (m.get("id") or 0))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(seed, f, ensure_ascii=False, indent=1)
    print(f"[导出] {len(seed['messages'])} 条消息 → {OUT}")
    print(f"[导出] 房间 {seed['room'].get('name')} / IM {seed['im'].get('sdk_user_id')} "
          f"/ 登录帧 {'有' if seed['ws_login_frame'] else '无'}")
