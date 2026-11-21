"""Bounded, credential-free public HTTPS fetches. No CRM or browser session."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit


USER_AGENT = 'CRM-Consulting-Lead-Hunter/1.3'
_ROBOT_NAME = USER_AGENT.split('/')[0].casefold()
_DNS_NAME = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
_SENSITIVE_QUERY = re.compile(r'^(?:key|api[_-]?key|auth|authorization|access[_-]?token|token|secret|password|signature|session(?:id)?)$', re.I)
_NAT64 = tuple(ipaddress.ip_network(x) for x in ('64:ff9b::/96', '64:ff9b:1::/48'))
_GOOGLE_GROUNDING_HOST = 'vertexaisearch.cloud.google.com'


class PublicSourceError(ValueError):
    """Short local reason. Never include response bodies, credentials or headers."""

    def __init__(self, reason, *, retryable=False):
        super().__init__(reason)
        self.retryable = retryable


def public_https_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > 2048 or '\\' in url or any(ch.isspace() or ord(ch) < 33 or ord(ch) == 127 for ch in url):
        raise PublicSourceError('Invalid public URL')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').encode('idna').decode('ascii').lower()
        valid_port = parsed.port in (None, 443)
    except (ValueError, UnicodeError):
        raise PublicSourceError('Invalid public URL') from None
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
            or not valid_port or not _DNS_NAME.fullmatch(host)
            or host.endswith(('.localhost', '.local', '.internal', '.test', '.example', '.onion'))
            or '/rest/' in parsed.path.lower() or '%' in parsed.netloc
            or any(_SENSITIVE_QUERY.fullmatch(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True))):
        raise PublicSourceError('Public HTTPS without credentials is required')
    # Requests use ASCII URI paths; reject ambiguous or unencoded paths.
    if not (parsed.path + parsed.query).isascii() or re.search(r'%(?![0-9a-fA-F]{2})', parsed.path + parsed.query):
        raise PublicSourceError('Invalid URI encoding')
    from urllib.parse import unquote
    decoded_path = unquote(parsed.path)
    if ('\\' in decoded_path or '/rest/' in decoded_path.casefold() or re.search(r'%[0-9a-fA-F]{2}', decoded_path)
            or any(segment in {'.', '..'} for segment in decoded_path.split('/'))
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded_path)):
        raise PublicSourceError('Ambiguous or restricted URI path')
    return urlunsplit(('https', host, parsed.path or '/', parsed.query, ''))


def resolve_public_addresses(host: str) -> tuple[str, ...]:
    """Reject mixed answers; the HTTP socket uses a selected numeric address."""
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        answers = tuple(dict.fromkeys(row[4][0] for row in records))
        if not answers:
            raise PublicSourceError('No DNS address', retryable=True)
        for address in answers:
            value = ipaddress.ip_address(address)
            if (not value.is_global or value.is_private or value.is_reserved or value.is_multicast
                    or value.is_loopback or value.is_link_local or value.is_unspecified
                    or (value.version == 6 and (value.scope_id is not None or value.is_site_local
                        or value.ipv4_mapped is not None or value.sixtofour is not None or value.teredo is not None
                        or any(value in prefix for prefix in _NAT64)))):
                raise PublicSourceError('DNS destination is not publicly routable')
    except (socket.gaierror, ValueError) as exc:
        if isinstance(exc, PublicSourceError):
            raise
        raise PublicSourceError('Public DNS resolution failed', retryable=True) from None
    return answers


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        # Numeric IP for the actual socket; original hostname for TLS/SNI/Host.
        raw = socket.create_connection((self._address, 443), self.timeout)
        try:
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(self._address):
                raise PublicSourceError('Connected address differs from the verified address')
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


@dataclass(frozen=True)
class PublicResponse:
    url: str
    status: int
    media_type: str
    body: bytes
    checked_at: datetime
    redirect_location: str | None = None


def _request(url: str, *, timeout: float, max_bytes: int) -> PublicResponse:
    parts = urlsplit(url)
    host = parts.hostname
    deadline = time.monotonic() + timeout
    addresses = resolve_public_addresses(host)
    # No automatic failover/re-resolution or proxy configuration.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PublicSourceError('Public connection timed out', retryable=True)
    conn = _PinnedHTTPSConnection(host, addresses[0], remaining)
    try:
        path = parts.path + (f'?{parts.query}' if parts.query else '')
        conn.request('GET', path, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity', 'Connection': 'close'})
        with conn.getresponse() as response:
            media = (response.getheader('Content-Type') or '').split(';', 1)[0].strip().casefold()
            if response.status not in {200, 204}:
                return PublicResponse(url, response.status, media, b'', datetime.now(timezone.utc),
                                      response.getheader('Location'))
            if (response.getheader('Content-Encoding') or 'identity').casefold() != 'identity':
                raise PublicSourceError('Compressed public responses are not accepted')
            length = response.getheader('Content-Length')
            if length and (not length.isascii() or not length.isdecimal() or len(length) > 12 or int(length) > max_bytes):
                raise PublicSourceError('Public response exceeds byte limit')
            chunks = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise PublicSourceError('Public response timed out', retryable=True)
                chunk = response.read1(min(65536, max_bytes + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise PublicSourceError('Public response exceeds byte limit')
            return PublicResponse(url, response.status, media, bytes(chunks), datetime.now(timezone.utc))
    except (OSError, http.client.HTTPException):
        raise PublicSourceError('Public HTTPS connection failed', retryable=True) from None
    finally:
        conn.close()


def _normalized_path(value):
    def normalize(match):
        char = chr(int(match.group()[1:], 16))
        return char if char.isascii() and (char.isalnum() or char in '-._~') else match.group().upper()
    return re.sub(r'%[0-9a-fA-F]{2}', normalize, value)


@dataclass(frozen=True)
class _Robots:
    rules: tuple[tuple[str, int, bool], ...] = ()
    delay: float = 0

    def allows(self, path):
        path = _normalized_path(path)
        matches = [(size, allow) for rule, size, allow in self.rules if _robots_match(rule, path)]
        return max(matches, default=(0, True))[1]


def _robots_match(pattern, path):
    """Greedy literal segment search without backtracking regular expressions."""
    terminal = pattern.endswith('$')
    if terminal:
        pattern = pattern[:-1]
    parts = pattern.split('*')
    if len(parts) == 1:
        return path == pattern if terminal else path.startswith(pattern)
    if not path.startswith(parts[0]):
        return False
    pos, end = len(parts[0]), len(path)
    tail = parts[-1] if terminal else None
    if tail is not None:
        if not path.endswith(tail):
            return False
        end -= len(tail)
    for literal in parts[1:-1] if terminal else parts[1:]:
        if literal:
            match = path.find(literal, pos, end)
            if match < 0:
                return False
            pos = match + len(literal)
    return pos <= end


def _robots_from_bytes(body: bytes) -> _Robots:
    try:
        lines = body.decode('utf-8-sig').splitlines()
    except UnicodeError:
        raise PublicSourceError('Unrecognized robots.txt encoding') from None
    if any(line.lstrip().startswith('<') for line in lines):
        raise PublicSourceError('Unrecognized robots.txt body')
    groups = []
    agents, directives = [], []
    for raw in lines:
        field, sep, value = raw.split('#', 1)[0].partition(':')
        if not sep:
            continue
        field, value = field.strip().casefold(), value.strip()
        if field == 'user-agent':
            if directives:
                groups.append((agents, directives))
                agents, directives = [], []
            agents.append(value.casefold())
        elif agents and field in {'allow', 'disallow', 'crawl-delay'}:
            directives.append((field, value))
    groups.append((agents, directives))
    # Honor our agent's rules and wildcard rules conservatively.
    rules, delay = [], 0.0
    for agents, directives in groups:
        if not ({'*', _ROBOT_NAME} & set(agents)):
            continue
        for field, value in directives:
            if field == 'crawl-delay':
                if re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,3})?', value):
                    delay = max(delay, float(value))
                continue
            if value.startswith('/'):
                pattern = _normalized_path(value)
                if len(rules) >= 4000 or len(pattern) > 2048:
                    raise PublicSourceError('robots.txt exceeds rule limits')
                rules.append((pattern, len(pattern.rstrip('$').replace('*', '')), field == 'allow'))
    return _Robots(tuple(rules), delay)


def _challenge(body: bytes) -> bool:
    return bool(re.search(rb'(?i)(?:<title[^>]*>\s*(?:just a moment|access denied|captcha)|'
                          rb'cf-chl-|captcha-delivery\.com|/challenge-platform/|verify you are human|'
                          rb'checking your browser|g-recaptcha|h-captcha|"captcha_required")', body[:131072]))


class PublicFetcher:
    """Run-scoped rate/robots/body cache; use one instance across checkpoints."""
    def __init__(self, *, max_requests=100, timeout=8, max_bytes=1_500_000, min_delay=1.0):
        if not (1 <= max_requests <= 250 and 1 <= timeout <= 20 and 4096 <= max_bytes <= 4_000_000 and 0 <= min_delay <= 10):
            raise ValueError('Unsafe public fetch budget')
        self.max_requests, self.timeout, self.max_bytes, self.min_delay = max_requests, timeout, max_bytes, min_delay
        self.requests = 0
        self._robots: dict[str, _Robots] = {}
        self._blocked: dict[str, str] = {}
        self._last: dict[str, float] = {}
        self._cache: dict[str, PublicResponse] = {}
        self._lock = asyncio.Lock()

    async def _get(self, url, host, delay, *, allow_redirect=False):
        if self.requests >= self.max_requests:
            raise PublicSourceError('Public request budget exhausted', retryable=True)
        if host in self._blocked:
            raise PublicSourceError(self._blocked[host])
        pace = max(self.min_delay, delay, 5.2 if host == 'api.gdeltproject.org' else 0)
        if pace > 10:
            raise PublicSourceError('Provider crawl delay exceeds run budget')
        wait = self._last.get(host, 0) + pace - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.requests += 1
        self._last[host] = time.monotonic()
        try:
            response = await asyncio.wait_for(asyncio.to_thread(_request, url, timeout=self.timeout, max_bytes=self.max_bytes), self.timeout + 1)
        except TimeoutError:
            raise PublicSourceError('Public HTTPS request timed out', retryable=True) from None
        if response.status in {401, 403, 429, 451} or _challenge(response.body):
            reason = f'Public provider blocked this run (HTTP {response.status} / challenge)'
            self._blocked[host] = reason
            raise PublicSourceError(reason)
        if 300 <= response.status < 400 and not allow_redirect:
            raise PublicSourceError('Redirect not followed')
        return response

    async def fetch(self, url, *, media=('text/html', 'application/xhtml+xml')) -> PublicResponse:
        url = public_https_url(url)
        parts = urlsplit(url)
        host = parts.hostname
        async with self._lock:
            if host in self._blocked:
                raise PublicSourceError(self._blocked[host])
            if url in self._cache:
                response = self._cache[url]
                if response.media_type not in media:
                    raise PublicSourceError('Unexpected public response media type')
                return response
            if host not in self._robots:
                try:
                    robots = await self._get(f'https://{host}/robots.txt', host, 0)
                    if robots.status in {404, 410, 204}:
                        self._robots[host] = _Robots()
                    elif robots.status == 200 and robots.media_type in {'text/plain', ''} and len(robots.body) <= 524288:
                        self._robots[host] = _robots_from_bytes(robots.body)
                    else:
                        raise PublicSourceError('Cannot establish robots.txt permission', retryable=500 <= robots.status <= 599)
                except PublicSourceError as exc:
                    if not exc.retryable:
                        self._blocked.setdefault(host, str(exc))
                    raise
            rules = self._robots[host]
            from urllib.parse import unquote
            suffix = f'?{parts.query}' if parts.query else ''
            if not all(rules.allows(path + suffix) for path in (parts.path, unquote(parts.path))):
                raise PublicSourceError('robots.txt disallows this path')
            response = await self._get(url, host, rules.delay)
            if response.status != 200:
                raise PublicSourceError(f'Public source returned HTTP {response.status}', retryable=500 <= response.status <= 599)
            if response.media_type not in media:
                raise PublicSourceError('Unexpected public response media type')
            self._cache[url] = response
            return response

    async def resolve_grounding_redirect(self, url: str) -> str:
        """Resolve one Google-grounding redirect without enabling general redirects.

        The API returns these URLs as citations. The final destination is still
        independently validated and fetched through ``fetch()``, including its
        own robots.txt policy. Redirects from all other hosts remain prohibited.
        """
        url=public_https_url(url)
        parts=urlsplit(url)
        if parts.hostname != _GOOGLE_GROUNDING_HOST or not parts.path.startswith('/grounding-api-redirect/'):
            raise PublicSourceError('Not a Google grounding redirect')
        async with self._lock:
            # This is the sole allowed redirect response in the public fetcher.
            # Its Location is validated below; no network request follows it here.
            response=await self._get(url,parts.hostname,0,allow_redirect=True)
        if response.status not in {301,302,303,307,308} or not response.redirect_location:
            raise PublicSourceError('Google grounding redirect has no destination')
        destination=public_https_url(response.redirect_location)
        if urlsplit(destination).hostname == _GOOGLE_GROUNDING_HOST:
            raise PublicSourceError('Nested Google grounding redirect is not permitted')
        return destination
