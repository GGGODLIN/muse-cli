"""Weekly usage for the account, as shown under Settings > General > Usage.

The web app reads it through a Next.js server action, not a REST route or a
gateway method. A server action is addressed by an ID that changes with each
muse.ai deploy, so the ID is cached and rediscovered only when the server
answers with x-nextjs-action-not-found. Discovery walks the app's JS chunks
for the createServerReference call registered as fetchSubscriptionAction.
"""
import json
import os
import re
import time
from collections import deque

ACTION_NAME = "fetchSubscriptionAction"
# Last known ID, so a fresh install does not have to crawl on its first call.
DEFAULT_ACTION_ID = "40749192384a7f6ed87eaca9474fba7d9e9e0d98c9"
_REF_RE = re.compile(r'createServerReference\)\("([0-9a-f]{40,44})",[^;]{0,200}?"'
                     + ACTION_NAME + r'"')
_CHUNK_RE = re.compile(r'(?:/_next/)?static/chunks/[A-Za-z0-9_\-~.]+\.js')
_PAYLOAD_RE = re.compile(r"^1:(\{.*\})\s*$", re.M)
MAX_CHUNKS = 1500


class ActionNotFound(RuntimeError):
    pass


def _load_cached_id(cache_path):
    try:
        with open(cache_path) as fh:
            return json.load(fh)["action_id"]
    except (OSError, ValueError, KeyError):
        return DEFAULT_ACTION_ID


def _save_cached_id(cache_path, action_id):
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump({"action_id": action_id, "found_at": int(time.time())}, fh)


def discover_action_id(cookies):
    """Return (action_id, chunks_scanned). The settings panel is lazy-loaded,
    so the reference sits in a chunk reachable only from other chunks."""
    from curl_cffi import requests as rq
    s = rq.Session(impersonate="chrome")
    html = s.get("https://muse.ai/", headers={"Cookie": cookies}, timeout=30).text
    queue = deque(u.split("?")[0] for u in re.findall(r'<script[^>]+src=["\']([^"\']+)', html))
    seen = set()
    while queue and len(seen) < MAX_CHUNKS:
        path = queue.popleft()
        if path in seen:
            continue
        seen.add(path)
        try:
            js = s.get("https://muse.ai" + path, timeout=30).text
        except Exception:                               # noqa: BLE001
            continue
        m = _REF_RE.search(js)
        if m:
            return m.group(1), len(seen)
        for ref in _CHUNK_RE.findall(js):
            p = ref if ref.startswith("/_next/") else "/_next/" + ref
            if p not in seen:
                queue.append(p)
    raise RuntimeError(f"{ACTION_NAME} not found in {len(seen)} JS chunks; "
                       "muse.ai may have renamed it")


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
    m = _PAYLOAD_RE.search(r.text)
    if r.status_code != 200 or not m:
        raise RuntimeError(f"usage -> {r.status_code} {r.text[:120]}")
    payload = json.loads(m.group(1))
    if not payload.get("success") or not payload.get("subscription"):
        raise AuthError(f"usage -> {payload.get('error') or 'no subscription'} "
                        "(cookies expired? re-export)")
    return payload["subscription"]


def fetch_usage(cookies, cache_path):
    action_id = _load_cached_id(cache_path)
    try:
        sub = _call(cookies, action_id)
    except ActionNotFound:
        action_id, _ = discover_action_id(cookies)
        _save_cached_id(cache_path, action_id)
        sub = _call(cookies, action_id)
    tier = sub.get("tier") or {}
    usage = sub.get("usage") or {}
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
