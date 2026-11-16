"""Poll every source in feeds.yaml and store headlines that haven't been seen before."""
import calendar
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import feedparser
import httpx
import yaml

from . import store

FEEDS_FILE = os.getenv("FEEDS_FILE", "feeds.yaml")
UA = {"User-Agent": "Mozilla/5.0 (news-trading-bot)"}


def _expand(v):
    """Replace ${VARS} from the environment in strings, recursively."""
    if isinstance(v, str):
        return os.path.expandvars(v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def _dig(obj, path):
    for key in filter(None, (path or "").split(".")):
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def to_ms(value):
    """ISO string or unix timestamp (s or ms) -> UTC milliseconds. None if unparseable."""
    if value in (None, ""):
        return None
    try:
        n = float(value)
        return int(n if n > 1e11 else n * 1000)  # > 1e11 can only be milliseconds
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # naive timestamps are assumed UTC
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def fetch(feed):
    """One source -> list of (title, link, published_ms or None)."""
    feed = _expand(feed)
    r = httpx.get(feed["url"], headers={**UA, **feed.get("headers", {})}, timeout=10, follow_redirects=True)
    r.raise_for_status()
    if feed.get("type") == "json":
        items = _dig(r.json(), feed.get("items")) or []
        return [
            (str(_dig(i, feed.get("title", "title")) or "").strip(),
             _dig(i, feed.get("link", "url")) or "",
             to_ms(_dig(i, feed.get("published", "published_at"))))
            for i in items
        ]
    out = []
    for e in feedparser.parse(r.content).entries:
        t = e.get("published_parsed") or e.get("updated_parsed")  # feedparser normalises to UTC
        out.append((e.get("title", "").strip(), e.get("link", ""), calendar.timegm(t) * 1000 if t else None))
    return out


def poll():
    """Fetch all feeds. Returns {feed name: error message} for the ones that failed."""
    with open(FEEDS_FILE) as f:
        feeds = (yaml.safe_load(f) or {}).get("feeds") or []
    errors = {}

    def safe(feed):
        try:
            return fetch(feed)
        except Exception as e:  # one broken feed must not stop the others
            errors[feed.get("name", feed.get("url"))] = f"{type(e).__name__}: {e}"[:200]
            return []

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(safe, feeds))

    now = int(time.time() * 1000)
    for feed, items in zip(feeds, results):
        name = feed.get("name", feed["url"])
        known = store.source_known(name)
        for title, link, published in items:
            if not title:
                continue
            if published is None:
                # No timestamp: treat as breaking only if we've polled this feed before,
                # otherwise the whole backlog would look fresh on first run.
                published = now if known else 0
            # Exact repeats are dropped here by link; the same story reworded by another
            # source is caught later by the screening model (see analyzer.triage).
            uid = hashlib.sha1((link or title).encode()).hexdigest()
            store.add_event(uid, name, title, link, published)
    return errors
