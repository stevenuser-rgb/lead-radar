import os
from typing import Any, Dict, Optional

import httpx


class FacebookRunnerError(RuntimeError):
    """Raised when the standalone Facebook runner cannot fulfil a request."""


def runner_url() -> str:
    return os.getenv("FACEBOOK_RUNNER_URL", "http://facebook-runner:9090").rstrip("/")


def _request(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    try:
        timeout = kwargs.pop("timeout", 15)
        response = httpx.request(method, f"{runner_url()}{path}", timeout=timeout, **kwargs)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise FacebookRunnerError(f"Facebook runner 無法連線：{exc}") from exc
    if response.status_code >= 400:
        message = payload.get("message", "runner request failed") if isinstance(payload, dict) else "runner request failed"
        raise FacebookRunnerError(message)
    return payload


def submit_facebook_job(
    group_url: str,
    max_posts: int,
    no_proxy: bool,
    cookies_file: str = "",
    no_new_post_cycles: int = 4,
    monitor: bool = False,
    source_key: str = "",
) -> Dict[str, Any]:
    payload = {
        "groupUrl": group_url,
        "maxPosts": max_posts,
        "noProxy": no_proxy,
        "cookiesFile": cookies_file,
        "noNewPostCycles": no_new_post_cycles,
        "monitor": monitor,
        "sourceKey": source_key,
    }
    return _request("POST", "/jobs", json=payload)


def get_runner_job(runner_job_id: str) -> Dict[str, Any]:
    return _request("GET", f"/jobs/{runner_job_id}")


def get_runner_logs(runner_job_id: str) -> Dict[str, Any]:
    return _request("GET", f"/jobs/{runner_job_id}/logs")


def get_runner_posts(runner_job_id: str) -> Dict[str, Any]:
    return _request("GET", f"/jobs/{runner_job_id}/posts")


def cancel_runner_job(runner_job_id: str) -> Dict[str, Any]:
    return _request("POST", f"/jobs/{runner_job_id}/cancel")


def runner_health() -> Optional[Dict[str, Any]]:
    try:
        return _request("GET", "/health", timeout=2)
    except FacebookRunnerError:
        return None
