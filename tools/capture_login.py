# -*- coding: utf-8 -*-
"""
抓「账号密码登录」请求并当场自检（mitmproxy addon）
=================================================
这是给 `accountPwdVerifyLogin.htm` 专门做的抓包器 —— 目的是**验证逆向结论**，
不是重放登录（票据一次性，重放无效）。

抓到 POST `account.zx093.cn/stoneserver/v1/account/accountPwdVerifyLogin.htm` 时：
  1. 原样存样本到 `data/seed/login_sample.json`（含 DES 密文；**明文密码只在内存里解，不落盘**）
  2. POST 给本地应用 `/api/login/verify-sample` 做自检：
       - 用源码里的 DES 密钥解 accountName → 与你说的账号对不对
       - 用 §2 的盐 + 算法重算 sign → 与抓到的是否逐字节一致
  3. 打印 captchaVerifyParam 的结构（看它到底长什么样）
  4. 响应里带 token 的话，顺手喂给 `/api/probe`（和 capture_token.py 同样的效果）

用法（与 protocol.md §8 的抓包环境一致）：

    mitmdump -p 8888 --mode upstream:http://127.0.0.1:20122 \
        -s tools/capture_login.py

然后把系统代理指向 127.0.0.1:8888，在**小程序里点「账号密码登录」**（或在约牛网页版
登录页操作），过一遍滑块即可。抓到时终端会打印解密/签名自检结果。

环境变量：
    NIULAI_API   应用地址，默认 https://127.0.0.1:5002

说明：
  * 只读流量，不修改、不重放任何请求。
  * 只用 Python 标准库（mitmdump 自带解释器里也能跑）。
  * **明文密码不落盘**：只把 DES 密文写进样本文件；解密仅在内存中做，用于比对账号。
"""
import json
import os
import ssl
import threading
import urllib.error
import urllib.request

from mitmproxy import http

API = os.environ.get("NIULAI_API", "https://127.0.0.1:5002").rstrip("/")
SAMPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "seed", "login_sample.json")
TARGET = "accountpwdverifylogin"
LOGIN_HINTS = ("/account/accountPwdVerifyLogin.htm", "login.htm", "accountPwd")
TOKEN_HOSTS = ("touguapi.zx093.com", "account.zx093.cn")

_seen = set()
_threads = []
_lock = threading.Lock()
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _post_json(path: str, payload: dict, timeout: int = 30):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _show_param_shape(param: str):
    """captchaVerifyParam 到底是 JSON 还是序列化串？字段有哪些？"""
    if not param:
        print("       captchaVerifyParam 为空")
        return
    print(f"       captchaVerifyParam: {len(param)} 字符")
    try:
        obj = json.loads(param)
        if isinstance(obj, dict):
            for k, v in obj.items():
                sv = str(v)
                print(f"         · {k:16} = {sv[:60]}{'…' if len(sv) > 60 else ''}")
        else:
            print(f"         (不是 dict，是 {type(obj).__name__})")
    except Exception:
        print(f"         原始值: {param[:160]}{'…' if len(param) > 160 else ''}")


def _verify(sample: dict):
    """交给本地应用做 DES 解密 + sign 重算自检。"""
    try:
        r = _post_json("/api/login/verify-sample", sample)
    except Exception as e:                                  # noqa: BLE001
        print(f"  ⚠️  调本地自检接口失败（应用没起？）：{e}")
        return
    print("  ── 自检结果 " + "─" * 40)
    print(f"     字段: {', '.join(r.get('fields', []))}")
    if r.get("decrypt_ok"):
        print(f"     ✅ DES 解密成功 → accountName = {r.get('account')!r}")
        print(f"        （pwd 解出 {r.get('pwd_len')} 个字符，按隐私只报长度）")
    else:
        print(f"     ❌ DES 解密失败：{r.get('decrypt_error')}")
        print("        → 说明密钥/字母表替换和我们复刻的不一致，需要重新对源码")
    if r.get("sign_ok") is True:
        print(f"     ✅ sign 复刻正确（重算 = 抓到的）")
    elif r.get("sign_ok") is False:
        print(f"     ❌ sign 不一致！")
        print(f"        抓到: {r.get('sign_got')}")
        print(f"        重算: {r.get('sign_expected')}")
        print("        → 检查是否有额外字段参与签名，或空值被过滤")
    else:
        print("     ⚠️  样本里没有 sign 字段，跳过校验")
    print("  " + "─" * 50)


def request(flow: http.HTTPFlow):
    if flow.request.method != "POST":
        return
    url = flow.request.pretty_url
    low = url.lower()
    if TARGET in low or any(h.lower() in low for h in LOGIN_HINTS):
        _handle_login(flow)


def _form(flow: http.HTTPFlow) -> dict:
    body = flow.request.get_text(strict=False) or ""
    if "=" not in body:
        return {}
    from urllib.parse import parse_qsl
    return dict(parse_qsl(body, keep_blank_values=True))


def _handle_login(flow: http.HTTPFlow):
    fields = _form(flow)
    if not fields:
        return
    key = json.dumps({k: fields[k] for k in sorted(fields) if k != "timestamp"}, sort_keys=True)
    with _lock:
        if key in _seen:
            return
        _seen.add(key)

    print("\n" + "=" * 62)
    print("🎯 抓到账号密码登录请求")
    print("=" * 62)
    print(f"  URL : {flow.request.pretty_url}")
    print(f"  字段: {', '.join(sorted(fields))}")
    print()
    _show_param_shape(fields.get("captchaVerifyParam", ""))
    print()
    _verify(fields)

    # 存样本：只存抓到的原样密文，明文不入盘
    try:
        os.makedirs(os.path.dirname(SAMPLE_PATH), exist_ok=True)
        existing = {}
        if os.path.exists(SAMPLE_PATH):
            with open(SAMPLE_PATH, encoding="utf-8") as f:
                existing = json.load(f)
        samples = existing.get("samples") or []
        samples.append({"url": flow.request.pretty_url,
                        "fields": {k: v for k, v in fields.items() if k != "pwd"},
                        "pwd_present": bool(fields.get("pwd")),
                        "note": "pwd 只记录存在性，不明文/密文落盘"})
        with open(SAMPLE_PATH, "w", encoding="utf-8") as f:
            json.dump({"samples": samples}, f, ensure_ascii=False, indent=2)
        print(f"  📄 样本已存（不含 pwd）: data/seed/login_sample.json")
    except Exception as e:                                  # noqa: BLE001
        print(f"  ⚠️  存样本失败：{e}")

    t = threading.Thread(target=_grab_response, args=(flow,), daemon=True)
    with _lock:
        _threads.append(t)
        _threads[:] = [x for x in _threads if x.is_alive()]
    t.start()


def _grab_response(flow: http.HTTPFlow):
    """等响应回来，看 status 和 data（data 就是 centraltoken）。"""
    import time
    for _ in range(60):
        if flow.response is not None:
            break
        time.sleep(0.5)
    if flow.response is None:
        return
    txt = flow.response.get_text(strict=False) or ""
    print("  ── 响应 " + "─" * 44)
    print(f"     HTTP {flow.response.status_code}")
    try:
        j = json.loads(txt)
        print(f"     status={j.get('status')} message={j.get('message') or j.get('msg') or ''}")
        data = j.get("data")
        if isinstance(data, str) and len(data) > 40:
            print(f"     ✅ 拿到 token：{data[:14]}…（{len(data)} 字符）→ 喂给应用")
            try:
                r = _post_json("/api/probe", {"centraltoken": data})
                if r.get("error"):
                    print(f"     ⚠️  应用校验：{r['error']}")
                else:
                    print(f"     ✅ 应用已写入：房间 #{r['room']['id']} {r['room']['name']}，"
                          f"IM {'已就绪' if r.get('im_ready') else '未就绪'}")
            except Exception as e:                          # noqa: BLE001
                print(f"     ⚠️  上报失败：{e}")
        else:
            print(f"     data: {str(data)[:200]}")
    except Exception:
        print(f"     (非 JSON) {txt[:200]}")
    print("  " + "─" * 50 + "\n")
