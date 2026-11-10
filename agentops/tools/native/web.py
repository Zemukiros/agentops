"""web_search + fetch_url (ADR-008).

web_search: a `SearchProvider` interface.
  - SEARCH_MODE=fixture (default): recorded results from tools/native/fixtures/web/search.json.
  - SEARCH_MODE=live: Tavily (POST https://api.tavily.com/search, Bearer key, search_depth
    "basic" = 1 credit per call; verified against Tavily's API reference on 2026-11-06). Used
    only if TAVILY_API_KEY is set; tests use recorded responses only.

fetch_url:
  - fixture mode: pages from tools/native/fixtures/web/pages.json (no network at all).
  - live mode, SSRF guard (checked after DNS resolution):
      * http/https only; no userinfo; host required
      * every resolved address must be public (is_global, not multicast; IPv4-mapped IPv6
        is checked as IPv4). One private address rejects the whole name.
      * the connection is PINNED to the checked address: the request goes to the IP, with
        the original Host header and TLS SNI/certificate check against the hostname. A second
        DNS answer (rebinding) is never consulted.
      * redirects are followed by hand, max 3, each hop re-checked from scratch
      * caps: 2 MB body (streamed, aborted when exceeded), 10 s total; html or text only
  - readability-lxml extracts the article text from HTML.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import lxml.html
from pydantic import Field
from readability import Document

from agentops.settings import SearchMode, Settings
from agentops.tools.spec import ToolContext, ToolInput, ToolOutput, ToolSpec

FIXTURES = Path(__file__).parent / "fixtures" / "web"
MAX_BYTES = 2_000_000
TOTAL_TIMEOUT_S = 10.0
MAX_REDIRECTS = 3
MAX_TEXT_CHARS = 20_000
TAVILY_URL = "https://api.tavily.com/search"
USER_AGENT = "agentops-fetch/0.1 (+https://github.com/Zemukiros/agentops)"


# ---- search --------------------------------------------------------------------------------
@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    async def search(self, query: str, max_results: int) -> list[SearchHit]: ...


class FixtureSearch:
    """Entries in search.json: {"match": [keywords], "results": [...]}. An entry matches when
    every keyword occurs in the lower-cased query; the first match wins; "default" otherwise."""

    def __init__(self, path: Path = FIXTURES / "search.json") -> None:
        self._data: dict[str, Any] = json.loads(path.read_text())

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        q = query.lower()
        results = self._data["default"]
        for entry in self._data["entries"]:
            if all(k in q for k in entry["match"]):
                results = entry["results"]
                break
        return [SearchHit(r["title"], r["url"], r["snippet"]) for r in results[:max_results]]


class TavilySearch:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key = api_key
        self._transport = transport

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        async with httpx.AsyncClient(transport=self._transport, timeout=15) as client:
            r = await client.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                json={"query": query, "max_results": max_results, "search_depth": "basic"},
            )
        if r.status_code != 200:
            raise RuntimeError(f"search provider returned HTTP {r.status_code}")
        return [
            SearchHit(x.get("title", ""), x.get("url", ""), x.get("content", ""))
            for x in r.json().get("results", [])[:max_results]
        ]


def search_provider(settings: Settings) -> SearchProvider:
    if settings.search_mode is SearchMode.FIXTURE:
        return _fixture_search()
    key = settings.tavily_api_key
    if key is None or not key.get_secret_value():
        raise RuntimeError("SEARCH_MODE=live needs TAVILY_API_KEY")
    return TavilySearch(key.get_secret_value())


@lru_cache(maxsize=1)
def _fixture_search() -> FixtureSearch:
    return FixtureSearch()


class SearchIn(ToolInput):
    query: str = Field(min_length=2, max_length=400)
    max_results: int = Field(default=5, ge=1, le=10)


async def web_search(args: SearchIn, ctx: ToolContext) -> ToolOutput:
    hits = await search_provider(ctx.settings).search(args.query, args.max_results)
    text = "\n\n".join(f"[{i}] {h.title}\n{h.url}\n{h.snippet}" for i, h in enumerate(hits, 1))
    return ToolOutput(text=text or "no results", data={"urls": [h.url for h in hits]})


# ---- fetch: SSRF guard ---------------------------------------------------------------------
class FetchBlockedError(ValueError):
    pass


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return sorted({str(i[4][0]) for i in infos})


def is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    ip: str
    url: str  # the URL as requested, for display


async def check_url(url: str, resolver: Resolver) -> Target:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise FetchBlockedError(f"scheme not allowed: {parts.scheme or '(none)'}")
    if parts.username or parts.password:
        raise FetchBlockedError("credentials in URLs are not allowed")
    host = parts.hostname
    if not host:
        raise FetchBlockedError("URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
        addrs = [str(literal)]
    except ValueError:
        try:
            addrs = await resolver(host, port)
        except OSError as exc:
            raise FetchBlockedError(f"cannot resolve {host}: {exc}") from None
    if not addrs:
        raise FetchBlockedError(f"cannot resolve {host}")
    bad = [a for a in addrs if not is_public(a)]
    if bad:
        raise FetchBlockedError(f"{host} resolves to a non-public address ({bad[0]})")
    return Target(parts.scheme, host, port, addrs[0], url)


def pinned_request(target: Target, method: str = "GET") -> httpx.Request:
    """Request the checked IP directly; Host header and TLS SNI keep the original hostname
    (httpcore verifies the certificate against `sni_hostname`)."""
    parts = urlsplit(target.url)
    ip_host = f"[{target.ip}]" if ":" in target.ip else target.ip
    default = 443 if target.scheme == "https" else 80
    netloc = ip_host if target.port == default else f"{ip_host}:{target.port}"
    pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    host_header = target.host if target.port == default else f"{target.host}:{target.port}"
    return httpx.Request(
        method,
        pinned,
        headers={"Host": host_header, "User-Agent": USER_AGENT, "Accept": "text/html,text/plain"},
        extensions={"sni_hostname": target.host},
    )


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    content_type: str
    body: bytes


async def guarded_fetch(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    max_bytes: int = MAX_BYTES,
) -> Fetched:
    async def _go() -> Fetched:
        current = url
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False
        ) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                target = await check_url(current, resolver)
                resp = await client.send(pinned_request(target), stream=True)
                try:
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        loc = resp.headers.get("location")
                        if not loc:
                            raise FetchBlockedError("redirect without a Location header")
                        current = urljoin(current, loc)
                        continue
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in {"text/html", "text/plain", "application/xhtml+xml"}:
                        raise FetchBlockedError(f"unsupported content type: {ctype or 'none'}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FetchBlockedError(f"response too large ({declared} bytes)")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            raise FetchBlockedError(f"response too large (> {max_bytes} bytes)")
                    return Fetched(current, resp.status_code, ctype, bytes(body))
                finally:
                    await resp.aclose()
            raise FetchBlockedError(f"too many redirects (> {MAX_REDIRECTS})")

    try:
        return await asyncio.wait_for(_go(), TOTAL_TIMEOUT_S)
    except TimeoutError:
        raise FetchBlockedError(f"fetch took longer than {TOTAL_TIMEOUT_S:.0f}s") from None


def extract_text(html: bytes | str) -> tuple[str, str]:
    """(title, readable text) via readability-lxml."""
    doc = Document(html)
    summary = doc.summary(html_partial=True)
    text = lxml.html.fromstring(summary).text_content() if summary.strip() else ""
    lines = [ln.strip() for ln in text.splitlines()]
    return doc.title() or "", "\n".join(ln for ln in lines if ln)


@lru_cache(maxsize=1)
def _fixture_pages() -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = json.loads((FIXTURES / "pages.json").read_text())
    return data


class FetchIn(ToolInput):
    url: str = Field(min_length=8, max_length=2000, description="http(s) URL to read")


async def fetch_url(args: FetchIn, ctx: ToolContext) -> ToolOutput:
    if ctx.settings.search_mode is SearchMode.FIXTURE:
        page = _fixture_pages().get(args.url)
        if page is None:
            raise FetchBlockedError("page not available (fixture mode serves recorded pages only)")
        html: bytes | str = (FIXTURES / page["file"]).read_text()
        final_url, status = args.url, 200
        if not page["file"].endswith(".html"):
            # like a real text/plain response: returned as-is, no HTML extraction
            body = str(html)[:MAX_TEXT_CHARS]
            return ToolOutput(text=f"URL: {final_url}\n\n{body}", data={"url": final_url})
    else:
        fetched = await guarded_fetch(args.url)
        html, final_url, status = fetched.body, fetched.url, fetched.status
        if fetched.content_type == "text/plain":
            text = fetched.body.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
            return ToolOutput(text=f"URL: {final_url}\n\n{text}", data={"url": final_url})
    title, text = extract_text(html)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[... truncated]"
    return ToolOutput(
        text=f"URL: {final_url}\nTitle: {title}\nHTTP {status}\n\n{text}",
        data={"url": final_url, "title": title},
    )


WEB_TOOLS = [
    ToolSpec(
        name="web_search",
        description="Search the web. Returns titles, URLs and snippets.",
        input_model=SearchIn,
        fn=web_search,
        allowed_agents=frozenset({"research"}),
        risk="read",
        timeout_s=20,
        rate_per_min=20,
    ),
    ToolSpec(
        name="fetch_url",
        description="Fetch a public web page and return its readable text.",
        input_model=FetchIn,
        fn=fetch_url,
        allowed_agents=frozenset({"research"}),
        risk="read",
        timeout_s=15,
        rate_per_min=20,
        meta={"url_provenance": "url"},  # ADR-044: no fetching of model-invented URLs
    ),
]
