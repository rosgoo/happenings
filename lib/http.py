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

CONTACT = os.environ.get("HAPPENINGS_CONTACT", "https://github.com/rosgoo/happenings")
UA = f"happenings/0.1 (+{CONTACT})"

DEFAULT_DELAY = 1.0
_last_hit = {}
_robots = {}


def _wait(host, delay):
    """One shared clock per host. Concurrency here would defeat the point."""
    prev = _last_hit.get(host)
    if prev is not None:
        gap = time.time() - prev
        if gap < delay:
            time.sleep(delay - gap)
    _last_hit[host] = time.time()


def allowed(url):
    """Check robots.txt before the first request to a host, then cache it.

    A host that won't serve robots.txt is treated as permissive -- that is the
    conventional reading, and these are documented open-data endpoints.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    if host not in _robots:
        rp = RobotFileParser()
        rp.set_url(f"{parts.scheme}://{host}/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        _robots[host] = rp
    rp = _robots[host]
    return True if rp is None else rp.can_fetch(UA, url)


def get(url, delay=DEFAULT_DELAY, retries=2, timeout=45, check_robots=True):
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
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "application/json"}
        )
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
