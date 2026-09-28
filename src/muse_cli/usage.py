"""Weekly usage for the account, as shown under Settings > General > Usage.

The Settings panel reads usage through a Next.js server action
(fetchSubscriptionAction); this module calls the same action. Server action IDs
can change between builds, so the ID is cached and rediscovered only when the
server answers with x-nextjs-action-not-found. Discovery walks the app's JS
chunks for the createServerReference call registered under that name.
"""
import json
import os
import re
import sys
import time
from collections import deque

ACTION_NAME = "fetchSubscriptionAction"
# Last known ID, so a fresh install does not have to crawl on its first call.
DEFAULT_ACTION_ID = "40749192384a7f6ed87eaca9474fba7d9e9e0d98c9"
_ID_RE = re.compile(r"^[0-9a-f]{40,44}$")
# No quote or parenthesis between the ID and the name: both must belong to the
# same createServerReference call. A looser gap can pair the name with the ID
# of the previous call, and that ID would then be POSTed as a server action.
_REF_RE = re.compile(r'createServerReference\)\("([0-9a-f]{40,44})",[^"()]*"'
                     + ACTION_NAME + r'"\)')
_CHUNK_RE = re.compile(r'(?:/_next/)?static/chunks/[A-Za-z0-9_\-~.]+\.js')
_PAYLOAD_RE = re.compile(r"^1:(\{.*\})\s*$", re.M)
MAX_CHUNKS = 1500
DISCOVERY_BUDGET_S = 180


class ActionNotFound(RuntimeError):
    pass


def _load_cached_id(cache_path):
    try:
        with open(cache_path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return DEFAULT_ACTION_ID
    aid = data.get("action_id") if isinstance(data, dict) else None
    return aid if isinstance(aid, str) and _ID_RE.match(aid) else DEFAULT_ACTION_ID


def _save_cached_id(cache_path, action_id):
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as fh:
            json.dump({"action_id": action_id, "found_at": int(time.time())}, fh)
    except OSError as e:
        # The ID is already known for this run; a failed cache write only means
        # the next run may have to rediscover it.
        print(f"warning: could not cache usage action ID: {e}", file=sys.stderr)


def find_action_id(js):
    m = _REF_RE.search(js)
    return m.group(1) if m else None


def discover_action_id(cookies):
    """Return (action_id, chunks_scanned). The settings panel is lazy-loaded,
    so the reference sits in a chunk reachable only from other chunks."""
    from curl_cffi import requests as rq
    print("usage: action ID changed, rediscovering from muse.ai JS (can take a minute)...",
          file=sys.stderr)
    deadline = time.monotonic() + DISCOVERY_BUDGET_S
    s = rq.Session(impersonate="chrome")
    home = s.get("https://muse.ai/", headers={"Cookie": cookies}, timeout=30)
    if home.status_code != 200:
        raise RuntimeError(f"rediscovery: muse.ai/ -> {home.status_code}")
    queue = deque(u.split("?")[0] for u in re.findall(r'<script[^>]+src=["\']([^"\']+)', home.text))
    seen = set()
    while queue and len(seen) < MAX_CHUNKS:
        if time.monotonic() > deadline:
            raise RuntimeError(f"rediscovery: gave up after {DISCOVERY_BUDGET_S}s "
                               f"({len(seen)} JS chunks scanned)")
        path = queue.popleft()
        if path in seen:
            continue
        seen.add(path)
        try:
            r = s.get("https://muse.ai" + path, timeout=30)
        except Exception:                               # noqa: BLE001
            continue
        if r.status_code == 429:
            raise RuntimeError("rediscovery: rate limited by muse.ai (429)")
        if r.status_code != 200:
            continue
        aid = find_action_id(r.text)
        if aid:
            return aid, len(seen)
        for ref in _CHUNK_RE.findall(r.text):
            p = ref if ref.startswith("/_next/") else "/_next/" + ref
            if p not in seen:
                queue.append(p)
    raise RuntimeError(f"{ACTION_NAME} not found in {len(seen)} JS chunks; "
                       "muse.ai may have renamed it")


def parse_subscription(text):
    """Pull the subscription out of the RSC reply. Only the shape seen from the
    Settings panel is accepted; anything else is an error, not a partial result."""
    m = _PAYLOAD_RE.search(text)
    if not m:
        raise RuntimeError("usage: unexpected response format")
    payload = json.loads(m.group(1))
    if not payload.get("success"):
        raise RuntimeError(f"usage: {payload.get('error') or 'request failed'}")
    sub = payload.get("subscription")
    usage = sub.get("usage") if isinstance(sub, dict) else None
    if not isinstance(usage, dict) or not isinstance(usage.get("percentUsed"), (int, float)):
        raise RuntimeError("usage: response has no usage.percentUsed")
    return sub


def _call(cookies, action_id):
    from curl_cffi import requests as rq
    from .gateway import AuthError, _hatch_headers
    h = _hatch_headers(cookies)
    h.update({"next-action": action_id, "Accept": "text/x-component",
              "Content-Type": "text/plain;charset=UTF-8"})
    r = rq.post("https://muse.ai/", headers=h, data='[{"includeAgreement":true}]',
                impersonate="chrome", timeout=30)
    if r.headers.get("x-nextjs-action-not-found") == "1":
        raise ActionNotFound(action_id)
    if r.status_code == 401:
        raise AuthError("usage -> 401 (cookies expired? re-export)")
    if r.status_code != 200:
        raise RuntimeError(f"usage -> {r.status_code} {r.text[:120]}")
    return parse_subscription(r.text)


def fetch_usage(cookies, cache_path):
    action_id = _load_cached_id(cache_path)
    try:
        sub = _call(cookies, action_id)
    except ActionNotFound:
        action_id, _ = discover_action_id(cookies)
        _save_cached_id(cache_path, action_id)
        sub = _call(cookies, action_id)
    tier = sub.get("tier") or {}
    usage = sub["usage"]
    return {
        "tier": tier.get("tierCode"),
        "tier_name": tier.get("name"),
        "paid": tier.get("isPaid"),
        "weekly": {
            "percent_used": usage.get("percentUsed"),
            "status": usage.get("quotaStatus"),
            "resets_at": usage.get("resetsAt"),
            "label": sub.get("usageRowValueLabel"),
        },
        # Raw balance units are undocumented; the label is what the UI shows.
        "topup": {
            "balance": sub.get("topupBalance"),
            "total": sub.get("topupTotal"),
            "label": sub.get("topupRowValueLabel"),
        },
    }
