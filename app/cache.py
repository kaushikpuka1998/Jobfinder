#!/usr/bin/env python3
"""
cache.py
========

Thin Redis wrapper for the Apify result cache — replaces the earlier
Mongo-with-a-manually-checked-timestamp approach with Redis's native
key expiry (`SETEX`), which is what a TTL cache should actually be.

Unconfigured (no REDIS_URL) means every get() misses and every set() is a
no-op — callers don't need to branch on whether caching is available.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import redis

from app.config import Config

LOG = logging.getLogger(__name__)

_client: Optional["redis.Redis"] = None
if Config.REDIS_URL:
    try:
        _client = redis.from_url(Config.REDIS_URL, decode_responses=True, socket_timeout=5)
        _client.ping()
    except redis.RedisError as exc:
        LOG.warning("Redis configured but unreachable (%s); caching disabled.", exc)
        _client = None


def get(key: str) -> Optional[Any]:
    if not _client:
        return None
    try:
        raw = _client.get(key)
    except redis.RedisError as exc:
        LOG.warning("Redis GET failed: %s", exc)
        return None
    return json.loads(raw) if raw else None


def set(key: str, value: Any, ttl_seconds: int) -> None:
    if not _client:
        return
    try:
        _client.setex(key, ttl_seconds, json.dumps(value))
    except redis.RedisError as exc:
        LOG.warning("Redis SETEX failed: %s", exc)
