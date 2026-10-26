"""兼容入口：实际FastAPI应用位于 web_app.main。"""

from web_app.main import app

__all__ = ["app"]
