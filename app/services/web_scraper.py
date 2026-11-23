"""SSRF-safe web scraper and HTML-to-Markdown extractor for Prompt-to-Product research."""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

VERSION = "2.9.1"

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
    "169.254.169.254",
}


class SSRFBlockedError(ValueError):
    """Raised when a URL fails SSRF safety validation (private IP, local host, or unsafe scheme)."""


def _is_unsafe_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_url(url: str, resolve_dns: bool = True) -> str:
    """Validate that URL uses http/https and does not target private, loopback, or metadata networks."""
    raw = (url or "").strip()
    if not raw:
        raise SSRFBlockedError("empty_url")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlockedError(f"unsafe_scheme:{parsed.scheme or 'none'}")

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise SSRFBlockedError("missing_hostname")

    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith((".local", ".internal", ".localhost")):
        raise SSRFBlockedError(f"ssrf_blocked_hostname:{hostname}")

    # Direct IP literal check
    ip_obj = None
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if _is_unsafe_ip(hostname):
            raise SSRFBlockedError(f"ssrf_blocked_private_ip:{hostname}")
        return raw

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise SSRFBlockedError(f"dns_resolution_failed:{hostname}") from exc

        if not infos:
            raise SSRFBlockedError(f"dns_no_records:{hostname}")

        for info in infos:
            ip_addr = info[4][0]
            if _is_unsafe_ip(ip_addr):
                raise SSRFBlockedError(f"ssrf_blocked_resolved_ip:{hostname}->{ip_addr}")

    return raw


def html_to_markdown(html: str, max_chars: int = 12000) -> dict[str, Any]:
    """Convert raw HTML into clean, structured Markdown for LLM product extraction."""
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    meta_desc = ""
    meta_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta_tag and meta_tag.get("content"):
        meta_desc = str(meta_tag["content"]).strip()

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer", "aside"]):
        tag.decompose()

    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
    if meta_desc:
        lines.append(f"> {meta_desc}")

    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"]):
        name = el.name
        if name in ("h1", "h2", "h3", "h4"):
            txt = el.get_text(" ", strip=True)
            if txt:
                level = "#" * int(name[1])
                lines.append(f"\n{level} {txt}")
        elif name == "li":
            txt = el.get_text(" ", strip=True)
            if txt:
                lines.append(f"- {txt}")
        elif name == "tr":
            cells = [c.get_text(" ", strip=True) for c in el.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if cells:
                lines.append("| " + " | ".join(cells) + " |")
        elif name == "p":
            txt = el.get_text(" ", strip=True)
            if txt and len(txt) > 3:
                lines.append(txt)

    if not lines:
        fallback_txt = soup.get_text("\n", strip=True)
        lines = [ln.strip() for ln in fallback_txt.splitlines() if ln.strip()]

    markdown = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return {
        "title": title,
        "markdown": markdown[:max_chars],
        "char_count": len(markdown),
    }


async def fetch_url_markdown(
    url: str,
    transport: Any = None,
    timeout: float = 15.0,
    resolve_dns: bool = True,
) -> dict[str, Any]:
    """Fetch a public URL safely (with SSRF checks on redirects) and return clean Markdown."""
    current_url = validate_public_url(url, resolve_dns=resolve_dns)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; LeadHunterResearchBot/2.4; +https://leadhunter.crmconsulting.kz)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=timeout) as client:
        for _ in range(4):
            resp = await client.get(current_url, headers=headers)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                if not loc:
                    break
                next_url = urljoin(current_url, loc)
                current_url = validate_public_url(next_url, resolve_dns=resolve_dns)
                continue
            if resp.status_code != 200:
                raise ValueError(f"scrape_http_{resp.status_code}")
            parsed_md = html_to_markdown(resp.text)
            parsed_md["url"] = current_url
            return parsed_md

    raise ValueError("scrape_too_many_redirects")
