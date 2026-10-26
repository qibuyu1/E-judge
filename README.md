# 链网E判 v9.5.7

面向银行对公授信与企业合作审查的企业公开信息、关联关系、经营偿债与履约风险分析系统。
输入企业名称或股票代码，系统检索公开证据、抽取结构化事实、构建关联图谱，输出低/中/高三档风险结论与审查建议。

## 目录结构

```text
链网E判2/
├── start_web.py           Web 服务主入口（自动选端口）
├── run_all.py             命令行一键尽调（不开浏览器）
├── requirements.txt       Python 依赖
├── pytest.ini             测试配置
├── .env.example           配置模板（复制为 .env 后填写密钥）
├── web_app/               后端核心
│   ├── main.py            FastAPI 路由、任务状态机
│   ├── config.py          版本、端口、模型与阈值配置
│   ├── search_service.py  Tavily 检索与证据精读
│   ├── deepseek_client.py DeepSeek 调用、结构化修复与重试
│   ├── risk_engine.py     风险评分、去重与状态衰减
│   ├── report_service.py  报告装配、缓存读写
│   └── ...                名称清洗、图谱、股票解析等模块
├── frontend/              前端页面（index.html / report.html + js / css / 视频）
├── scripts/               安装、启动、体检、清缓存等脚本
├── tests/                 pytest 用例
├── tools/                 诊断与抓数工具
├── pipeline/              旧版分阶段流水线（01_crawler … 05_api_server，可选，不参与 Web 服务）
├── docs/                  文档：架构、部署、风险评分、数据说明、变更记录
└── runtime/               运行时缓存（research_cache.db，可安全删除）
```

## 快速启动（Windows）

```text
1. scripts\install.bat          安装依赖并体检
2. 复制 .env.example 为 .env，填入两个 API Key
3. scripts\check_config.bat     检查密钥、模型、端口
4. scripts\start_web.bat        启动服务
```

等价的手动方式（**需 Python 3.11+**，推荐 3.12；3.10 会让所有模型调用静默失效）：

```powershell
python -m pip install -r requirements.txt
python scripts/check_config.py
python start_web.py
```

浏览器访问启动日志打印的地址，默认 `http://127.0.0.1:8000`。

### 端口被占用时

8000 常被本机其他项目长期占用。`start_web.py` 会先探测端口，被占用就自动顺延到下一个空闲端口（最多 20 个），并在启动日志与 `scripts/check_config.py` 中打印真实地址。
**如果打开页面看到的不是链网E判，或 `/api/config`、`/api/health` 返回 404，说明请求落到了别的服务上**，请改用日志里的端口，或在 `.env` 中写 `APP_PORT=8010` 固定。

## 三档结论

- **低风险（0–39.9）**：当前公开证据下风险相对可控，可进入常规审查；不等于零风险。
- **中风险（40–59.9）**：存在需核验的实质关注点，建议有条件准入并控制额度、期限、担保与账期。
- **高风险（60–100）**：已出现直接信用失败、严重审计异常、重大执行/失信/违约，或持续经营与偿债压力；落实核验与增信前不宜新增无保障敞口。

证据不足时显示“证据不足/待核验”，不把缺失数据当低风险，也不仅凭企业名称或行业印象定级。
评分遵循“主体归属 → 事件性质 → 当前状态 → 金额比例 → 偿债影响”的顺序，已解除、已结案、已清偿、已整改事项显著降权，同一风险按家族去重。

## 页面

- `/`：品牌首页与企业查询页。
- `/report?report_id=...`：完整风险分析报告。
- 顶栏“历史查询”只展示与当前评分版本一致的报告，旧评分版本报告自动失效。

## 诊断接口

- `/api/config`：版本、模型、检索与模型服务状态。
- `/api/health`：后端、缓存与 API 准备状态。
- `/api/stock/debug?q=600282`：股票代码解析诊断。
- `/api/search/debug?q=企业名称或代码`：Tavily 身份检索诊断。
- `/api/tavily/usage`：Tavily 套餐、已用额度与上限（不消耗 credit）。

## 测试

```powershell
python -m pip install pytest
python -m pytest -q
```

## 升级与缓存

不要覆盖旧目录：解压到全新目录，只把两个 API Key 填入新版 `.env`。
评分或字段结构变化时 `web_app/config.py` 的 `cache_schema_version` 会升级，旧评分报告自动失效；也可运行 `scripts/reset_cache.bat` 主动清空 `runtime/cache` 并刷新浏览器缓存（Ctrl+F5）。

## 文档

- `docs/architecture.md`：技术架构与模块职责。
- `docs/deployment.md`：部署、端口排查与诊断接口。
- `docs/risk-scoring.md`：风险评分规则与外部基准审计。
- `docs/data-accuracy.md`：数据来源与准确性边界。
- `docs/api-and-video.md`：API 与首页视频配置。
- `docs/CHANGELOG.md`：历次版本变更（v9.2 → v9.5.7）。
- `docs/release-notes-8.4.1.md`、`docs/architecture-and-deployment-v8.4.1.docx`：历史版本资料。

用户提供的 `benchmark.xlsx` 不随项目分发、不在运行时读取，也不存在按企业名称查人工等级并强制改分的逻辑。
