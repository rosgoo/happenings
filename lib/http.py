"""Polite HTTP. Stdlib only -- the crawl has to run anywhere.

Carries over from gearherd's fetch.py: identify yourself, pace requests, honour
Retry-After, never retry a block. The politeness is not decoration; it is the
difference between reading public data and being a problem.
"""
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.robotparser import RobotFileParser

CONTACT = os.environ.get("HAPPENINGS_CONTACT", "https://happenings.town")
UA = f"happenings/0.1 (+{CONTACT})"

DEFAULT_DELAY = 1.0
_last_hit = {}
_robots = {}

class _Redirect308(urllib.request.HTTPRedirectHandler):
    """Follow 308 Permanent Redirect on Python < 3.11.

    urllib gained 308 support in 3.11. Before that TWO things are missing, and
    fixing only the obvious one does nothing: the dispatch method
    `http_error_308`, and `redirect_request`'s allowlist, which raises for any
    code outside (301, 302, 303, 307). Patch just the first and the second still
    rejects it, so the redirect is lost either way.

    This matters beyond tidiness: a venue that has moved behind a 308 is
    indistinguishable from one that is dead, and the census would file it as a
    negative result. The pipeline is meant to run anywhere, and anywhere
    includes the 3.9 that ships with macOS.

    308 is mapped to 307 rather than 301 because 307 is the method-preserving
    redirect, which is exactly 308's semantics.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, 307 if code == 308 else code,
                                        msg, headers, newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_301


if not hasattr(urllib.request.HTTPRedirectHandler, "http_error_308"):
    urllib.request.install_opener(urllib.request.build_opener(_Redirect308()))


def _wait(host, delay):
    """One shared clock per host. Concurrency here would defeat the point."""
    prev = _last_hit.get(host)
    if prev is not None:
        gap = time.time() - prev
        if gap < delay:
            time.sleep(delay - gap)
    _last_hit[host] = time.time()


def _load_robots(scheme, host):
    """Fetch and parse robots.txt for one host.

    Deliberately does NOT use RobotFileParser.read(), which follows redirects
    blindly and will happily parse an HTML page as rules. api.wikimedia.org
    redirects /robots.txt to a documentation page on another host; parsed as
    robots that produced a phantom blanket disallow and silently blocked a
    perfectly permitted API.

    Per RFC 9309, robots.txt must be served by the same authority as the
    request, and an unavailable robots.txt means unrestricted. So: no
    cross-host redirects, must look like text, otherwise permissive.
    """
    url = f"{scheme}://{host}/robots.txt"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if urllib.parse.urlsplit(r.geturl()).netloc != host:
                return None                       # redirected off-authority
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/plain" not in ctype:
                return None                       # not a robots file
            body = r.read(512_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 401/403 mean the whole site is off limits; other 4xx/5xx mean
        # "no robots.txt", which is unrestricted.
        return "DENY" if e.code in (401, 403) else None
    except Exception:
        return None
    rp = RobotFileParser()
    rp.parse(body.splitlines())
    return rp


def allowed(url):
    """Check robots.txt before the first request to a host, then cache it."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    if host not in _robots:
        _robots[host] = _load_robots(parts.scheme, host)
    rp = _robots[host]
    if rp is None:
        return True
    if rp == "DENY":
        return False
    return rp.can_fetch(UA, url)


def get(url, delay=DEFAULT_DELAY, retries=2, timeout=45, check_robots=True,
        max_bytes=None):
    """GET a URL politely. Returns bytes, or raises after retries.

    Retry-After is obeyed exactly. A 4xx that is not 429 is not retried --
    the server has told us the answer and asking again is just noise.
    """
    if check_robots and not allowed(url):
        raise PermissionError(f"robots.txt disallows {url}")

    host = urllib.parse.urlsplit(url).netloc
    attempt = 0
    while True:
        _wait(host, delay)
        headers = {"User-Agent": UA, "Accept-Encoding": "gzip",
                   "Accept": "application/json"}
        if max_bytes:
            # Only the head of the document. An article's infobox sits near the
            # top, so pulling 1.3 MB to read one <img> is waste we impose on
            # someone else's server.
            headers["Range"] = f"bytes=0-{max_bytes}"
            headers.pop("Accept-Encoding")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                if attempt >= retries:
                    raise
                wait = int(e.headers.get("Retry-After") or (2 ** attempt) * 5)
                print(f"    {e.code} on {host}; waiting {wait}s (attempt {attempt + 1}/{retries})")
                time.sleep(wait)
                attempt += 1
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt >= retries:
                raise
            time.sleep((2 ** attempt) * 3)
            attempt += 1


def get_json(url, **kw):
    return json.loads(get(url, **kw).decode("utf-8"))


HTML_ACCEPT = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"


def get_full(url, delay=DEFAULT_DELAY, timeout=25, check_robots=True,
             accept=HTML_ACCEPT, max_bytes=800_000):
    """GET a URL and report what happened, instead of raising.

    `get()` is for sources we have already decided to trust, where a failure
    should be loud. This is for reconnaissance, where the status line IS the
    finding: a 404 on /wp-json is an answer, not an error, and a good share of
    the hosts this visits are expected to be dead or hostile.

    Always returns a dict -- {status, ctype, url, headers, body, error}.
    `status` is None when nothing was reached at all, and `url` is the final URL
    after redirects, which is itself a signal (a venue calendar that 302s to
    Eventbrite has told you where its events live).

    `headers` is there for the same reason as `status`: some answers only live
    up there. WordPress reports the size of a collection in `X-WP-Total`, so one
    request tells you whether a feed holds 5 events or 5,000 -- paging to find
    that out would be several hundred needless requests across a census.

    Does not advertise gzip. The body is read to a cap, and a truncated gzip
    stream is undecompressable -- costing a little bandwidth to keep every
    response readable is the right trade for a one-off census.
    """
    if check_robots and not allowed(url):
        return {"status": None, "ctype": "", "url": url, "headers": {},
                "body": b"", "error": "robots-denied"}

    host = urllib.parse.urlsplit(url).netloc
    _wait(host, delay)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "ctype": (r.headers.get("Content-Type") or "").lower(),
                    "url": r.geturl(), "headers": dict(r.headers),
                    "body": r.read(max_bytes), "error": None}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(max_bytes)
        except Exception:
            body = b""
        return {"status": e.code, "ctype": (e.headers.get("Content-Type") or "").lower() if e.headers else "",
                "url": e.geturl() if hasattr(e, "geturl") else url,
                "headers": dict(e.headers) if e.headers else {},
                "body": body, "error": None}
    except Exception as e:
        return {"status": None, "ctype": "", "url": url, "headers": {},
                "body": b"", "error": f"{type(e).__name__}: {e}"}
