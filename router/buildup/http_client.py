"""HTTP session builder with retry logic."""

from __future__ import annotations

import sys

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("ERROR: 'requests' 패키지가 필요합니다.  pip install requests", file=sys.stderr)
    raise SystemExit(1)

from buildup.config import BuildupConfig


def build_session(cfg: BuildupConfig) -> requests.Session:
    """Create a requests.Session with automatic retry on transient errors."""
    session = requests.Session()
    retry = Retry(
        total=cfg.http_max_retries,
        backoff_factor=cfg.http_backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
