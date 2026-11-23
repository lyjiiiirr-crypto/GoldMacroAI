"""共享 HTTP 抓取工具（仅使用标准库）。

设计目标：零依赖、跨平台、兼容 Windows 系统代理与证书吊销检查离线的问题。
优先调用系统 curl，失败时回退 urllib；两者都由 config.py 设置的
HTTPS_PROXY / HTTP_PROXY 环境变量自动接管代理。
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request


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
