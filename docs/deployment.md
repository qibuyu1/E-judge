# 部署与排查

## 启动

1. 运行 `scripts/install.bat`。
2. 复制 `.env.example` 为根目录 `.env`，填入两个 API Key。
3. 运行 `scripts/check_config.bat`。
4. 运行 `scripts/start_web.bat`（等价于根目录 `python start_web.py`）。
5. 浏览器访问启动日志打印的地址，默认 `http://127.0.0.1:8000`。

## 端口被占用怎么办

8000 常被本机其他项目（例如 zhichuang）长期占用。此时 `start_web.py` 会自动顺延到下一个空闲端口，并在启动日志与 `check_config` 输出中打印真实地址；也可以在 `.env` 中写入 `APP_PORT=8010` 固定端口。
若浏览器打开后页面不是链网E判，或 `/api/config`、`/api/health` 返回 404，说明访问到了别的服务，请改用启动日志里的端口。

## 更新版本

不要覆盖旧目录。解压到全新目录，只把两个API Key填入新版 `.env`。必要时运行 `scripts/reset_cache.bat`，浏览器按 `Ctrl+F5`。

## 服务器更新到 v9.5.7

本次只改了 Python 后端与前端静态文件，缓存结构 `cache_schema_version` 仍是 `45`，历史报告不受影响。

1. 停服务 → 更新代码 → 重启（`python start_web.py`，或原有的 systemd / supervisor / pm2 单元）。
2. 重启后核对 `GET /api/health` 返回 `version: 9.5.7`、`build_id: 20260802.5`。
3. 浏览器 `Ctrl+F5` 强刷；前端资源已带 `?v=20260802.5`，正常情况下会自动失效重取。
4. 若前面挂着 Cloudflare，建议对 `/` 与 `/static/*` 清一次缓存，避免浏览器仍拿到旧的 `query.js` / `report.js`。

## 检查

- `/api/config`：版本、模型、搜索服务状态。
- `/api/health`：后端、缓存和API准备状态。
- `/api/stock/debug?q=600282`：股票代码结构化解析诊断。
- `/api/search/debug?q=企业名称或代码`：Tavily身份检索诊断。

- `/api/tavily/usage`：读取Tavily账户真实套餐、已用额度和上限，不消耗搜索credit。
