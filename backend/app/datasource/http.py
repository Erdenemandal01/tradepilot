"""Minimal JSON-over-HTTPS client (standard library only).

Error messages name the URL without its query string, so an API key passed
as a query parameter never ends up in a log.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.app import __version__
from backend.app.datasource.base import DataSourceError

USER_AGENT = f"tradepilot/{__version__}"


def get_json(url: str, params: Mapping[str, str], timeout: float = 10.0) -> Any:
    request = Request(
        f"{url}?{urlencode(params)}" if params else url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace")
        raise DataSourceError(f"HTTP {exc.code} from {url}: {detail}") from None
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise DataSourceError(f"cannot reach {url}: {reason}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise DataSourceError(f"response from {url} is not JSON") from None
