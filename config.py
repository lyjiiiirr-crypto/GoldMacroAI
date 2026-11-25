"""GoldMacroAI V1.0 全局配置。

密钥只从环境变量或项目根目录的 .env 读取，切勿提交到仓库。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("GOLD_MACRO_DB", str(BASE_DIR / "database" / "gold_macro.db")))


def load_dotenv(path: Path | str | None = None) -> None:
    """极简 .env 读取器：不覆盖已有环境变量，无需第三方依赖。"""
    path = Path(path) if path else BASE_DIR / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def _configure_system_proxy() -> None:
    """Windows 下自动采用系统代理，避免本机网络访问 Telegram / FRED 失败。

    只使用标准库；已在环境变量中显式设置代理时不覆盖。非 Windows 平台跳过。
    """
    if os.name != "nt" or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
        return
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if not enabled or not server:
            return
        server = str(server).strip()
        if "=" in server:  # 形如 http=host:port;https=host:port
            mapping = dict(
                part.split("=", 1) for part in server.split(";") if "=" in part
            )
            http = mapping.get("http")
            https = mapping.get("https") or http
        else:
            http = https = server
        if https and "://" not in https:
            https = "http://" + https
        if http and "://" not in http:
            http = "http://" + http
        if https:
            os.environ["HTTPS_PROXY"] = https
        if http:
            os.environ["HTTP_PROXY"] = http
    except Exception:  # noqa: BLE001 - 代理探测失败不应影响程序启动
        pass


_configure_system_proxy()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


# ---- 数据源密钥 ----
FRED_API_KEY = env("FRED_API_KEY")
FRED_BASE_URL = env("FRED_BASE_URL", "https://api.stlouisfed.org/fred/series/observations")
# 无 API Key 时的公开 CSV 回退接口（FRED 官方图表数据，无需注册）
FRED_CSV_BASE_URL = env("FRED_CSV_BASE_URL", "https://fred.stlouisfed.org/graph/fredgraph.csv")

# ---- Telegram ----
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")
TELEGRAM_API_BASE = env("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
# 免注册推送备选：ntfy.sh（订阅 https://ntfy.sh/<你的topic> 即可收到）
NTFY_TOPIC = env("NTFY_TOPIC")
NTFY_BASE_URL = env("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")
ALERT_COOLDOWN_HOURS = int(env("ALERT_COOLDOWN_HOURS", "12"))

# ---- AI 引擎：本地 Ollama 优先，兼容 OpenAI 格式的云端接口备用 ----
OLLAMA_ENABLED = env("OLLAMA_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
OLLAMA_HOST = env("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = env("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = int(env("OLLAMA_TIMEOUT", "60"))
CLOUD_AI_BASE_URL = env("CLOUD_AI_BASE_URL").rstrip("/")
CLOUD_AI_API_KEY = env("CLOUD_AI_API_KEY")
CLOUD_AI_MODEL = env("CLOUD_AI_MODEL", "qwen/qwen3-8b:free")
CLOUD_AI_TIMEOUT = int(env("CLOUD_AI_TIMEOUT", "30"))

# ---- 宏观序列（含 FRED / Yahoo / BLS 等多种免费来源）----
SERIES = {
    "DFII10": {"name": "10年期实际收益率", "unit": "%", "stale_days": 10, "source": "fred"},
    "DGS2": {"name": "2年期国债收益率", "unit": "%", "stale_days": 10, "source": "fred"},
    "DGS10": {"name": "10年期国债收益率", "unit": "%", "stale_days": 10, "source": "fred"},
    "T10YIE": {"name": "10年期通胀预期", "unit": "%", "stale_days": 10, "source": "fred"},
    "DTWEXBGS": {"name": "美元广义贸易加权指数", "unit": "点", "stale_days": 10, "source": "fred"},
    "DFF": {"name": "有效联邦基金利率", "unit": "%", "stale_days": 10, "source": "fred"},
    "DXY": {"name": "美元指数 DXY", "unit": "点", "stale_days": 10, "source": "yahoo"},
    "PCEPI": {"name": "PCE物价指数(整体)", "unit": "点", "stale_days": 100,
              "source": "fred", "agency": "BEA", "change_label": "月度变化"},
    "PCEPILFE": {"name": "核心PCE物价指数", "unit": "点", "stale_days": 100,
                 "source": "fred", "agency": "BEA", "change_label": "月度变化"},
    "ICSA": {"name": "初请失业金人数", "unit": "千人", "stale_days": 14,
             "source": "fred", "agency": "美国劳工部", "change_label": "周度变化"},
    "CPI_YOY": {"name": "CPI同比", "unit": "%", "stale_days": 100,
                "source": "bls", "change_label": "月度变化"},
    "CORE_CPI_YOY": {"name": "核心CPI同比", "unit": "%", "stale_days": 100,
                     "source": "bls", "change_label": "月度变化"},
    "NFP_CHG": {"name": "非农就业变动", "unit": "千人", "stale_days": 100,
                "source": "bls", "change_label": "月度变化"},
}

# 新增免费数据源的公开入口（可在 .env 覆盖，便于换镜像）
BLS_API_URL = env("BLS_API_URL", "https://api.bls.gov/publicAPI/v1/timeseries/data/")
CFTC_HISTORY_URL = env("CFTC_HISTORY_URL", "https://www.cftc.gov/files/dea/history/")
FOMC_CALENDAR_URL = env("FOMC_CALENDAR_URL", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
WGC_API_BASE = env("WGC_API_BASE", "https://fsapi.gold.org/api/v11/charts/etfv2/revised")
FEDWATCH_ARCHIVE_URL = env(
    "FEDWATCH_ARCHIVE_URL",
    "https://raw.githubusercontent.com/zuowood1234/cme-fedwatch-tracker/main/data/daily/{date}.json",
)
# Polymarket 利率预测：公开 Gamma API，无需密钥；仅作报告上下文
POLYMARKET_GAMMA_URL = env("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/")
YHOO_DXY_URL = env(
    "YHOO_DXY_URL",
    "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=1mo&interval=1d",
)
# 技术面报告：Yahoo Finance 公开行情 + 本地规则计算（非 Trading Central 官方研报）
YHOO_TECH_URL = env(
    "YHOO_TECH_URL",
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d",
)
TECHNICAL_SYMBOLS = tuple(
    s.strip() for s in env("TECHNICAL_SYMBOLS", "GC=F").split(",") if s.strip()
)
# XAUS 免费公开实时行情（免密钥、GET、带 data_state 新鲜度校验）
# chart 提供 60m 已收盘 OHLC（4H 结构高低点），spot 提供临近收线的实时价（P3 收盘）
# history 提供已定稿日线；intraday 5 分钟采样保留为备用参考
XAUS_INTRADAY_URL = env(
    "XAUS_INTRADAY_URL",
    "https://xaus.com/api/v1/intraday?symbol={symbol}&hours={hours}",
)
XAUS_CHART_URL = env(
    "XAUS_CHART_URL",
    "https://xaus.com/api/v1/chart?symbol={symbol}&range={range}&interval={interval}",
)
XAUS_HISTORY_URL = env(
    "XAUS_HISTORY_URL",
    "https://xaus.com/api/v1/history?days={days}",
)
XAUS_SPOT_URL = env(
    "XAUS_SPOT_URL",
    "https://xaus.com/api/v1/spot?compact=1",
)
# 4H CRT（蜡烛区间理论）：优先 XAUS 60m P1/P2 结构 + TradingView 已收盘 P3，失败回退 Yahoo
TV_CRT_URL = env(
    "TV_CRT_URL",
    "https://scanner.tradingview.com/global/scan",
)
TV_CRT_SYMBOL = env("TV_CRT_SYMBOL", "OANDA:XAUUSD")
YHOO_CRT_URL = env(
    "YHOO_CRT_URL",
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval={interval}",
)
CRT_SYMBOLS = tuple(
    s.strip() for s in env("CRT_SYMBOLS", "XAUUSD").split(",") if s.strip()
)
# GEX / Gamma flip：Cboe 免费延迟期权报价（GLD 黄金代理 + SPY 大盘），延迟近似估算
# Cboe 单次请求即返回整条链（含 open_interest / gamma / iv），比 Yahoo 免费链更完整
CBOE_OPTIONS_URL = env(
    "CBOE_OPTIONS_URL",
    "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json",
)
# Yahoo 期权链仅保留给历史/手动导入兼容（现网 open_interest 常为 0，不作为自动源）
YHOO_OPTIONS_BASE_URL = env(
    "YHOO_OPTIONS_BASE_URL",
    "https://query1.finance.yahoo.com/v7/finance/options/{symbol}",
)
YHOO_OPTIONS_URL = env(
    "YHOO_OPTIONS_URL",
    "https://query1.finance.yahoo.com/v7/finance/options/{symbol}?date={date}",
)
GEX_SYMBOLS = tuple(
    s.strip() for s in env("GEX_SYMBOLS", "GLD,SPY").split(",") if s.strip()
)
# 只取近月到期日：近月 gamma 占主导，避免远月噪声；可按需调高。
GEX_MAX_DAYS = int(env("GEX_MAX_DAYS", "90"))
GEX_GRID_STEPS = int(env("GEX_GRID_STEPS", "161"))
# Yahoo 限流时使用欧洲央行公开汇率按 ICE DXY 权重计算的近似值（免费、无 Key）
FRANKFURTER_URL = env(
    "FRANKFURTER_URL",
    "https://api.frankfurter.dev/v1/{start}..?base=USD&symbols=EUR,JPY,GBP,CAD,SEK,CHF",
)

# ---- 100 分制评分权重 ----
WEIGHTS = {
    "fed_policy": 25,   # 利率预期变化（FedWatch 人工快照）
    "real_rate": 25,    # DFII10 变化
    "dollar": 20,       # DTWEXBGS 变化（非 ICE DXY）
    "econ_data": 15,    # CPI / PCE / NFP 意外值（人工导入）
    "flows": 15,        # COT / ETF 资金流向（人工导入）
}

# 模块达到满信号所需的变动幅度（可调，未回测）
FULL_SCALE = {
    "fed_policy_pp": 15.0,   # 降息概率变化 15 个百分点
    "real_rate_bp": 20.0,    # 实际收益率变化 20 个基点
    "dollar_pct": 1.5,       # 美元指数变化 1.5%
}

GRADES = [
    (60, "强烈偏多"),
    (25, "温和偏多"),
    (-24, "中性震荡"),
    (-60, "温和偏空"),
]

CHINA_TIMEZONE_OFFSET_HOURS = 8
