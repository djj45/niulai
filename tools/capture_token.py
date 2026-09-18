# -*- coding: utf-8 -*-
"""
一键抓 centraltoken（mitmproxy addon）
====================================
只要在流量里看到任意一个带 `centraltoken` 请求头的约牛业务请求，就自动把它
POST 给本地约牛聊天室应用（默认 https://127.0.0.1:5002/api/probe），应用会：
  1. 校验 token 是否有效（调 getByTeacherId）
  2. 带出房间信息、imGroupId
  3. 自动刷新 sdkAppId / userSig / 我的资料
  4. 重启 IM 实时通道

用法（与 protocol.md §8 的抓包环境一致）：

    mitmdump -p 8888 --mode upstream:http://127.0.0.1:20122 \
        -s tools/capture_token.py

然后把 macOS 系统代理指向 127.0.0.1:8888，**彻底关掉小程序窗口再重开**
（已建立的长连不会迁移到新代理），进任意一个老师的聊天室即可。
抓到时终端会打印 ✅，浏览器里的应用会自动变成"已配置"。

环境变量：
    NIULAI_API         应用地址，默认 https://127.0.0.1:5002
    NIULAI_TEACHER_ID  老师 ID，默认 328

说明：
  * 只读流量，不修改、不重放任何请求；不会把密码/账号发给任何人。
  * 只用到 Python 标准库，不依赖项目环境（mitmdump 自带解释器里也能跑）。
  * 同一个 token 只上报一次；抓包期间 token 刷新会自动再次上报。
"""
import json
import os
import ssl
import threading
import urllib.error
import urllib.request

from mitmproxy import http

API = os.environ.get("NIULAI_API", "https://127.0.0.1:5002").rstrip("/")
TEACHER_ID = int(os.environ.get("NIULAI_TEACHER_ID", "328"))
ENDPOINT = f"{API}/api/probe"

_seen = set()
_threads: list = []
_lock = threading.Lock()
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# 只看这些域名的请求（约牛业务 API）
WATCH_HOSTS = ("touguapi.zx093.com", "zx093.com", "zx093.cn", "zx0093.com")


def _relevant(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in WATCH_HOSTS)


def _report(token: str):
    """把 token 交给本地应用（异步，别阻塞 mitmproxy 主循环）。"""
    body = json.dumps({"centraltoken": token, "teacher_id": TEACHER_ID}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25, context=_ssl_ctx) as r:
            out = json.loads(r.read().decode("utf-8", "replace"))
        if out.get("room"):
            room = out["room"]
            print(f"\n✅ centraltoken 已自动写入并验证通过")
            print(f"   房间 #{room.get('id')} {room.get('name')}"
                  f"｜老师 {room.get('teacher')}")
            print(f"   IM 凭证：{'已刷新' if out.get('im_ready') else '未刷新（' + str(out.get('im_error')) + '）'}")
            if out.get("user"):
                print(f"   当前账号：{out['user'].get('nickName')} ({out['user'].get('ynNo')})")
            print("   可以恢复系统代理了。\n")
        else:
            print(f"\n⚠️  token 上报了但校验未通过：{out.get('error')}\n")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("error", "")
        except Exception:
            detail = ""
        print(f"\n⚠️  token 校验失败（HTTP {e.code}）：{detail}\n")
    except Exception as e:
        print(f"\n⚠️  上报本地应用失败：{type(e).__name__}: {e}\n"
              f"   （应用是否在 {API} 运行？）\n")


def request(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    if not _relevant(host):
        return
    token = flow.request.headers.get("centraltoken")
    if not token:
        return
    with _lock:
        if token in _seen:
            return
        _seen.add(token)
    print(f"\n🔑 捕获到 centraltoken（来自 {host}{flow.request.path.split('?')[0]}）")
    t = threading.Thread(target=_report, args=(token,), daemon=True)
    t.start()
    with _lock:
        _threads.append(t)


def done() -> None:
    # 等上报线程收尾（离线回放模式下进程马上就退，不 join 会丢输出）
    for t in list(_threads):
        t.join(timeout=30)
    if _seen:
        print(f"[抓包] 共捕获 {len(_seen)} 个 centraltoken")
    else:
        print("[抓包] 未捕获到 centraltoken。检查：\n"
              "  1) 系统代理是否指向 mitmdump 端口\n"
              "  2) 小程序窗口是否**彻底关闭后重开**（旧长连不会走新代理）\n"
              "  3) 是否进了聊天室（要触发业务请求才带该请求头）")
