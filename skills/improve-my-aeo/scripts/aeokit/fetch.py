"""One polite HTTP GET at a time, with a timeout, a body cap, and redirects followed by hand."""

from __future__ import annotations

import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 improve-my-aeo-audit"
MAX_REDIRECTS = 5
CHARSET_RE = re.compile(r"charset=([\w.:-]+)", re.I)


class FetchResult:
    """What one GET observed: the final URL, status, headers, decoded body, size, time, and any error."""

    def __init__(self, url: str) -> None:
        # Starts as a failed fetch of url; HttpFetcher fills in what it saw.
        self.url = url
        self.final_url = url
        self.status = 0
        self.headers: dict = {}
        self.body = ""
        self.bytes = 0
        self.truncated = False
        self.elapsed_ms = 0
        self.error = None
        self.redirects: list = []

    @property
    def ok(self) -> bool:
        # True for a 2xx response.
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        # The media type without parameters, lowercased, or "" when the server sent none.
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def header(self, name: str) -> str:
        # One response header by case-insensitive name, or "".
        return self.headers.get(name.lower(), "")

    def summary(self) -> dict:
        # The log row printed in the gather summary.
        row = {"url": self.url, "status": self.status, "bytes": self.bytes, "ms": self.elapsed_ms}
        if self.final_url != self.url:
            row["final_url"] = self.final_url
        if self.error:
            row["error"] = self.error
        return row


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stops urllib from following redirects, so every hop is seen and counted."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        # Returning None makes urllib raise HTTPError for the 3xx, which HttpFetcher handles.
        return None


class HttpFetcher:
    """Sequential GETs with a pause between them, a per-request timeout, and a body cap."""

    def __init__(self, timeout: float = 15.0, max_bytes: int = 3_000_000, pause: float = 0.25, opener=None) -> None:
        # opener is injectable for tests; the default refuses automatic redirects.
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.pause = pause
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self._log: list = []
        self._last = 0.0

    def get(self, url: str, user_agent: str = BROWSER_UA, accept: str = None) -> FetchResult:
        # Fetches url, following up to MAX_REDIRECTS Location hops; never raises for network or HTTP errors.
        result = FetchResult(url)
        started = time.monotonic()
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            if urllib.parse.urlsplit(current).scheme not in ("http", "https"):
                result.error = "unsupported scheme"
                break
            location = self._one(current, user_agent, accept, result)
            if not location:
                break
            result.redirects.append({"from": current, "status": result.status, "to": location})
            current = urllib.parse.urljoin(current, location)
            result.final_url = current
        else:
            result.error = "too many redirects"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        self._log.append(result.summary())
        return result

    def log(self) -> list:
        # Every fetch's summary row, in order.
        return list(self._log)

    def _one(self, url: str, user_agent: str, accept: str, result: FetchResult):
        # Performs one request and fills result; returns the Location to follow, or None.
        self._wait()
        headers = {"User-Agent": user_agent, "Accept-Encoding": "identity", "Accept": accept or "text/html,application/xhtml+xml,text/plain,*/*;q=0.8"}
        request = urllib.request.Request(url, headers=headers)
        try:
            response = self.opener.open(request, timeout=self.timeout)
            self._read(response, result)
            return None
        except urllib.error.HTTPError as error:
            self._read(error, result)
            if 300 <= error.code < 400 and result.header("location"):
                return result.header("location")
            return None
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError, ValueError) as error:
            result.status = 0
            result.error = self._reason(error)
            return None

    def _read(self, response, result: FetchResult) -> None:
        # Copies status and headers, then reads at most max_bytes of the body and decodes it.
        result.status = int(getattr(response, "status", None) or getattr(response, "code", 0) or 0)
        result.headers = {k.lower(): v for k, v in (response.headers.items() if response.headers else [])}
        try:
            raw = response.read(self.max_bytes + 1) or b""
        except (OSError, ValueError) as error:
            raw = b""
            result.error = self._reason(error)
        result.truncated = len(raw) > self.max_bytes
        raw = raw[: self.max_bytes]
        result.bytes = len(raw)
        match = CHARSET_RE.search(result.headers.get("content-type", ""))
        encoding = match.group(1) if match else "utf-8"
        try:
            result.body = raw.decode(encoding, "replace")
        except LookupError:
            result.body = raw.decode("utf-8", "replace")

    def _wait(self) -> None:
        # Keeps at least `pause` seconds between two requests.
        gap = self.pause - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    @staticmethod
    def _reason(error: BaseException) -> str:
        # A short, single-line description of a network error.
        reason = getattr(error, "reason", None) or error
        return (type(error).__name__ + ": " + str(reason)).replace("\n", " ")[:200]
