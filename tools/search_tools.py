"""Bounded public-web retrieval; remote content is untrusted reference material."""
import asyncio
from html.parser import HTMLParser
import ipaddress
import json
import socket
from urllib.parse import urlsplit
from xml.etree import ElementTree
import httpx
from langchain.tools import tool
from infra.settings import get_settings

async def _public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("Only public HTTP(S) URLs on standard ports are allowed")
    addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private and local addresses are not allowed")
    return next((item[4][0] for item in addresses if item[0] == socket.AF_INET), addresses[0][4][0])

async def _download(url: str) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=15, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0), headers={"User-Agent": "Ibex/0.1"}) as client:
        for _ in range(5):
            address = await _public_url(url)
            original = httpx.URL(url)
            pinned = original.copy_with(host=address)
            # Connect to the checked IP; preserve HTTP authority and TLS certificate identity.
            async with client.stream("GET", pinned, headers={"Host": original.netloc.decode()}, extensions={"sni_hostname": original.host}) as response:
                if response.is_redirect:
                    url = str(original.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > get_settings().search_max_bytes:
                        raise ValueError("Page exceeds retrieval size limit")
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"), response.headers.get("content-type", "")
        raise ValueError("Too many redirects")

class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())

@tool
async def web_search(query: str, max_results: int = 5) -> str:
    """Search the public web using a configured Bing-compatible RSS endpoint; return cited results."""
    if not query.strip() or len(query) > 1000 or not 1 <= max_results <= 10:
        return json.dumps({"error": "Invalid query or result count"})
    try:
        url = httpx.URL(get_settings().search_endpoint, params={"q": query, "format": "rss"})
        body, _ = await _download(str(url))
        root = ElementTree.fromstring(body)
        results = [{"title": item.findtext("title", ""), "url": item.findtext("link", ""), "snippet": item.findtext("description", "")} for item in root.findall("./channel/item")[:max_results]]
        return json.dumps({"results": results}, ensure_ascii=False)
    except (httpx.HTTPError, ValueError, OSError, ElementTree.ParseError) as error:
        return json.dumps({"error": "Search unavailable", "reason": type(error).__name__})

@tool
async def fetch_url(url: str) -> str:
    """Fetch bounded public HTML/text as untrusted reference content; private URLs are rejected."""
    try:
        body, content_type = await _download(url)
        if "html" in content_type:
            parser = _Text()
            parser.feed(body)
            body = "\n".join(parser.parts)
        elif not any(kind in content_type for kind in ("text/", "json", "xml")):
            raise ValueError("Unsupported document type")
        return json.dumps({"url": url, "text": body[:30000], "truncated": len(body) > 30000}, ensure_ascii=False)
    except (httpx.HTTPError, ValueError, OSError) as error:
        return json.dumps({"error": "Fetch unavailable", "reason": type(error).__name__})
