# AIOS Intelligence Monitor

本地情报监测与分析系统 — 一个运行在你自己电脑上的单用户 Web 应用。

它不是"每天自动写一篇文章"的脚本，而是一个 **持续情报监测系统**。核心对象是：

```
来源 Source → 文章 Article → 事件 Event → 观测 Observation → 变化 Change → 报告 Report
```

日报只是最终展示层。真正有长期价值的是：某个技术事件第一次什么时候出现、之后发生了
什么变化、哪个数字改变了、哪些来源证明了这个变化、模型当时做出了什么判断、今天和昨天
究竟有什么新增。

---

## 目录

- [它能做什么](#它能做什么)
- [安装](#安装)
- [AI 模型接入](#ai-模型接入)
- [Windows 自动启动](#windows-自动启动)
- [运行](#运行)
- [数据保存在哪里](#数据保存在哪里)
- [修改监测模块](#修改监测模块)
- [新增 Topic 与检索式](#新增-topic-与检索式)
- [执行监测](#执行监测)
- [设置自动运行](#设置自动运行)
- [查看报告对比](#查看报告对比)
- [导入历史报告](#导入历史报告)
- [备份与恢复](#备份与恢复)
- [升级](#升级)
- [架构](#架构)
- [测试](#测试)
- [已知限制](#已知限制)

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| 本地 Web UI | FastAPI + Jinja2 + HTMX，不需要 Node.js / Docker / MySQL |
| 可编辑监测配置 | 模块 / Topic / 检索式全部存在数据库，网页可改，改完下次监测立即生效 |
| 多模型可替换 | DeepSeek / 通义千问 / 智谱 GLM / Kimi / 豆包 / MiniMax / 混元 / 千帆 / 硅基流动 + OpenAI / Claude / Gemini / Ollama |
| 任务级模型分配 | 便宜模型做筛选与归并，强模型做综合研判，全部在设置页配置 |
| API Key 安全存储 | 按服务商分别保存在 Windows 凭据管理器，**不入库、不入日志、不入 HTML/JSON** |
| Windows 自动启动 | 登录即启动，无需管理员权限 |
| 情报事件追踪 | 同一件事持续发展时创建新的 Observation，而不是新的 Event |
| 结构化 Diff | 事件级差分：NEW / UPDATED / UNCHANGED / DATA_CHANGE / CORRECTION / RESOLVED |
| 数值变化计算 | `8500万 → 8720万` 显示 `+220万 / +2.59%`；无法比较时只显示 `旧 → 新`，不瞎算 |
| 来源证据链 | 每条情报可追溯到 Observation → RawArticle → URL |
| 定时执行 | APScheduler 每天定时运行，重启后自动恢复 |
| 运行进度 | 阶段式展示（采集 / 抓取 / 分析 / 归并 / 生成），不伪造百分比 |
| HTML 日报 + JSON 审计 | 沿用原有卡片式版式，同时保留机器可读快照 |

---

## 安装

需要 **Python 3.11 或更高版本**（开发环境为 3.12）。

```bash
python -m pip install -r requirements.txt
```

---

## AI 模型接入

AIOS 负责情报流程，模型只是**可替换的推理引擎**。切换服务商或模型只需要改设置，
不需要改任何 Python 代码。

### 支持的服务商

**国内服务商（优先支持）**

| 服务商 | provider_id | 默认 Base URL |
|---|---|---|
| DeepSeek | `deepseek` | `https://api.deepseek.com` |
| 通义千问（阿里云百炼） | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM | `zhipu` | `https://open.bigmodel.cn/api/paas/v4` |
| Kimi（月之暗面） | `moonshot` | `https://api.moonshot.cn/v1` |
| 豆包（火山方舟） | `doubao` | `https://ark.cn-beijing.volces.com/api/v3` |
| MiniMax | `minimax` | `https://api.minimax.chat/v1` |
| 腾讯混元 | `hunyuan` | `https://api.hunyuan.cloud.tencent.com/v1` |
| 百度千帆 / 文心 | `qianfan` | `https://qianfan.baidubce.com/v2` |
| 硅基流动 | `siliconflow` | `https://api.siliconflow.cn/v1` |

**国际 / 本地**

| 服务商 | provider_id | 说明 |
|---|---|---|
| OpenAI | `openai` | |
| Anthropic Claude | `anthropic` | 独立适配器（Messages API） |
| Google Gemini | `gemini` | 独立适配器（generateContent） |
| OpenRouter | `openrouter` | |
| Ollama（本地模型） | `ollama` | 无需 API Key |
| 本地 / 自建兼容服务 | `local_openai` | LM Studio、vLLM、Xinference 等 |
| 自定义兼容端点 | `custom` | 任意提供 `/chat/completions` 的网关 |

> 上表的 Base URL 是**默认值**，随时可以改成企业版端点、代理网关或自建服务。

### 添加一个国内服务商

以通义千问为例：

1. 打开 **设置 → AI 模型**（`/settings/ai`）
2. 在「添加服务商」中选择 **通义千问（阿里云百炼）**
   Base URL 与示例模型会自动填入
3. 填写模型名，例如 `qwen-plus`
4. 粘贴 API Key
5. 点击「添加服务商」
6. 点击该卡片的「测试连接」确认可用
7. 点击「设为默认」

下一次监测就会使用通义千问，**不需要修改任何 Python 文件**。

智谱 GLM、Kimi、豆包、MiniMax、混元、千帆、硅基流动的步骤完全相同。

> **火山方舟（豆包）特别说明**：模型名要填「接入点 ID」(endpoint id)，
> 而不是模型的公开名称。

### 模型名称

模型名是**自由文本**。厂商发布新模型时直接填写即可，系统不会因为模型名不在
内置列表里而拒绝。界面上显示的示例模型只是占位提示。

### 使用 OpenAI 兼容端点

大多数国内 API 都提供 OpenAI 兼容接口，因此共用同一个适配器
（`OpenAICompatibleProvider`）。如果你使用的是：

- 代理网关
- 企业专属端点
- 自建的兼容服务

选择「自定义 OpenAI 兼容端点」，填入 Base URL 和模型名即可。

### 配置 Ollama（完全本地运行）

```
服务商:    Ollama（本地模型）
Base URL:  http://127.0.0.1:11434/v1
模型:      qwen3:32b
API Key:   留空（不需要）
```

先在本机执行 `ollama pull qwen3:32b`。LM Studio、vLLM、Xinference 同理，
使用「本地 / 自建 OpenAI 兼容服务」并填入各自的端口。

### 任务级模型分配

不同环节的负载不同：

| 任务 | 特点 | 建议 |
|---|---|---|
| 主题情报分析 | 量大、重复 | 便宜快速的模型 |
| 事件归并判断 | 短输入短输出 | 便宜快速的模型 |
| 日报综合研判 | 跨来源综合 | 更强的模型 |

在 **设置 → AI 模型 → 任务模型分配** 中，每个任务可以：

- 留空 = 使用默认服务商
- 指定某个已配置的服务商
- 额外指定模型名（留空则用该服务商的默认模型）

例如：默认用 DeepSeek，只把「日报综合研判」指给智谱 GLM。一次监测会同时用到两家，
每次调用都会按服务商记入用量统计。

### API Key 如何存储

- 使用 Python `keyring`，保存在操作系统凭据存储（Windows 为**凭据管理器**）
- **每个服务商独立存放**，用户名槽位为 `provider:<provider_id>`，
  例如 `provider:deepseek`、`provider:qwen`
- 配置通义千问不会影响已有的 DeepSeek Key
- 数据库中只保存 `has_api_key` 与 `api_key_last_four`（末四位）
- 完整 Key 不会出现在：数据库、日志、HTML 报告、JSON 审计、备份文件、异常信息中

自行验证：

```bash
python -c "print(open('data/aios.db','rb').read().find(b'sk-'))"   # 应输出 -1
```

### 测试连接

每个服务商卡片都有「测试连接」，会发送一个极小的请求并显示：

```
连接成功 · 通义千问（阿里云百炼） · 模型 qwen-plus · 响应 821 ms
```

失败时显示可读的原因（限流 / 余额不足 / Key 无效 / 网络错误），
**不会**回显任何认证信息。

### 从旧版 DeepSeek 配置升级

已有安装无需任何操作：

- 旧的 `deepseek_*` 设置会自动迁移成一条 DeepSeek 服务商配置
- 迁移后 DeepSeek 仍是默认服务商
- 凭据管理器中已有的 Key 会被复制到 `provider:deepseek`，**不需要重新输入**
- 迁移只在首次升级时执行一次，之后不会覆盖你的配置

---

## Windows 自动启动

APScheduler 只在 AIOS 运行时有效。开启自动启动后：

```
Windows 登录 → AIOS 自动启动 → APScheduler 生效 → 到点自动生成日报
```

在 **设置 → Windows 自动启动** 中开启或关闭。

### 两种机制

| 机制 | 何时使用 | 是否需要管理员 |
|---|---|---|
| 任务计划程序（Task Scheduler） | AIOS 以管理员身份运行时 | 是 |
| 启动文件夹（Startup） | 其他情况（默认） | **否** |

Windows 规定**登录触发（ONLOGON）的计划任务需要管理员权限**（日常的 DAILY/ONCE
任务不需要）。因此普通用户默认使用启动文件夹方式：在
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` 中创建一个指向
`pythonw.exe` 的快捷方式，效果相同且无需任何权限。

两种机制不会同时生效——以管理员身份启用任务计划时会清除启动文件夹中的条目。

### 启动行为

- 自动启动使用 `python app.py --no-browser`，**不会**每次登录都弹出浏览器
- 手动执行 `python app.py` 仍然会自动打开浏览器
- 使用 `pythonw.exe`，不显示控制台窗口（若不可用则退回 `.cmd`，可靠性优先）
- 如果 AIOS 已在运行，`--no-browser` 启动会检测到 8765 端口已占用并**干净退出**，
  不会产生第二个服务进程

### 安全

计划任务/快捷方式中**只包含**：解释器路径、`app.py` 路径、`--no-browser`。
任何 API Key 都不会写入其中——凭据始终在启动后从系统凭据存储读取。

### 路径变更

启动项在创建时会写入当前的解释器与项目路径。如果之后移动了项目目录，
请在设置中**先关闭再重新开启**自动启动。

---

## 分工：Windows 与 APScheduler

这个分工是刻意的，不要混淆：

| 组件 | 职责 |
|---|---|
| Windows（任务计划 / 启动文件夹） | 登录后**启动 AIOS 一次** |
| AIOS 内置 APScheduler | 决定**每天什么时候执行监测** |

Windows 不直接执行监测任务——那样会绕过定时设置、并发保护和执行历史记录。

---

## 运行

```bash
python app.py
```

或：

```bash
python -m aios
```

启动后会看到：

```
  AIOS Intelligence Monitor v2.1.0
  Starting server...

  Database:   ...\data\aios.db
  Reports:    ...\data\reports
  Logs:       ...\data\logs
  AI model:   DeepSeek / deepseek-chat  (+1 more configured)
  Web UI:     http://127.0.0.1:8765

  Press Ctrl+C to stop.
```

浏览器会自动打开 <http://127.0.0.1:8765>。

常用参数：

```bash
python app.py --no-browser        # 不自动打开浏览器
python app.py --port 8766         # 换端口
python app.py --reload            # 开发模式，代码改动自动重载
```

端口被占用时会明确报错并提示换端口，**不会**偷偷改成随机端口。

---

## 数据保存在哪里

全部在本机，默认位于项目下的 `data/`：

```
data/
├── aios.db          SQLite 数据库 —— 唯一的事实来源
├── reports/         导出的 HTML 日报与 JSON 审计文件
├── logs/            运行日志（滚动，最多 5 × 2MB）
└── cache/           临时缓存
```

可以用环境变量把数据目录放到别处：

```powershell
$env:AIOS_DATA_DIR = "D:\AIOS\data"
python app.py
```

> **数据库是唯一事实来源。** `reports/` 里的 HTML 和 JSON 是导出产物，删掉不会丢数据，
> 随时可以在报告页点"重新导出"重新生成。

---

## 修改监测模块

进入 **监测配置**（`/monitoring`）。首次启动会自动创建 8 个默认模块，
它们来自原 `aios_daily.py` 中的 `TRACKS` 常量：

| 模块 | Topics |
|---|---|
| 移动智能终端侧 | HarmonyOS / Android / Apple Intelligence |
| PC 侧 | Windows / HarmonyOS PC / AI PC |
| 服务器侧 | openEuler / Anolis / Linux AI |
| 智算超节点侧 | Huawei SuperPoD / 国产超节点 / Interconnect |
| 物联网侧 | OpenHarmony / RTOS / Industrial IoT |
| 无人飞行器侧 | Flight Control OS / Edge AI UAV / Low Altitude Economy |
| 具身智能侧 | ROS / OpenHarmony Robotics / Isaac / VLA |
| 太空智算侧 | Space OS / Satellite Computing / Space AI |

共 24 个 Topic、51 条检索式。原脚本里的每一条 query 都被原样保留。

每个模块可以修改：名称、标识、描述、启用状态、回溯天数、最大候选数、
最多入报条目数、分析补充说明、排序。

**删除策略**：模块和 Topic 使用**归档（软删除）**而不是真删除，这样历史报告仍然
可以正常查看。检索式没有历史数据，可以直接删除。

> ⚠️ 种子数据只在**数据库首次创建**时写入。以后重启应用**不会**覆盖你的配置。

---

## 新增 Topic 与检索式

完全不需要修改任何 Python 文件。

1. 监测配置 → 点击某个模块的"编辑"
2. 在"新增主题"里填名称，例如 `AI Browser`
3. 进入该主题页面，逐条添加检索式：
   ```
   OpenAI browser agent
   Gemini Chrome agent
   Perplexity Comet
   ```
4. 可选：添加"优先来源"（例如 `openai.com`）让官方源排在转载之前
5. 可选：添加"排除关键词"（例如 `招聘`）过滤垃圾结果

下一次监测就会自动包含这个新主题。

新增一个完整模块也是一样：监测配置页最下方的"新增监测模块"表单。

---

## 执行监测

**手动**：概览页点击"立即执行监测"。

任务在后台线程运行，HTTP 请求立即返回，页面每 2 秒轮询一次状态，依次显示：

```
采集中 → 抓取正文 → 分析中 → 事件归并 → 生成报告
```

每个模块单独显示状态和候选/来源/入报数量。

**失败隔离**：某个模块失败不会影响其他模块。最终状态可能是：

| 状态 | 含义 |
|---|---|
| `completed` | 全部成功 |
| `completed_with_errors` | 部分模块/主题失败，其余结果已保留 |
| `failed` | 整体失败（例如未配置 API Key） |
| `cancelled` | 用户取消 |

运行日志在 `/runs/{id}` 页面逐行展示，也写入 `data/logs/aios.log`。

---

## 设置自动运行

设置页 → **自动监测**：

- 开启/关闭
- 每日执行时间（例如 `06:00`）
- 时区自动使用本机时区

配置写入数据库，**重启应用后自动恢复**。

如果到点时已有任务在执行，本次会自动跳过，不会并发执行。

---

## 查看报告对比

进入 **报告对比**（`/compare`），选择两个日期。默认自动选中最近两份报告。

对比结果按事件分类：

| 标记 | 含义 |
|---|---|
| `NEW` 新增 | 新出现的事件 |
| `UPDATED` 更新 | 已有事件出现新观测 |
| `DATA_CHANGE` 数据变化 | 结构化指标发生变化 |
| `CORRECTION` 数据修正 | 来源出现数据更正 |
| `RESOLVED` 已结束 | 事件不再出现 |
| `UNCHANGED` 无变化 | 没有实质新增 |

数值变化示例：

```
device_count   8500万  →  8720万   ▲ +220万   ▲ +2.59%
```

**计算规则**：只有两边都能解析成数字时才计算变化量；基数为 0 时不计算百分比；
非数值只显示 `旧 → 新` 并标注"非数值，不计算变化率"。

颜色始终配合文字，不单独依赖颜色表达状态。

---

## 导入历史报告

原 `aios_daily.py` 生成的 `AIOS监测日报-YYYY-MM-DD.json` 可以导入。

**网页**：设置 → 导入历史报告 → 选择文件 → 导入

**命令行**：

```bash
python scripts/import_report.py AIOS监测日报-2026-09-15.json
python scripts/import_report.py "*.json" --overwrite
```

会导入：报告、栏目、条目、来源证据。

旧数据没有跨日事件历史，因此每条旧条目会生成一个标记为 `legacy_import = true` 的
单观测事件。这是对旧数据真实情况的如实表示——不会假装它有本来不存在的时间线。

---

## 备份与恢复

所有历史都在一个 SQLite 文件里，请定期备份。

**网页**：设置 → 数据与备份 → "创建备份（数据库 + 报告）"，生成
`aios-backup-YYYYMMDD-HHMMSS.zip`，可直接点击下载。

**命令行**：

```bash
python scripts/backup.py
python scripts/backup.py --output D:\backups
python scripts/backup.py --no-reports      # 只备份数据库
```

备份使用 SQLite 官方 backup API，**应用运行时也能安全备份**（普通文件复制在 WAL
模式下可能拿到不一致的快照）。

### 恢复

1. 停止应用
2. 解压备份
3. 用 `aios.db` 覆盖 `data/aios.db`
4. 删除残留的 `data/aios.db-wal` 和 `data/aios.db-shm`
5. 如需恢复导出文件，把 `reports/` 覆盖回 `data/reports/`
6. 重新启动

---

## 升级

```bash
git pull                                  # 或覆盖新版本文件
python -m pip install -r requirements.txt
python app.py
```

数据库结构变更由 Alembic 管理：

```bash
alembic upgrade head                      # 应用迁移
alembic revision --autogenerate -m "..."  # 改了模型后生成迁移
alembic check                             # 检查模型与迁移是否一致
```

全新数据库由应用自动创建并 stamp 到最新版本，无需手动操作。

**升级前请先备份。**

---

## 架构

```
aios/
├── app.py                应用工厂 + 生命周期
├── config.py             路径与进程级配置（通过 get_paths() 延迟解析）
├── database.py           引擎 / 会话 / 初始化
├── timeutil.py           统一时间处理（存 UTC，显示本地时间）
├── logging_setup.py      日志 + API Key 脱敏过滤器
├── web.py                模板环境、过滤器、flash
│
├── models/               SQLAlchemy ORM（19 张表）
│   ├── monitoring.py     MonitorModule / Topic / SearchQuery / PreferredSource / ExcludedKeyword
│   ├── providers.py      LLMProviderConfig / TaskModelRoute
│   ├── intelligence.py   RawArticle / IntelligenceEvent / EventObservation / ObservationSource
│   ├── reports.py        Report / ReportSection / ReportItem
│   ├── runs.py           MonitoringRun / ModuleRun / RunLog / LLMUsage
│   └── settings.py       AppSetting
│
├── schemas/              Pydantic 校验
├── repositories/         数据库访问层
├── routers/              HTTP 层（只处理请求/响应）
├── services/             业务逻辑
│   ├── collector.py              GDELT / Google News（SourceCollector 接口）
│   ├── article_extractor.py      正文抓取 + URL 规范化 + 哈希
│   ├── deduplicator.py           URL / 内容 / 标题三层去重
│   ├── source_scoring.py         来源可信度（纯规则，模型不参与打分）
│   ├── llm/                      可替换的模型层
│   │   ├── base.py               LLMProvider 抽象、LLMResponse、错误体系、JSON 解析
│   │   ├── presets.py            服务商预设（国内优先）
│   │   ├── openai_compatible.py  /chat/completions 适配器（多数厂商共用）
│   │   ├── anthropic.py          Claude Messages API 适配器
│   │   ├── gemini.py             Gemini generateContent 适配器
│   │   ├── registry.py           预设 -> 适配器 的工厂
│   │   └── service.py            LLMService：任务路由、凭据、用量
│   ├── intelligence_analyzer.py  Topic / Module / 综合分析 Prompt
│   ├── event_matcher.py          规则 + LLM 两阶段事件归并
│   ├── diff_engine.py            事件级结构化差分
│   ├── report_generator.py       持久化 + HTML + JSON 审计
│   ├── pipeline.py               完整监测流水线
│   ├── run_manager.py            后台执行与取消
│   ├── scheduler.py              APScheduler 定时任务
│   ├── importer.py               历史报告导入
│   ├── backup.py                 备份
│   ├── provider_migration.py     旧版 DeepSeek 配置迁移
│   ├── startup_service.py        Windows 自动启动
│   ├── seed.py                   首次启动种子数据
│   ├── settings_service.py       设置读写
│   └── keyring_service.py        凭据存储 + 脱敏
│
├── templates/            Jinja2
└── static/               CSS + HTMX + 少量原生 JS
```

### 职责边界

- **Router** 只处理 HTTP；**Service** 处理业务；**Repository** 处理数据库访问。
- 业务代码（主题分析、事件归并、报告综合）只依赖 `LLMService`，
  不认识任何厂商 SDK，也看不到厂商的响应格式。
- 厂商差异只存在于 `services/llm/` 内部：预设负责默认值与能力标记，
  适配器负责各自的报文格式。
- `LLMProvider` 不知道 HTML；`ReportGenerator` 不发 HTTP 请求；`Collector` 不碰数据库。
- 采集配置全部来自数据库，Python 代码中没有任何硬编码的模块/主题/检索式。

### 并发模型

流水线在**后台线程**中运行（`ThreadPoolExecutor`，单 worker），采集阶段用线程池并行
抓取。SQLite 开启 WAL，每个工作单元使用独立 session，ORM 对象不跨 session 传递——
只传 id。

> 实现注记：流水线**不会**在已打开的事务中再开一个 session。SQLite 的写锁会让嵌套写入
> 阻塞到 `busy_timeout` 超时，日志和用量记录因此改为先缓冲、事务关闭后再落库。

### 事实与研判分离

每条情报的 `fact_summary`（事实摘要）与 `assessment`（情报判断）在数据模型、Prompt、
HTML、对比页面中**始终分开**。Prompt 强制要求：

- 只能使用提供的证据，证据没写的数字/日期/因果一律不得补全
- 判断必须使用"判断/预计/值得关注/可能"等措辞
- 证据不足时允许返回空数组，**绝不为了填满栏目而编造事件**
- 结构化指标只能提取证据原文中明确出现的数字

没有可引用来源的条目会被直接丢弃，不会进入日报。

---

## 测试

```bash
python -m pytest tests/ -q
```

当前 **206 个测试全部通过**，不需要真实 DeepSeek API 或外网——
所有 LLM 调用和网络请求都被 mock。

覆盖范围：

| 文件 | 内容 |
|---|---|
| `test_database_and_seed.py` | 建库、种子数据、不覆盖用户配置 |
| `test_collection.py` | URL 规范化、三层去重、来源打分、正文抽取 |
| `test_event_matcher.py` | 分词、候选筛选、LLM 归并、离线规则回退 |
| `test_diff_engine.py` | 数值差分规则（含 0 基数、非数值、方向） |
| `test_security_and_client.py` | Key 脱敏/存储、**全盘扫描数据库确认无明文 Key**、重试与错误分类 |
| `test_pipeline_e2e.py` | 完整流水线、跨日事件连续性、失败隔离、去重 |
| `test_web.py` | 全部页面、CRUD、设置、转义（XSS） |
| `test_routes_exist.py` | 实际请求每一个按钮的目标，确认没有死按钮 |
| `test_importer.py` | 旧版 JSON 导入 |
| `test_llm_providers.py` | 预设、三种适配器、JSON 修复重试、任务路由、凭据隔离、**全盘扫描确认无明文 Key** |
| `test_startup_service.py` | 启动命令生成、两种机制的启用/关闭、重复启动防护、`--no-browser` |

---

## 已知限制

1. **事件归并依赖 LLM 判断。** 规则先把候选缩小到同模块/同主题、近期活跃且有词汇重合的
   少数事件，再由 DeepSeek 决定 MATCH 还是 NEW_EVENT。置信度低于 0.6 的匹配会被拒绝——
   宁可拆分事件也不错误合并（拆分可恢复，错误合并会污染时间线）。模型不可用时退回纯词汇
   重合度规则，阈值 0.55。尚未使用 embedding。

2. **采集源目前只有 GDELT 和 Google News RSS。** `SourceCollector` 接口已经就位，
   增加 RSS / 厂商新闻中心 collector 不需要改动流水线或 UI，但这些 collector 本身尚未实现。

3. **Google News 的 RSS 链接是跳转地址**，正文抓取会跳过它们，这类条目只有标题和摘要作为证据，
   来源打分也会相应降低。

4. **正文抓取是尽力而为。** 反爬、付费墙、纯 JS 渲染的页面会抓不到正文，该文章降级为
   仅摘要，不会导致失败。

5. **单次运行是串行的。** 模块按顺序执行（同一主题内的检索并行）。8 个模块全开时，
   一次完整监测通常需要若干分钟，主要耗时在 DeepSeek 调用。

6. **Token 用量依赖接口返回。** 如果 DeepSeek 响应不含 `usage`，界面显示 `—` 而不是估算值。
   暂未内置价格换算。

7. **单用户本地应用。** 没有登录、没有权限系统、没有多用户隔离，不适合直接暴露到公网。

8. **服务商预设是默认值，不是保证。** 各厂商的 Base URL、模型名与鉴权方式可能变化。
   预设只负责把常用值填好；真正的判据是「测试连接」是否通过。

9. **只覆盖 `/chat/completions` 语义。** 多模态、流式输出、工具调用、结构化 Schema
   约束尚未接入——AIOS 目前只需要「给一段提示词，返回一个 JSON 对象」。

10. **未做模型能力探测。** 例如某个模型是否真的支持 `response_format`，
    以预设中的 `supports_json_mode` 为准；若厂商行为变化，可能需要手动调整。
    JSON 解析失败时有修复重试兜底，不会因此丢失整次监测。

11. **未内置价格换算。** 用量按服务商与模型记录 token 数，但不做费用估算。

12. **Windows 自动启动依赖登录会话。** 用户不登录就不会启动；
    这是启动文件夹与 ONLOGON 任务的共同限制。

8. **`RawArticle` 会持续增长。** 按 URL 去重，同一篇文章不会重复存储，但长期运行后表会变大。
   目前没有自动清理策略，需要时可手动清理旧记录。

---

## 与原项目的关系

原 `aios_daily.py` 及其生成的 HTML/JSON 都保留在仓库中，未被删除。
新系统复用了它已经验证可用的部分：

| 原实现 | 现在的位置 |
|---|---|
| `collect_gdelt` / `collect_google_news` | `services/collector.py` |
| `fetch_article_text` | `services/article_extractor.py` |
| `dedupe` / `title_similarity` | `services/deduplicator.py` |
| `source_score` / `TRUST_HINTS` | `services/source_scoring.py` |
| `call_deepseek`（重试 + JSON 模式） | `services/llm/openai_compatible.py` + `llm/base.py` |
| 分析 Prompt（事实/研判分离） | `services/intelligence_analyzer.py` |
| `CSS` / `render_html` 卡片版式 | `services/report_generator.py` |
| `TRACKS` 常量 | 数据库种子数据 `services/seed.py` |
| `DEEPSEEK_*` 环境变量 / 设置 | `LLMProviderConfig` 表（自动迁移） |

日报的视觉特征（深蓝渐变 Header、白色卡片、Tag、今日头条、趋势研判、数据速览）
完整保留，并新增了 confidence、可点击来源链接和 NEW/UPDATED 事件状态。
打印样式也做了保留处理。
