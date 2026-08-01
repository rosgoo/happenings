#!/usr/bin/env python3
"""The quality gate. Runs before the commit, because the commit is the deploy.

Two halves, and only one of them is inherited.

STRUCTURAL carries verbatim from gearherd: required fields, unique ids, times
that parse, categories inside the closed vocabulary.

REGRESSION does NOT. gearherd checks "count must not fall >25% vs the last
commit", which assumes a stable entity set. Here the set turns over every week
and a Monday always looks catastrophic beside a Saturday -- that check would
either fire constantly or be set so loose it never fires.

It is replaced by a PER-SOURCE FRESHNESS gate, because the failure mode is
different. A broken parser in gearherd produces wrong data; a broken adapter
here produces *nothing*, silently, forever, and nobody notices until someone
asks why a venue hasn't listed anything since March.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import taxonomy  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
REQUIRED = ("id", "title", "start_utc", "start_local", "date_local",
            "venue_id", "category", "subcategory", "source")

# The multi-city rule, made enforceable. Principles rot; a grep doesn't.
CITY_LITERALS = [r"America/New_York", r"\bNYC\b", r"New York City"]
SCAN_DIRS = ("lib",)
SCAN_FILES = ("fetch.py", "normalize.py")  # not validate.py: it defines the patterns


class Gate:
    def __init__(self):
        self.errors, self.warnings = [], []

    def err(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def check_structure(g, events):
    ids = Counter()
    bad_cat = Counter()
    for e in events:
        for f in REQUIRED:
            if e.get(f) in (None, ""):
                g.err(f"missing {f}: {e.get('title')!r}")
                break
        ids[e["id"]] += 1
        if not taxonomy.valid(e.get("category"), e.get("subcategory")):
            bad_cat[f"{e.get('category')}/{e.get('subcategory')}"] += 1
        if e.get("end_local") and e["end_local"] < e["start_local"]:
            g.warn(f"end before start: {e['title']!r} @ {e['start_local']}")
        # is_free must never be inferred. Unknown price is null, not false.
        if e.get("is_free") is True and e.get("price_min") not in (None, 0):
            g.err(f"is_free true with a nonzero price: {e['title']!r}")

    dupes = [i for i, n in ids.items() if n > 1]
    if dupes:
        g.err(f"{len(dupes)} duplicate event ids (a collision means one event "
              f"silently overwrites another)")
    for k, n in bad_cat.items():
        g.err(f"{n} events outside the closed vocabulary: {k}")


def check_freshness(g, city, events, meta):
    """The replacement for the regression gate. A source that goes quiet fails
    the build -- silence is the failure mode here, not wrongness."""
    with open(os.path.join(ROOT, "cities", city, "sources.json")) as f:
        sources = json.load(f)["sources"]
    per_source = Counter(e["source"] for e in events)
    for s in sources:
        if not s.get("enabled"):
            continue
        n = per_source.get(s["id"], 0)
        floor = s.get("min_expected", 1)
        if n == 0:
            g.err(f"source '{s['id']}' produced NOTHING -- adapter has rotted or "
                  f"the upstream shape changed")
        elif n < floor:
            g.err(f"source '{s['id']}' produced {n}, below its floor of {floor}")

    now = datetime.now(timezone.utc)
    soon = sum(1 for e in events
               if now <= datetime.fromisoformat(e["start_utc"]) <= now + timedelta(days=7))
    if soon < 25:
        g.err(f"only {soon} events in the next 7 days -- the index is effectively empty")
    elif soon < 100:
        g.warn(f"only {soon} events in the next 7 days")


def check_city_literals(g):
    """No timezone literal or city name outside cities/. This is what keeps
    multi-city from becoming a rewrite eighteen months from now."""
    targets = list(SCAN_FILES)
    for d in SCAN_DIRS:
        for root, _, files in os.walk(os.path.join(ROOT, d)):
            targets += [os.path.join(root, f) for f in files if f.endswith(".py")]
    for path in targets:
        full = path if os.path.isabs(path) else os.path.join(ROOT, path)
        if not os.path.exists(full):
            continue
        try:
            src = open(full, encoding="utf8").read()
        except OSError:
            continue
        # Comments and docstrings are prose, not configuration.
        code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
        code = re.sub(r'"""[\s\S]*?"""', "", code)
        for pat in CITY_LITERALS:
            if re.search(pat, code):
                g.err(f"city literal /{pat}/ in {os.path.relpath(full, ROOT)} -- "
                      f"belongs in cities/<slug>/city.json")


def check_git_regression(g, city, events, max_drop):
    """Not a count gate -- a *structural* one. Catches a taxonomy or resolver
    change silently zeroing a facet, which counts alone would hide."""
    rel = f"data/{city}/meta.json"
    try:
        prev = json.loads(subprocess.check_output(
            ["git", "show", f"HEAD:{rel}"], cwd=ROOT, stderr=subprocess.DEVNULL))
    except Exception:
        g.warn("no committed meta.json to compare against (first run)")
        return
    old_cats = set(prev.get("by_category", {}))
    new_cats = set(Counter(e["category"] for e in events))
    gone = old_cats - new_cats
    if gone:
        g.err(f"categories vanished entirely: {sorted(gone)}")
    old_hood = prev.get("coverage", {}).get("neighborhood", 0)
    new_hood = round(100 * sum(1 for e in events if e["neighborhood"]) / max(1, len(events)), 1)
    if old_hood - new_hood > max_drop:
        g.err(f"neighbourhood coverage fell {old_hood}% -> {new_hood}% "
              f"(>{max_drop} points). Use --max-drop if this is intended.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--max-drop", type=float, default=10.0,
                    help="allowed coverage drop in points before failing")
    args = ap.parse_args()

    d = os.path.join(ROOT, "data", args.city)
    events = json.load(open(os.path.join(d, "events.json")))
    meta = json.load(open(os.path.join(d, "meta.json")))

    g = Gate()
    check_structure(g, events)
    check_freshness(g, args.city, events, meta)
    check_city_literals(g)
    check_git_regression(g, args.city, events, args.max_drop)

    print(f"validate: {args.city} · {len(events)} events")
    for w in g.warnings[:20]:
        print(f"  warn  {w}")
    if len(g.warnings) > 20:
        print(f"  warn  … and {len(g.warnings) - 20} more")
    for e in g.errors[:20]:
        print(f"  FAIL  {e}")
    if len(g.errors) > 20:
        print(f"  FAIL  … and {len(g.errors) - 20} more")

    if g.errors:
        print(f"\n  {len(g.errors)} error(s) -- blocking the commit")
        return 1
    print(f"  ok ({len(g.warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
