# GoldMacroAI V1.0

AI 黄金宏观分析交易助手：用免费数据源监控美联储政策预期、实际利率、美元和经济数据，用 Python 规则计算宏观方向，用本地 AI（Ollama）解释，并通过 Telegram 推送日报与异动提醒。

> ⚠️ **重要边界**：V1.0 的评分是未经历史回测的初始规则模型，只描述宏观背景，**不是实盘交易信号**。宏观方向 ≠ 交易方向 ≠ 入场信号，入场必须由价格结构确认。

## 当前本机状态

- Telegram Bot **@GoldMacroAI_helper_bot** 已通过本机 Telegram Desktop 的 `@BotFather` 创建并完成 `/start`，Token 与 chat_id 已写入 `.env`，推送通道为 `telegram`。
- 本机使用系统代理（`127.0.0.1:7890`），`config.py` 会自动读取 Windows 代理设置；FRED 采集在本机通过 curl 回退完成，无需手动配置。
- ntfy 主题 `NTFY_TOPIC` 作为备用通道保留；配置 Telegram 后优先走 Telegram。

> 提示：`.env` 含密钥，不要公开或提交到仓库；重新分发时请从 ZIP 解压并重新生成本地配置。

## GitHub 云端定时部署

- 仓库：<https://github.com/lyjiiiirr-crypto/GoldMacroAI>（Public）
- `.github/workflows/gold-macro.yml`：每 6 小时（UTC）自动运行一次，也可在 Actions 页面点 **Run workflow** 手动触发。
- 所需 Secrets（Settings → Secrets and variables → Actions）：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`NTFY_TOPIC`。密钥只存在 GitHub Secrets，不进代码库。
- 每次运行后历史数据库 `database/gold_macro.db` 会自动提交回仓库（`.gitignore` 默认忽略，工作流中强制添加）。
- 修改代码后直接 `git push` 即可，GitHub Actions 会自动使用仓库里的 Secrets。

## 零配置模式（不需要任何账号）

- **FRED 数据**：未配置 `FRED_API_KEY` 时自动回退到 FRED 官方公开 CSV 接口（`fredgraph.csv`），无需注册。配置免费 Key 后会自动优先使用官方 JSON API。Windows 下若 urllib 因代理超时，会自动改用系统自带的 `curl.exe` 回退。
- **推送通知**：未配置 Telegram 时，可用免注册的 [ntfy.sh](https://ntfy.sh)：
  ```powershell
  python main.py --setup-ntfy
  ```
  命令会生成随机 topic、写入 `.env`，并打印订阅网址；用浏览器或 ntfy App 打开该网址就能收到报告。topic 相当于密码，不要公开。
- **Telegram**：Bot Token 必须通过你自己的 Telegram 账号向 `@BotFather` 申请（需要手机验证码，无法代注册）。拿到 Token 后可自动查 chat_id：
  ```powershell
  python main.py --telegram-id --token "你的Bot Token"
  ```
- **网络代理**：`config.py` 会自动读取 Windows 系统代理（HKCU Internet Settings），无需手动设置环境变量；也可在 `.env` 中用 `HTTPS_PROXY` 覆盖。

## 双击运行（不想用命令行）

项目根目录已提供三个批处理文件，直接双击即可，窗口不会闪退（结束时会停住让你看结果）：

| 文件 | 作用 |
|---|---|
| `双击运行_演示报告.bat` | 用演示数据输出完整报告，无需任何密钥 |
| `双击运行_日报并推送.bat` | 采集数据、生成日报并推送到 Telegram / ntfy |
| `双击运行_安装环境.bat` | 自动创建虚拟环境、安装依赖、生成 `.env` |

如需手动操作，在项目文件夹地址栏输入 `cmd` 回车，再执行 `python main.py --demo` 等命令。

## 已实现功能

| 模块 | 说明 |
|---|---|
| FRED 数据采集 | DFII10、DGS2、DGS10、T10YIE、DTWEXBGS、DFF、PCEPI、PCEPILFE、ICSA；官方 API 优先，无 Key 时走公开 CSV |
| FedWatch 自动采集 | 读取社区公开归档的每日 CME FedWatch 快照，计算 7 日"鹰鸽净变化"，保留人工导入覆盖通道 |
| DXY 自动采集 | Yahoo Finance DX-Y.NYB；限流时自动改用 ECB 汇率按 DXY 权重计算近似值 |
| BLS 自动采集 | CPI 同比 / 核心 CPI 同比 / 非农就业月度变动，API v1 免 Key |
| FOMC 文件索引 | 自动索引 Statement / Minutes / Press Conference / SEP / 点阵图的官方链接 |
| CFTC COT 自动采集 | 官方年度 ZIP，解析 COMEX 黄金 Managed Money 净多头、多空持仓与未平仓量 |
| WGC ETF 自动采集 | 公开图表接口，按周汇总全球持仓吨数与资金流，保留分项数据 |
| 宏观评分引擎 | 100 分制：政策预期 25 / 实际利率 25 / 美元 20 / 经济数据 15 / 资金流向 15 |
| 方向分级 | ≥60 强烈偏多，≥25 温和偏多，-24~24 中性震荡，≤-25 温和偏空，≤-60 强烈偏空 |
| 时效性控制 | 每条数据记录观测日期；过期数据不参与打分，并降低置信度 |
| AI 解释 | 本地 Ollama → 云端兼容接口（可选）→ 规则模板，三级自动降级 |
| Telegram 推送（或免注册 ntfy） | 日报 + 方向变化 / 降息预期变化 / 实际利率异动 / 美元异动 / 数据意外提醒，带冷却去重 |
| 主动查询命令 | `/now` `/fed` `/rates` `/dxy` `/calendar` `/cot` `/bias` `/report` `/help` |
| 定时任务 | GitHub Actions 每 6 小时运行，历史数据库自动保存回仓库 |
| 可视化 | 可选 Streamlit 仪表盘（查看历史评分与最新数据） |

**V1.0 仍不做/限制**：CME 官方 FedWatch API（收费，本项目使用社区公开归档，非官方承诺）、市场一致预期（BLS/BEA 不发布，故不伪造"意外值"，仍可人工导入）、秒级行情监控、交易结构识别（V2.0）。

### 数据源与归属（全部免费、无需付费 API）

| 数据 | 来源 | 说明 |
|---|---|---|
| 美债收益率、实际收益率、通胀预期、美元广义指数、PCE、初请失业金 | FRED 公开 CSV / 官方 API | PCE 原始数据来自 BEA，初请来自美国劳工部 |
| FedWatch 概率 | [zuowood1234/cme-fedwatch-tracker](https://github.com/zuowood1234/cme-fedwatch-tracker) 每日公开归档 | 原始数据来自 CME FedWatch 页面；CME 官方 API 为付费服务，本项目并非 CME 官方产品 |
| DXY | Yahoo Finance `DX-Y.NYB`；回退 [Frankfurter](https://frankfurter.dev/) + ECB 参考汇率 | 回退值为按 ICE 权重计算的近似值，与 Yahoo 实测相差约 0.1%，报告中标注实际来源 |
| CPI / 核心 CPI / 非农 | BLS Public Data API v1 | 只取官方实际值，不虚构一致预期；同比/环比由项目本地派生 |
| FOMC 文件 | [federalreserve.gov](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) | 只保存标题、日期与官方链接，不转载正文；点阵图在 SEP 页面中 |
| COT | [CFTC 官方历史 ZIP](https://www.cftc.gov/files/dea/history/) | COMEX 黄金 Managed Money 净多头；每周发布日期滞后约 3 天 |
| 黄金 ETF | [World Gold Council](https://www.gold.org/goldhub/data/gold-etfs-holdings-and-flows) 公开图表接口 | 版权归 WGC；XLSX 匿名下载受限，改用官网自身调用的 JSON |

## 目录结构

```text
GoldMacroAI/
├── main.py                     命令行入口
├── config.py                   配置（环境变量 / .env）
├── requirements.txt            核心依赖（实际为空骨架，可安装可选增强）
├── requirements-dashboard.txt  仪表盘可选依赖
├── .env.example
├── data/                       fred.py / common.py / fedwatch.py / fedwatch_auto.py
│                               dxy.py / macro_auto.py / cot.py / etf.py / fomc.py / calendar.py
├── analysis/                   data_cleaner.py / macro_score.py / gold_bias.py / ai_interpreter.py
├── alerts/                     telegram_bot.py / message_templates.py / alert_rules.py
├── database/                   models.py（SQLite，运行时生成 gold_macro.db）
├── scheduler/jobs.py           完整任务流程
├── dashboard/app.py            可选 Streamlit 仪表盘
├── tests/                      单元测试
└── .github/workflows/gold-macro.yml
```

## 一、本地 Windows 部署（零成本）

### 1. 准备

- Python 3.10+（建议 3.11）
- 可选：免费 FRED API Key：<https://fredaccount.stlouisfed.org/apikeys>（不填也能用公开 CSV）
- 可选：Telegram Bot（找 `@BotFather` 发 `/newbot`）或免注册 ntfy（运行 `--setup-ntfy`）

### 2. 安装

在项目根目录打开 CMD / PowerShell：

```powershell
py -3.11 -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

编辑 `.env`（全部可留空，留空时自动用免注册方案）：

```env
FRED_API_KEY=你的FRED密钥
TELEGRAM_BOT_TOKEN=你的Telegram机器人密钥
TELEGRAM_CHAT_ID=你的Telegram用户ID
NTFY_TOPIC=
```

> 核心功能只用 Python 标准库；不装 `requirements.txt` 也能运行。

### 3. 先验证，再推送

```powershell
python -m unittest discover -s tests -v      # 运行单元测试
python main.py --demo                        # 无密钥查看完整报告样式
python main.py --dry-run                     # 联网采集 + 打印报告，不推送、不落库
python main.py --send-now                    # 联网采集 + 推送 Telegram + 保存历史
python main.py --status                      # 查看数据库统计
```

### 4. 自动同步与人工数据

每次 `--dry-run` / `--send-now` / `--scheduled` 都会自动采集上面列出的全部免费数据源；
只想同步入库、不生成报告时运行：

```powershell
python main.py --sync-only       # 全部来源采集并写入 SQLite，打印各来源结果
python main.py --status          # 查看各表记录数
```

人工数据仍可作为补充或回退（自动数据会被同日期的人工快照覆盖）：

```powershell
# CME FedWatch：页面免费查看后录入当日概率快照
python main.py --import-fedwatch --date 2026-11-22 --cut 65 --hold 35 --hike 0 --meeting 2026-12-16

# 经济数据：实际值 / 预期值（scale 为满信号幅度，CPI 建议 0.3 个百分点）
python main.py --import-event --name CPI --actual 2.8 --expected 2.9 --previous 3.0 --scale 0.3 --date 2026-11-12

# 初请失业金等"数值越高越利多黄金"的数据要加 --direction inverse
python main.py --import-event --name 初请失业金 --actual 250000 --expected 200000 --scale 25000 --direction inverse --date 2026-11-19

# 资金流向：COT 净多头或 ETF 持仓（scale 为满信号变化量）
python main.py --import-flow --type COT --value 250000 --scale 30000 --date 2026-11-14
python main.py --import-flow --type ETF --value 1100 --scale 50 --date 2026-11-14
```

未来 24 小时经济事件：复制 `data/calendar.example.json` 为 `data/calendar.json` 并按自己的日历修改。

### 5. 本地 AI（可选）

```powershell
# 安装 https://ollama.com/download/windows 后：
ollama pull qwen2.5:3b
```

在 `.env` 中设置：

```env
OLLAMA_ENABLED=true
OLLAMA_MODEL=qwen2.5:3b
```

本地不可用时可设置 `CLOUD_AI_BASE_URL` / `CLOUD_AI_API_KEY` 使用兼容 OpenAI 格式的免费接口；两者都不可用会自动退回规则模板。

### 6. 主动查询机器人

```powershell
python main.py --listen
```

在 Telegram 中发送 `/help` 查看命令。

## 二、部署到 GitHub 免费云端

1. 创建仓库（本项目的实际仓库为 Public，默认分支 `main`）。
2. 上传项目文件，**不要上传 `.env`、`venv`、`database/*.db`**。
3. `Settings → Secrets and variables → Actions` 添加：
   - `FRED_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - （可选）`CLOUD_AI_BASE_URL` / `CLOUD_AI_API_KEY` / `CLOUD_AI_MODEL`
4. 进入 `Actions`，手动运行 `GoldMacroAI scheduled run` 验证。
5. 之后每 6 小时（UTC）自动运行一次；数据库提交回私有仓库。

**云端注意**：GitHub Actions 不是常驻服务器，定时任务可能延迟；本地 Ollama 地址 `localhost:11434` 云端访问不到，云端默认使用规则分析。免费额度（私有仓库每月 2000 分钟 Actions）需自行确认。

## 三、评分规则（可调，未回测）

- 实际利率：DFII10 五日变化每下降 20bp → +1（利多黄金）；上升 → 负分。
- 美元：优先使用 DXY（Yahoo / ECB 近似）五日变化，缺失时回退 DTWEXBGS；每下降 1.5% → +1。
- 政策预期：FedWatch 下一次会议概率的七日变化每上升 15 个百分点 → +1；当加息概率可得时使用鹰鸽净变化 `(降息变化) - (加息变化)`，避免漏掉"降息不变、加息下滑"的偏鸽信号。
- 经济数据：实际值高于预期视为偏鹰（利空黄金），按 `scale` 归一化；只使用已发布数据，避免用修订值冒充当时信息。
- 资金流向：COT 净多头/ETF 持仓变化上升 → 偏多。
- 数据过期或缺失时该模块信号归零并降低置信度；有效权重低于 40% 时输出「数据不足」，不强给方向。

调整幅度阈值见 `config.py` 的 `FULL_SCALE`，权重见 `WEIGHTS`。

## 四、常见问题

- **报告显示「数据不足」**：先运行 `--sync-only` 看每个来源的成功/失败原因。若某个来源被网络限制，其他来源不受影响；仍可用 `--import-fedwatch` / `--import-event` / `--import-flow` 手工补齐。
- **FedWatch 降息概率一直是 0%**：当前周期市场定价主要是"加息 vs 不变"，系统会自动改用鹰鸽净变化评分；报告里同时显示加息概率。
- **收不到推送**：没配置 Telegram 时先运行 `--setup-ntfy` 获取订阅网址；配置 Telegram 时确认已给机器人发过 `/start`、chat_id 正确（`--telegram-id` 可查），Token 未填错。
- **AI 解读来源显示「规则模板」**：本地 Ollama 未启动或云端接口未配置，系统自动降级，不影响数据采集与推送。
- **GitHub 数据库没更新**：确认仓库为私有且 Actions 有 `contents: write` 权限；工作流使用 `git add -f` 忽略本地 `.gitignore` 规则。

## 五、后续版本

V1.1：市场一致预期的免费合规来源（用于真正的 CPI/PCE/NFP 意外值）；V2.0：TradingView 结构联动与 HTF MSNR + SMC 交易过滤。
