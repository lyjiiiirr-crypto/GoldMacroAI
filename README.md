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
- **重要**：GitHub 原生 `schedule` 事件在本仓库实测完全不触发（主工作流与全新探针工作流均为 0 次 schedule run，GitHub 状态页正常），因此不再依赖原生 cron。
- `.github/workflows/gold-macro-keeper.yml`：云端调度链。每次运行计算下一个 UTC 02:06 / 06:06 / 10:06 / 14:06 / 18:06 / 22:06（北京时间 10:06 / 14:06 / 18:06 / 22:06 / 02:06 / 06:06），等待到点后触发日报工作流，再用内置 `GITHUB_TOKEN` 触发下一次 keeper，形成闭环。全程在 GitHub Actions 云端完成，**不依赖本地电脑**。
- `.github/workflows/gold-macro.yml`：只保留 `workflow_dispatch`，由 keeper 触发；也可在 Actions 页面点 **Run workflow** 手动触发。带 `concurrency: gold-macro-report`，防止同一时段重复推送。
- 首次启用：在 Actions 手动运行一次 `GoldMacroAI cloud keeper`（无需输入参数），之后自动每 4 小时续链。若链因 GitHub API 故障中断，再次手动运行该工作流即可恢复。
- 所需 Secrets（Settings → Secrets and variables → Actions）：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`NTFY_TOPIC`。密钥只存在 GitHub Secrets，不进代码库；keeper 只使用内置 `GITHUB_TOKEN`，无需新建 secret。
- 每次运行后历史数据库 `database/gold_macro.db` 会自动提交回仓库（`.gitignore` 默认忽略，工作流中强制添加）。
- 修改代码后直接 `git push` 即可，GitHub Actions 会自动使用仓库里的 Secrets；keeper 每次都按 `main` 分支最新代码触发。

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
| 技术面报告（本地自算） | 黄金优先用 XAUS 免费公开实时行情的已定稿日线（免密钥、带 `data_state` 新鲜度校验），失败回退 Yahoo Finance `GC=F`；自算 SMA/RSI/MACD/ATR 与支撑阻力；**不是 Trading Central 官方研报**，保留人工导入通道 |
| GEX 地图 / Gamma 翻转点 | Cboe 免费延迟期权报价（GLD 黄金代理 + SPY 大盘），用 IV + 未平仓量按 Black-Scholes 本地估算 GEX、gamma flip 与对冲压力方向；延迟/近似值，**非交易信号、不参与宏观评分**，可人工导入 |
| 技术面 × GEX 融合方向 | 技术面（SMA/RSI/MACD）定基础方向，GEX 通过现价与 gamma flip 的相对位置给出偏向并放大/削弱，输出**偏多 / 偏空 / 中性震荡**；写入日报、`/fusion` 可查，未回测、**不参与宏观评分** |
| 4H CRT 三根结构 | 已收盘 4H K 线优先用 XAUS 免费公开实时行情的 60m OHLC 本地聚合（带 `data_state` 新鲜度校验，每根 4H 必须含满 4 根 1H）确定 P1/P2 结构；最新 P3 优先用 TradingView scanner 的已收盘 `[1]` 列覆盖，其次用 XAUS 实时现货价覆盖收盘，失败再回退 Yahoo 1H 聚合；报告发出时 P3 已收线，当前信号由 **P3 是否扫过 P2 高低点、P3 收盘是否收回 P2 区间** 判定，P1 仅作历史背景。写入「交易执行提醒」并附 3 根 K 线图，`/crt` 可查，未回测、**非交易信号** |
| Polymarket 利率预测 | 公开 Gamma API 读取利率市场当前 Yes 赔率，展示在「美联储政策预期」；**预测市场定价，非官方利率数据、不参与宏观评分**，可人工导入 |
| 宏观评分引擎 | 100 分制：政策预期 25 / 实际利率 25 / 美元 20 / 经济数据 15 / 资金流向 15 |
| 方向分级 | ≥60 强烈偏多，≥25 温和偏多，-24~24 中性震荡，≤-25 温和偏空，≤-60 强烈偏空 |
| 时效性控制 | 每条数据记录观测日期；过期数据不参与打分，并降低置信度 |
| AI 解释 | 本地 Ollama → 云端兼容接口（可选）→ 规则模板，三级自动降级 |
| Telegram 推送（或免注册 ntfy） | 日报 + 方向变化 / 降息预期变化 / 实际利率异动 / 美元异动 / 数据意外提醒，带冷却去重 |
| 主动查询命令 | `/now` `/fed` `/rates` `/dxy` `/calendar` `/cot` `/tech` `/gex` `/fusion` `/crt` `/bias` `/report` `/help` |
| 定时任务 | GitHub 云端 keeper 链每 4 小时触发，在 TradingView 4H 收线后约 6 分钟（北京 10:06/14:06/18:06/22:06/02:06/06:06）采集，历史数据库自动保存回仓库；不依赖本地电脑 |
| 可视化 | 可选 Streamlit 仪表盘（查看历史评分与最新数据） |

**V1.0 仍不做/限制**：CME 官方 FedWatch API（收费，本项目使用社区公开归档，非官方承诺）、Trading Central 授权研报（商业/B2B 产品，无可验证免费公开源，本项目改用公开行情本地自算并明确标注非官方研报）、市场一致预期（BLS/BEA 不发布，故不伪造"意外值"，仍可人工导入）、秒级行情监控、交易结构识别（V2.0）。

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
| 黄金技术面 | XAUS 免费公开实时行情历史日线（`XAUS_HISTORY_URL`），失败回退 Yahoo Finance `GC=F`（可配置 `TECHNICAL_SYMBOLS`） | 只取已定稿日线，盘中未收盘根会被丢弃；SMA / RSI / MACD / ATR 与支撑阻力由本项目纯 Python 计算；**并非 Trading Central 产品，也不转载其授权内容**，仅供研究 |
| GEX / Gamma flip | [Cboe 免费延迟期权报价](https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json) `GLD`、`SPY`（`GEX_SYMBOLS` 可配置） | 免费公开接口，约 15 分钟延迟，单次请求返回整条链（含未平仓量 / IV / gamma）。Yahoo 免费链现网未平仓量常为 0，故改用 Cboe；本项目用 IV 本地按 Black-Scholes 估算（IV 缺失时回退 Cboe gamma）。GEX = Γ×OI×100×S²×0.01（看涨为正、看跌为负），flip 由整条链重定价找净 GEX 过零点。**数据延迟、未含真实做市商库存，仅作研究背景，非交易信号** |
| 4H CRT 三根结构 | XAUS 60m 已收盘 OHLC（`XAUS_CHART_URL`）聚合 P1/P2 结构；TradingView scanner（`TV_CRT_SYMBOL` / `TV_CRT_URL`）的 `[1]` 已收盘列覆盖 P3，缺失时用 XAUS 现货（`XAUS_SPOT_URL`）覆盖 P3 收盘；Yahoo 1H（`YHOO_CRT_URL`）为辅 | 只接受 `data_state.status=fresh` 且 `data_state.as_of` 在允许时差内的数据；`chart` 的 upstream 收盘可能与现货偏差较大，所以只取已收盘 60m 的高低点。免费 scanner 常只返回 `[1]`（最近一根已收盘），时间戳对齐后用它覆盖 P3 的 OHLC，避免把当前未完成根当成收盘。每根 4H 必须含满 4 根已收盘 1H。按 UTC 02/06/10/14/18/22 收线（北京 10/14/18/22/02/06）；采集时点在收线后约 6 分钟；P1 仅作背景，按 P2 当前区间 + P3 扫荡/收盘收回判定；结构参考位由 P2/P3 边界给出。**近似值，未回测，非交易信号** |
| Polymarket 利率预测 | [Polymarket Gamma API](https://gamma-api.polymarket.com)（`POLYMARKET_GAMMA_URL` 可配置） | 免费公开市场元数据与 Yes 赔率，无密钥；失败时优雅降级，可用 `--import-polymarket` 人工导入。仅作报告上下文，不参与评分 |

## 目录结构

```text
GoldMacroAI/
├── main.py                     命令行入口
├── config.py                   配置（环境变量 / .env）
├── requirements.txt            核心依赖（实际为空骨架，可安装可选增强）
├── requirements-dashboard.txt  仪表盘可选依赖
├── .env.example
├── data/                       fred.py / common.py / fedwatch.py / fedwatch_auto.py
│                               dxy.py / macro_auto.py / cot.py / etf.py / fomc.py / technical.py / gex.py / crt.py / calendar.py
├── analysis/                   data_cleaner.py / macro_score.py / gold_bias.py / ai_interpreter.py / tech_gex_fusion.py
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

# 技术面报告：自动采集失败时手动录入（与自动来源按来源区分，不会互相覆盖）
python main.py --import-technical-report --date 2026-11-23 --symbol GC=F --bias 偏多 --summary "日线偏多，RSI 58，支撑 3560，阻力 3680"

# 或用 JSON 文件一次导入完整结构化报告（可含 indicators / levels / signals）
python main.py --import-technical-report --date 2026-11-23 --report-json my_technical_report.json

# GEX 快照：自动采集失败时手动录入（可传快照 JSON，也可直接传期权链原始 JSON 由本地计算）
# 快照结构（symbol/spot/net_gex/gamma_flip/regime/summary 等，来源默认 manual，与自动来源并存）
python main.py --import-gex --date 2026-11-23 --gex-json gex_snapshot.json
# 原始期权链响应（Cboe data.options 或 Yahoo optionChain.result[0]）→ 本地计算 GEX / flip
python main.py --import-gex --symbol GLD --gex-json cboe_option_chain.json --rate 0.04

# 4H CRT 三根结构：可传三根 K 线、区间/扫荡/扩张高低点，也可传 XAUS chart / Yahoo chart 响应 / 1H K 线（自动采集优先 XAUS 60m 结构 + TradingView 已收盘 P3）
python main.py --import-crt --date 2026-11-23 --crt-json crt_snapshot.json
# crt_snapshot.json 示例（三根 K 线）：
# {"candle1":{"high":3629.6,"low":3567.8,"open":3580,"close":3610},
#  "candle2":{"high":3636.4,"low":3560.2,"open":3610,"close":3615},
#  "candle3":{"high":3640.1,"low":3590.0,"open":3615,"close":3630}}

# Polymarket 利率预测：Gamma 原始响应 / 市场列表 / 已构造快照均可
python main.py --import-polymarket --polymarket-json polymarket.json
```

GEX 报告中会明确标注「Cboe 免费延迟期权报价 + 本地 Black-Scholes 估算、非做市商真实库存、非交易信号」。每条 GEX 快照还会按净 GEX 符号与现价相对 flip 的位置标注 `🟢偏多` / `🔴偏空` / `⚪中性` 并附简短理由；该标注同样**未回测、仅作背景、不参与宏观评分**。`gex_snapshots` 表按（来源、标的、日期）幂等写入，人工导入与自动采集互不覆盖。

#### 技术面 × GEX 融合方向

日报会在「技术面」与「GEX / Gamma 翻转点」之后追加一段融合结论（`/fusion` 命令可单独查询）。规则见 `analysis/tech_gex_fusion.py`，均为**初始设定、未回测**：

- **技术面方向**：技术报告 `score` 以 ±4 为满信号映射到 -1~+1；缺 `score` 时用 `bias`（偏多 / 偏空）按 0.6 折算。
- **GEX 方向**：净 GEX < 0 且现价低于 flip → 负 Gamma 放大下行，方向为负；净 GEX > 0 且现价高于 flip → 正 Gamma 托底，方向为 +0.6 倍强度；其余情形记 0。GLD 权重 0.65、SPY 权重 0.35，flip 距离 ±8% 视为满强度。
- **放大系数**：负 Gamma 随强度最高放大到约 1.5×，正 Gamma 最低压缩到约 0.6×；融合时仅按 `0.75 + 0.25 × 系数` 温和调节。
- **合成**：`0.65 × 技术方向 + 0.35 × GEX 方向`，乘调节系数后，≥ +0.2 判偏多、≤ -0.2 判偏空，其余为中性震荡。
- 该结果**只写入报告**，不进入 100 分宏观评分，也不触发交易提醒；输出固定带「延迟近似、未回测、不参与宏观评分、非交易信号」标注。

#### 交易执行提醒（4H CRT 三根结构 → 4H/15M 执行）

报告结尾的执行提醒基于 4H 三根 CRT 快照生成（`/crt` 可单独查询），并在 Telegram 文本后附一张 3 根 4H K 线图：

- **P1 仅作背景**：报告发出时 P3 已收线，第 1 根区间不再作为当前阻力/支撑或目标。
- **P2 是当前区间**：第 2 根扫荡后形成的高/低点为当前参考边界。
- **P3 扫荡与收回**：先标注 P3 高点距 P2 高点、P3 低点距 P2 低点的点数（正数仍在区间内，负数表示已扫过），再标注 P3 收盘是否收回 P2 区间；**不标注距现价**。
- **方向判定**：扫 P2 低点后收回 → 偏多反转；扫 P2 高点后收回 → 偏空反转；扫低未收回 → 偏空延续；扫高未收回 → 偏多延续；同时扫高低或两侧都未扫 → 中性待确认。
- **执行参考**：反转情形目标看 P2 另一侧、失效参考为 P3 扫荡影线；延续情形只给 P2 边界作为失效参考；均为结构位而非挂单建议。
- **融合方向**：与 4H 情景冲突时降级观望；LTF 用 4H/15M 依据 P3 扫荡与收盘收回结果，等回测确认再入场，不追价。
- **截图**：本地纯标准库生成 PNG，通过 `sendPhoto` 推送；生成/推送失败时不影响文本日报。
- 规则未回测，仅为流程清单，**非交易信号、不构成投资建议**。

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

在 Telegram 中发送 `/help` 查看命令；`/fusion` 查看融合方向，`/crt` 查看 4H CRT 三根结构与关键位。

## 二、部署到 GitHub 免费云端

1. 创建仓库（本项目的实际仓库为 Public，默认分支 `main`）。
2. 上传项目文件，**不要上传 `.env`、`venv`、`database/*.db`**。
3. `Settings → Secrets and variables → Actions` 添加：
   - `FRED_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - （可选）`CLOUD_AI_BASE_URL` / `CLOUD_AI_API_KEY` / `CLOUD_AI_MODEL`
4. 进入 `Actions`，手动运行 `GoldMacroAI scheduled run` 验证日报与 Telegram 推送。
5. 再手动运行一次 `GoldMacroAI cloud keeper` 启动调度链；之后每 4 小时自动触发（北京时间 10:06/14:06/18:06/22:06/02:06/06:06），数据库提交回仓库。

**云端注意**：GitHub 原生 `schedule` 在本仓库不可用，调度完全依赖 keeper 链（见上）。本地 Ollama 地址 `localhost:11434` 云端访问不到，云端默认使用规则分析。公开仓库的 GitHub Actions 分钟免费；若改为私有仓库需自行确认免费额度。

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
- **需要 Trading Central 官方技术分析报告吗**：Trading Central 是商业/B2B 授权研报，官网无免费公开报告接口，本项目不抓取或转载其付费内容。当前技术面优先由 XAUS 公开实时行情（失败回退 Yahoo Finance）本地自算，报告中明确标注来源；若你已有合法授权接口，可用 `--import-technical-report --report-json` 导入。
- **收不到推送**：没配置 Telegram 时先运行 `--setup-ntfy` 获取订阅网址；配置 Telegram 时确认已给机器人发过 `/start`、chat_id 正确（`--telegram-id` 可查），Token 未填错。
- **AI 解读来源显示「规则模板」**：本地 Ollama 未启动或云端接口未配置，系统自动降级，不影响数据采集与推送。
- **GitHub 数据库没更新**：确认仓库为私有且 Actions 有 `contents: write` 权限；工作流使用 `git add -f` 忽略本地 `.gitignore` 规则。

## 五、后续版本

V1.1：市场一致预期的免费合规来源（用于真正的 CPI/PCE/NFP 意外值）；V2.0：TradingView 结构联动与 HTF MSNR + SMC 交易过滤。
