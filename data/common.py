"""共享 HTTP 抓取工具（仅使用标准库）。

设计目标：零依赖、跨平台、兼容 Windows 系统代理与证书吊销检查离线的问题。
优先调用系统 curl，失败时回退 urllib；两者都由 config.py 设置的
HTTPS_PROXY / HTTP_PROXY 环境变量自动接管代理。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid


class FetchError(RuntimeError):
    pass


def fetch_bytes(url: str, *, timeout: int = 45, headers: dict[str, str] | None = None,
                post_json: dict | list | None = None) -> bytes:
    """下载原始字节；post_json 存在时改用 POST + application/json。"""
    payload = None
    if post_json is not None:
        payload = json.dumps(post_json).encode("utf-8")

    extra = []
    for key, value in (headers or {}).items():
        extra += ["-H", f"{key}: {value}"]
    if payload is not None:
        extra += ["-H", "Content-Type: application/json", "--data-binary", "@-"]

    for curl_bin in ("curl.exe", "curl"):
        cmd = [curl_bin, "-sSL", "-m", str(timeout)]
        if os.name == "nt":
            # Windows 离线吊销检查会导致 CRYPT_E_REVOCATION_OFFLINE，curl 需显式跳过
            cmd.append("--ssl-no-revoke")
        cmd += extra + [url]
        try:
            result = subprocess.run(
                cmd, input=payload, capture_output=True, timeout=timeout + 15,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:  # noqa: BLE001 - curl 不可用时尝试 urllib
            continue

    request_headers = dict(headers or {})
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=payload, headers=request_headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"下载失败 {url}: {exc}") from exc


def fetch_text(url: str, *, timeout: int = 45, headers: dict[str, str] | None = None,
               post_json: dict | list | None = None) -> str:
    return fetch_bytes(url, timeout=timeout, headers=headers,
                       post_json=post_json).decode("utf-8-sig", errors="replace")


def fetch_json(url: str, *, timeout: int = 45, headers: dict[str, str] | None = None,
               post_json: dict | list | None = None):
    text = fetch_text(url, timeout=timeout, headers=headers, post_json=post_json)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise FetchError(f"响应不是合法 JSON: {url}（前 120 字符: {text[:120]!r}）") from exc


def fetch_yahoo_texts(urls: list[str], *, timeout: int = 45,
                      pause: float = 1.5, retries: int = 2) -> list[str]:
    """用同一个 cookie jar + crumb 批量请求 Yahoo 鉴权接口，降低触发限流概率。

    Yahoo 先用 fc.yahoo.com 设置 cookie，再由 getcrumb 返回一次性 crumb；
    这些在一个会话内可复用。返回与 urls 等长的文本列表，单个 URL 失败时
    对应位置为空字符串，由调用方决定跳过，避免一个坏请求拖垮整批。
    """
    if not urls:
        return []
    jar = os.path.join(tempfile.gettempdir(), f"gma_yahoo_{uuid.uuid4().hex}.txt")
    errors: list[str] = []
    try:
        for curl_bin in ("curl.exe", "curl"):
            def run(args: list[str]):
                cmd = [curl_bin, "-sSL", "-m", str(timeout), "-A", "Mozilla/5.0"]
                if os.name == "nt":
                    cmd.append("--ssl-no-revoke")
                cmd += args
                return subprocess.run(cmd, capture_output=True, timeout=timeout + 15)

            try:
                run(["-c", jar, "-o", os.devnull, "https://fc.yahoo.com"])
                crumb = ""
                for attempt in range(retries + 1):
                    crumb_result = run([
                        "-b", jar, "https://query1.finance.yahoo.com/v1/test/getcrumb",
                    ])
                    crumb = crumb_result.stdout.decode("utf-8-sig", errors="replace").strip()
                    if crumb and "too many" not in crumb.lower():
                        break
                    crumb = ""
                    if attempt < retries:
                        time.sleep(pause * (attempt + 2))
                if not crumb:
                    raise RuntimeError("getcrumb 返回空值或被限流")
                texts: list[str] = []
                for index, url in enumerate(urls):
                    if index:
                        time.sleep(pause)  # 对免费接口保持礼貌，降低限流概率
                    separator = "&" if "?" in url else "?"
                    target = f"{url}{separator}crumb={urllib.parse.quote(crumb)}"
                    body = ""
                    for attempt in range(retries + 1):
                        result = run(["-b", jar, target])
                        candidate = result.stdout.decode("utf-8-sig", errors="replace")
                        if (result.returncode == 0 and candidate
                                and "Too Many Requests" not in candidate):
                            body = candidate
                            break
                        if attempt < retries:
                            time.sleep(pause * (attempt + 2))
                    texts.append(body)
                return texts
            except Exception as exc:  # noqa: BLE001 - 尝试下一个 curl 可执行文件
                errors.append(f"{curl_bin}: {exc}")
    finally:
        try:
            os.remove(jar)
        except OSError:
            pass
    raise FetchError("Yahoo crumb 请求失败：" + "；".join(errors[:2]))


def fetch_yahoo_text(url: str, *, timeout: int = 45) -> str:
    """请求单个需要 crumb + cookie 的 Yahoo Finance 接口（如期权链）。"""
    text = fetch_yahoo_texts([url], timeout=timeout)[0]
    if not text:
        raise FetchError(f"Yahoo 响应为空: {url}")
    return text


def fetch_cboe_text(url: str, *, timeout: int = 60, retries: int = 2) -> str:
    """请求 Cboe 免费延迟期权报价（约 5 MB/标的）。

    Cboe 由 Cloudflare 保护：需浏览器 UA + Referer，且必须绕过本机代理
    （代理出口 IP 会被拦截）；GitHub Actions 直连环境下 --noproxy 同样无害。
    """
    errors: list[str] = []
    for curl_bin in ("curl.exe", "curl"):
        for attempt in range(retries + 1):
            cmd = [
                curl_bin, "-sSL", "--noproxy", "*", "-m", str(timeout),
                "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "-H", "Referer: https://www.cboe.com/",
            ]
            if os.name == "nt":
                cmd.append("--ssl-no-revoke")
            cmd.append(url)
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
                body = result.stdout.decode("utf-8-sig", errors="replace")
                if (result.returncode == 0 and body
                        and "Attention Required" not in body
                        and "Too Many Requests" not in body):
                    return body
                errors.append(f"{curl_bin} 第{attempt + 1}次: HTTP/内容异常 "
                              f"(len={len(body)}, rc={result.returncode})")
            except Exception as exc:  # noqa: BLE001 - 尝试下一个 curl / 重试
                errors.append(f"{curl_bin} 第{attempt + 1}次: {exc}")
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise FetchError("Cboe 期权链请求失败：" + "；".join(errors[:3]))
