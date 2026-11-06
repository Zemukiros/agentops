"""web_search / fetch_url (ADR-008). Offline: httpx.MockTransport + a stub resolver."""

import json
import uuid
from pathlib import Path

import httpx
import pytest

from agentops.settings import SearchMode, Settings
from agentops.tools.native import web
from agentops.tools.spec import ToolContext

pytestmark = pytest.mark.unit


def resolver(table: dict[str, list[str]], calls: list[str] | None = None) -> web.Resolver:
    async def resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append(host)
        if host not in table:
            raise OSError("no such host")
        return table[host]

    return resolve


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "100.64.0.1",  # carrier-grade NAT
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "224.0.0.1",
    ],
)
async def test_private_addresses_are_blocked(addr: str) -> None:
    with pytest.raises(web.FetchBlockedError, match="non-public"):
        await web.check_url("https://evil.example/x", resolver({"evil.example": [addr]}))


async def test_one_private_answer_blocks_the_name() -> None:
    r = resolver({"mixed.example": ["93.184.216.34", "10.0.0.5"]})
    with pytest.raises(web.FetchBlockedError, match=r"10\.0\.0\.5"):
        await web.check_url("http://mixed.example/", r)


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("file:///etc/passwd", "scheme"),
        ("gopher://x.example/", "scheme"),
        ("http://user:pw@x.example/", "credentials"),
        ("http://127.0.0.1/", "non-public"),
        ("http://[::1]:8080/", "non-public"),
        ("http://2130706433/", "non-public"),  # decimal 127.0.0.1
    ],
)
async def test_url_level_checks(url: str, needle: str) -> None:
    r = resolver({"2130706433": ["127.0.0.1"], "x.example": ["93.184.216.34"]})
    with pytest.raises(web.FetchBlockedError, match=needle):
        await web.check_url(url, r)


async def test_connection_is_pinned_to_the_checked_ip() -> None:
    """DNS rebinding: the name is resolved once per hop and the request goes to that IP,
    with the original Host header and SNI."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>hi</p>")

    calls: list[str] = []
    out = await web.guarded_fetch(
        "https://good.example/page?q=1",
        resolver=resolver({"good.example": ["93.184.216.34"]}, calls),
        transport=httpx.MockTransport(handler),
    )
    assert out.status == 200 and calls == ["good.example"]
    [req] = seen
    assert req.url.host == "93.184.216.34" and req.url.path == "/page" and req.url.query == b"q=1"
    assert req.headers["host"] == "good.example"
    assert req.extensions["sni_hostname"] == "good.example"


async def test_redirect_to_a_private_address_is_blocked() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    with pytest.raises(web.FetchBlockedError, match="non-public"):
        await web.guarded_fetch(
            "https://good.example/",
            resolver=resolver({"good.example": ["93.184.216.34"]}),
            transport=httpx.MockTransport(handler),
        )


async def test_redirect_hops_are_rechecked_and_capped() -> None:
    hops: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hops.append(req.headers["host"])
        return httpx.Response(301, headers={"location": f"https://h{len(hops)}.example/"})

    table = {f"h{i}.example": ["93.184.216.34"] for i in range(10)} | {
        "start.example": ["93.184.216.34"]
    }
    calls: list[str] = []
    with pytest.raises(web.FetchBlockedError, match="too many redirects"):
        await web.guarded_fetch(
            "https://start.example/",
            resolver=resolver(table, calls),
            transport=httpx.MockTransport(handler),
        )
    assert calls == ["start.example", "h1.example", "h2.example", "h3.example"]


async def test_size_and_type_caps() -> None:
    def big(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 5000)

    r = resolver({"good.example": ["93.184.216.34"]})
    with pytest.raises(web.FetchBlockedError, match="too large"):
        await web.guarded_fetch(
            "https://good.example/", resolver=r, transport=httpx.MockTransport(big), max_bytes=1000
        )

    def pdf(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    with pytest.raises(web.FetchBlockedError, match="unsupported content type"):
        await web.guarded_fetch(
            "https://good.example/", resolver=r, transport=httpx.MockTransport(pdf)
        )


def test_readability_extracts_the_article() -> None:
    html = (web.FIXTURES / "pgvector-overview.html").read_text()
    title, text = web.extract_text(html)
    assert title.startswith("pgvector")
    assert "HNSW" in text and "Home | Docs | Blog" not in text


async def test_tavily_adapter_request_shape_and_parsing() -> None:
    sent: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(
            200,
            json={
                "query": "q",
                "results": [
                    {"title": "T", "url": "https://a.example", "content": "C", "score": 0.9}
                ],
                "response_time": 0.5,
            },
        )

    hits = await web.TavilySearch("tvly-test", transport=httpx.MockTransport(handler)).search(
        "pgvector", 3
    )
    assert hits == [web.SearchHit("T", "https://a.example", "C")]
    [req] = sent
    assert str(req.url) == "https://api.tavily.com/search"
    assert req.headers["authorization"] == "Bearer tvly-test"
    assert json.loads(req.content) == {
        "query": "pgvector",
        "max_results": 3,
        "search_depth": "basic",
    }


def _ctx(tmp_path: Path, mode: SearchMode) -> ToolContext:
    return ToolContext(
        task_id=uuid.uuid4(),
        owner_id=None,
        agent="research",
        subtask_id="s",
        revision=0,
        step=1,
        workspace=tmp_path,
        settings=Settings(search_mode=mode),
        engine=None,  # type: ignore[arg-type]
    )


async def test_fixture_mode_search_and_fetch_never_touch_the_network(tmp_path: Path) -> None:
    c = _ctx(tmp_path, SearchMode.FIXTURE)
    s = await web.web_search(web.SearchIn(query="what is pgvector"), c)
    assert "https://fixtures.agentops.test/pgvector-overview" in s.text
    page = await web.fetch_url(
        web.FetchIn(url="https://fixtures.agentops.test/pgvector-overview"), c
    )
    assert "IVFFlat" in page.text
    with pytest.raises(web.FetchBlockedError, match="fixture mode"):
        await web.fetch_url(web.FetchIn(url="https://example.com/"), c)


def test_live_search_needs_a_key() -> None:
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        web.search_provider(Settings(_env_file=None, search_mode=SearchMode.LIVE))
