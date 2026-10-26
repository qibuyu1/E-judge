# 部署与排查

## 运行环境要求（务必先看）

- **Python >= 3.11**，推荐 3.12。`web_app/deepseek_client.py:646`、`web_app/stock_resolver.py:369`、`web_app/stock_resolver.py:404` 使用 `asyncio.timeout()`，该 API 从 Python 3.11 才有。
- 用 3.10 跑不会报错退出，但每一次 DeepSeek 调用都会抛 `AttributeError: module 'asyncio' has no attribute 'timeout'` 并降级成规则兜底：页面照常出报告，但公司信息、风险分、决策分析全部不可信。线上服务器曾因此在 Python 3.10.12 上静默退化两个多月。
- 自查方式：`GET /api/health` 看 `python_supported`（必须为 `true`）与 `python_version`；也可用 `systemctl cat ejudge` 确认 ExecStart 指向的解释器路径。
- 用 uv 建 3.12 环境（无需 root）：

```bash
uv venv venv312 --python 3.12
uv pip install --python venv312/bin/python -r requirements.txt
# 再把启动命令指向 venv312/bin/python（systemd 的 ExecStart 或 supervisor 配置）
```

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

## 服务器更新到 v9.5.8

本次为环境守卫版本，未改缓存结构（`cache_schema_version` 仍为 `45`）与数据结构。

1. 确认部署机解释器是 3.11+（见上节）；仍是 3.10 的先按上面 uv 步骤换环境，否则本次更新没有任何效果。
2. `git pull` 后重启服务，核对 `GET /api/health` 返回 `version: 9.5.8`、`build_id: 20260802.6`、`python_supported: true`。
3. 浏览器 `Ctrl+F5` 强刷；前端资源已带 `?v=20260802.6`。
4. 挂 Cloudflare 的话，对 `/` 与 `/static/*` 清一次缓存。

## 检查

- `/api/config`：版本、模型、搜索服务状态。
- `/api/health`：后端、缓存和API准备状态。
- `/api/stock/debug?q=600282`：股票代码结构化解析诊断。
- `/api/search/debug?q=企业名称或代码`：Tavily身份检索诊断。

- `/api/tavily/usage`：读取Tavily账户真实套餐、已用额度和上限，不消耗搜索credit。
