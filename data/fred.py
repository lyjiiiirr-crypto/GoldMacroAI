"""FRED 数据采集（仅使用标准库）。

优先官方 API（需免费 Key）；未配置 Key 时自动回退到 FRED 官方公开 CSV
图表接口，无需注册，适合零配置启动。
"""
from __future__ import annotations

import csv
import html as html_lib
import io
import json
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from config import FRED_BASE_URL, FRED_CSV_BASE_URL, FRED_API_KEY, SERIES


class FetchError(RuntimeError):
    pass


def parse_fred_csv(text: str, series_id: str, limit: int = 400) -> list[dict]:
    """解析 fredgraph.csv：observation_date,<series_id>，跳过缺失值 '.'。"""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows = []
    for item in reader:
        raw = item.get(series_id)
        date = item.get("observation_date")
        if not date or raw in (".", "", None):
            continue
        try:
            rows.append({"date": date.strip(), "value": float(raw)})
        except (TypeError, ValueError):
            continue
    return sorted(rows, key=lambda r: r["date"], reverse=True)[:limit]


def parse_fred_html(text: str, limit: int = 10) -> list[dict]:
    """FRED 系列页面含最近 5 个观测值；CSV 不可用时的最后兜底。"""
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    pairs = re.findall(r"(\d{4}-\d{2}-\d{2})\s*:\s*(-?\d+\.?\d*)", plain)
    rows, seen = [], set()
    for date, value in pairs:
        if date in seen:
            continue
        seen.add(date)
        rows.append({"date": date, "value": float(value)})
    return sorted(rows, key=lambda r: r["date"], reverse=True)[:limit]


def _get_text(url: str, timeout: int) -> str:
    """下载文本。Windows 优先调用 curl（兼容系统代理，且不发送自定义 UA）。

    FRED 对带自定义 User-Agent 的请求可能一直挂起，因此 curl 使用默认 UA；
    代理由 config 设置的 HTTPS_PROXY / HTTP_PROXY 环境变量自动提供。
    """
    for curl_bin in ("curl.exe", "curl"):
        try:
            result = subprocess.run(
                [curl_bin, "-sSL", "-m", str(timeout), url],
                capture_output=True, timeout=timeout + 10,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8-sig", errors="replace")
        except Exception:  # noqa: BLE001 - curl 不可用时回退 urllib
            continue
    request = urllib.request.Request(url, headers={"User-Agent": "GoldMacroAI/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def fetch_observations_csv(series_id: str, limit: int = 400, timeout: int = 60) -> list[dict]:
    url = f"{FRED_CSV_BASE_URL}?id={urllib.parse.quote(series_id)}"
    try:
        rows = parse_fred_csv(_get_text(url, timeout), series_id, limit=limit)
        if rows:
            return rows
    except Exception:  # noqa: BLE001 - 继续尝试页面兜底
        pass
    try:
        page = _get_text(f"https://fred.stlouisfed.org/series/{urllib.parse.quote(series_id)}", timeout)
        rows = parse_fred_html(page, limit=max(limit, 10))
        if rows:
            return rows
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"{series_id} 无密钥回退失败（CSV/页面均不可用）: {exc}") from exc
    raise FetchError(f"{series_id} 无密钥回退未取得有效观测值")


def fetch_observations(series_id: str, api_key: str | None = None, limit: int = 400,
                       timeout: int = 30) -> list[dict]:
    """返回按日期降序排列的 [{date, value}]；FRED 缺失值 '.' 会被跳过。"""
    api_key = FRED_API_KEY if api_key is None else api_key
    if not api_key:
        return fetch_observations_csv(series_id, limit=limit)
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": str(limit),
    }
    url = f"{FRED_BASE_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "GoldMacroAI/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001 - 网络错误统一向上抛出
        raise FetchError(f"{series_id} 请求失败: {exc}") from exc
    if "error_code" in payload:
        raise FetchError(f"{series_id} API 错误: {payload.get('error_message', payload)}")
    rows = []
    for item in payload.get("observations", []):
        raw = item.get("value")
        if raw in (".", "", None):
            continue
        try:
            rows.append({"date": item["date"], "value": float(raw)})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(rows, key=lambda r: r["date"], reverse=True)[:limit]


def collect(db, series_ids: list[str] | None = None, api_key: str | None = None) -> dict:
    """抓取 configured 序列并写入 SQLite，返回每个序列的结果摘要。"""
    summary: dict[str, dict] = {}
    for series_id in series_ids or list(SERIES):
        try:
            rows = fetch_observations(series_id, api_key=api_key)
            saved = db.upsert_observations(series_id, rows)
            summary[series_id] = {
                "ok": True,
                "source": "api" if (api_key or FRED_API_KEY) else "csv",
                "fetched": len(rows),
                "saved": saved,
                "latest": rows[0] if rows else None,
            }
        except FetchError as exc:
            summary[series_id] = {"ok": False, "error": str(exc)}
    return summary


def describe_collection(summary: dict) -> str:
    lines = []
    for series_id, result in summary.items():
        name = SERIES.get(series_id, {}).get("name", series_id)
        if result.get("ok"):
            latest = result.get("latest") or {}
            lines.append(f"  [OK] {series_id} {name} [{result.get('source', 'api')}]: {result['fetched']} 条, 最新 {latest.get('date', '-')} = {latest.get('value', '-')}")
        else:
            lines.append(f"  [FAIL] {series_id} {name}: {result['error']}")
    return "\n".join(lines)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
