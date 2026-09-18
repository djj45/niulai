# -*- coding: utf-8 -*-
"""
Hypercorn 启动脚本（HTTP/2 + HTTPS，多路复用，图片多也不卡）
==========================================================
浏览器打开：https://127.0.0.1:5002  （自签证书首次会提示信任）
"""
import asyncio
import os
import signal


def _patch_hypercorn_bugs():
    """修复 Hypercorn 的两个 HTTP/2 问题，避免进程崩溃：
    1. PriorityLoop：浏览器 priority 帧触发循环检测异常
    2. KeyError：浏览器对已关闭的 stream 发送数据
    """
    try:
        from hypercorn.protocol import h2 as h2mod
        import priority

        original_priority = h2mod.H2Protocol._priority_updated

        async def patched_priority(self, event):
            try:
                await original_priority(self, event)
            except priority.PriorityLoop:
                pass

        h2mod.H2Protocol._priority_updated = patched_priority

        original_handle = h2mod.H2Protocol._handle_events

        async def patched_handle(self, events):
            for event in events:
                try:
                    await original_handle(self, [event])
                except KeyError:
                    pass

        h2mod.H2Protocol._handle_events = patched_handle
    except Exception:
        pass


_patch_hypercorn_bugs()

from hypercorn.asyncio import serve          # noqa: E402
from hypercorn.config import Config          # noqa: E402

from app import app                          # noqa: E402

HOST = os.environ.get("NIULAI_HOST", "127.0.0.1")
PORT = int(os.environ.get("NIULAI_PORT", "5002"))
CERT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cert.pem")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "key.pem")


async def main():
    config = Config()
    config.bind = [f"{HOST}:{PORT}"]
    if os.path.exists(CERT) and os.path.exists(KEY):
        config.certfile = CERT
        config.keyfile = KEY
        scheme = "https"
    else:
        scheme = "http"
    config.h2 = True

    print("=" * 56)
    print(f"约牛聊天室  →  {scheme}://{HOST}:{PORT}")
    print("按 Ctrl+C 停止")
    print("=" * 56)

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()

    def _on_signal(*_):
        shutdown.set()
        loop.call_later(1.0, os._exit, 0)

    try:
        loop.add_signal_handler(signal.SIGINT, _on_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_signal)
    except NotImplementedError:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

    await serve(app, config, shutdown_trigger=shutdown.wait)


if __name__ == "__main__":
    asyncio.run(main())
