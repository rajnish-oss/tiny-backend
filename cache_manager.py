"""
Cache Manager — Stores and retrieves job results with TTL-based freshness.
Keeps results fresh without re-scraping on every request.
"""

import json
import os
from datetime import datetime, timedelta
from config import CACHE_FILE, CACHE_TTL_HOURS


class CacheManager:
    """Simple JSON-based cache with TTL support."""

    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self._ensure_cache_exists()

    def _ensure_cache_exists(self):
        if not os.path.exists(self.cache_file):
            self._write_cache({"searches": {}, "last_updated": None})

    def _read_cache(self):
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {"searches": {}, "last_updated": None}

    def _write_cache(self, data):
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_cached_results(self, cache_key):
        """
        Get cached results if they exist and are fresh.

        Returns:
            (jobs_list, is_fresh) tuple. is_fresh=False means cache miss.
        """
        cache = self._read_cache()
        entry = cache.get("searches", {}).get(cache_key)

        if not entry:
            return [], False

        cached_at = datetime.fromisoformat(entry["cached_at"])
        ttl = timedelta(hours=CACHE_TTL_HOURS)

        if datetime.now() - cached_at > ttl:
            return entry.get("jobs", []), False  # Stale, return but flag as not fresh

        return entry.get("jobs", []), True  # Fresh cache hit

    def save_results(self, cache_key, jobs, prefs):
        """Save job results to cache."""
        cache = self._read_cache()
        cache["searches"][cache_key] = {
            "jobs": jobs,
            "preferences": prefs,
            "cached_at": datetime.now().isoformat(),
            "count": len(jobs),
        }
        cache["last_updated"] = datetime.now().isoformat()
        self._write_cache(cache)

    def clear_cache(self):
        """Clear all cached results."""
        self._write_cache({"searches": {}, "last_updated": None})

    @staticmethod
    def make_cache_key(role, location, seniority, keywords=""):
        """Generate a cache key from search preferences."""
        return f"{role}|{location}|{seniority}|{keywords}".lower().strip()
