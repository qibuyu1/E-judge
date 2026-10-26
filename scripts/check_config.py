"""链网E判 配置自检：核对 .env、API Key、端口占用与运行环境。"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_app.config import settings  # noqa: E402


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def main() -> None:
    print("链网E判配置检查")
    print("-" * 48)
    print(f"项目根目录: {ROOT}")
    print(f"版本: {settings.app_version} ({settings.build_id})")
    print(f"Tavily API: {'已配置' if settings.tavily_ready else '未配置'}")
    print(f"DeepSeek API: {'已配置' if settings.deepseek_ready else '未配置'}")
    print(f"抽取模型: {settings.fast_model}（固定使用Flash，旧.env中的Pro设置会被忽略）")
    print(f"决策模型: {settings.review_model if settings.enable_pro_decision_analysis else '未启用'}")
    print(f"证券代码结构化解析: {'启用' if settings.stock_resolver_enabled else '关闭'}")
    print(f"上市公司结构化档案: {'启用' if settings.listed_profile_resolver_enabled else '关闭'}")
    print(f"Tavily定向正文提取: {'启用' if settings.tavily_extract_enabled else '关闭'}")
    print(f"证据补全轮数: {settings.tavily_followup_rounds}")
    print("本地搜索信用点上限: 无（不会因内部预算中断调查）")
    print(f"Tavily国家偏置: {settings.tavily_country or '未启用'}")
    port_free = _port_is_free(settings.host, settings.port)
    state = "端口空闲，可直接启动" if port_free else "端口已被占用，启动时会自动顺延到空闲端口"
    print(f"监听地址: {settings.host}:{settings.port}（{state}）")
    print(f"系统状态: {'可以启动真实查询' if settings.api_ready else '请先完善 .env'}")


if __name__ == "__main__":
    main()
