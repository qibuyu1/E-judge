"""链网E判 启动入口。

浏览器默认访问 http://127.0.0.1:8000 打开前端页面，后端 API 由同一个进程提供。
如果 8000 端口已经被其它程序占用（例如另一个项目的服务常驻在 8000），
uvicorn 会绑定失败并直接退出，此时浏览器打开 8000 看到的是别的服务，
表现就是"页面打不开 / 点查询没有反应 / 读不到后端配置"。
因此这里先做端口自检：首选端口被占用时自动顺延到下一个空闲端口，
并把真实访问地址打印出来。
"""
from __future__ import annotations

import socket
import sys

from web_app.config import settings


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def resolve_port(host: str, preferred: int, span: int = 20) -> tuple[int, list[int]]:
    """返回 (可用端口, 因占用而被跳过的端口列表)。"""
    skipped: list[int] = []
    for port in (preferred, *range(preferred + 1, min(preferred + 1 + span, 65536))):
        if _port_is_free(host, port):
            return port, skipped
        skipped.append(port)
    raise SystemExit(
        f"{host} 的 {preferred}-{preferred + span} 端口全部被占用，请关闭占用进程，"
        "或在 .env 中设置 APP_PORT 为其它端口后重试。"
    )


def main() -> None:
    host = settings.host
    preferred = settings.port
    port, skipped = resolve_port(host, preferred)
    if skipped:
        print("=" * 68)
        print(f"警告：端口 {', '.join(str(item) for item in skipped)} 已被其它程序占用。")
        print("常见原因：另一个项目（如 zhichuang）的服务常驻在 8000 端口。")
        print(f"本次已自动改用空闲端口 {port}，浏览器请访问：http://{host}:{port}")
        print("如需固定使用原端口，请先结束占用该端口的进程，")
        print("或在 .env 中设置 APP_PORT / APP_HOST 后重新启动。")
        print("=" * 68)
    else:
        print(f"链网E判 v{settings.app_version} 已启动，浏览器请访问：http://{host}:{port}")
    print("按 Ctrl+C 停止服务。")

    import uvicorn

    uvicorn.run("web_app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
