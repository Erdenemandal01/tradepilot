"""Minimal JSON-over-HTTPS client (standard library only).

Error messages name the URL without its query string, so an API key passed
as a query parameter never ends up in a log.
"""

from __future__ import annotations

import json
from http.client import HTTPException
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.app.datasource.base import DataSourceError

# A generic client name: requests carry nothing that identifies the project.
USER_AGENT = "Mozilla/5.0 (compatible; candle-feed-client)"


def get_json(url: str, params: Mapping[str, str], timeout: float = 10.0) -> Any:
    request = Request(
        f"{url}?{urlencode(params)}" if params else url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        try:
            detail = exc.read()[:200].decode("utf-8", "replace")
        except (OSError, HTTPException):
            detail = ""
        raise DataSourceError(f"HTTP {exc.code} from {url}: {detail}") from None
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        # HTTPException covers a connection cut in mid-response (IncompleteRead)
        # and a garbled status line; neither is an OSError.
        reason = getattr(exc, "reason", None) or f"{type(exc).__name__}: {exc}"
        raise DataSourceError(f"cannot reach {url}: {reason}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise DataSourceError(f"response from {url} is not JSON") from None
