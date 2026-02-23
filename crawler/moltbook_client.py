import os
import time
import requests
import urllib.parse
from typing import Any, Dict, List, Optional, Union

class MoltbookClient:
    """
    Drop-in replacement Moltbook API client with:
      - rate limiting (REQUESTS_PER_MINUTE)
      - retry + exponential backoff for 429/502/503/504
      - response-shape tolerant helpers for list endpoints
    """

    def __init__(self):
        self.base = os.getenv("MOLTBOOK_BASE_URL", "https://www.moltbook.com/api/v1").rstrip("/")
        self.api_key = os.getenv("MOLTBOOK_API_KEY")
        self.ua = os.getenv("USER_AGENT", "MoltGraphCrawler/0.1")
        self.rpm = int(os.getenv("REQUESTS_PER_MINUTE", "80"))
        self._min_interval = 60.0 / max(self.rpm, 1)
        self._last = 0.0

    def _sleep_if_needed(self):
        now = time.time()
        dt = now - self._last
        if dt < self._min_interval:
            time.sleep(self._min_interval - dt)
        self._last = time.time()

    def _req(self, method: str, path: str, params=None, *, no_auth: bool = False, extra_headers: Optional[Dict[str, Any]] = None) -> Any:
        """
        Returns parsed JSON. Can be dict or list depending on endpoint.

        Important gotchas (observed in the wild):
          - Some Moltbook API deployments issue redirects (e.g., adding a trailing slash or switching hosts).
            The `requests` library drops `Authorization` on cross-host redirects for safety.
            To avoid silently becoming "anonymous", we do *not* follow redirects automatically.
        """
        url = f"{self.base}{path}"
        headers = {
            "User-Agent": self.ua,
            "Accept": "application/json",
            "Accept-Encoding": os.getenv("ACCEPT_ENCODING", "gzip, deflate"),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if self.api_key and not no_auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if extra_headers:
            headers.update(extra_headers)

        max_tries = int(os.getenv("MAX_RETRIES", "8"))
        backoff = float(os.getenv("RETRY_BACKOFF_SECONDS", "1.5"))
        timeout = int(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))

        last_exc = None

        def _do_request(final_url: str):
            self._sleep_if_needed()
            return requests.request(method, final_url, headers=headers, params=params, timeout=timeout, allow_redirects=False)

        for attempt in range(1, max_tries + 1):
            try:
                r = _do_request(url)

                # Manual redirect handling (preserve headers + params)
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location")
                    if loc:
                        # Join relative redirects and retry once immediately
                        url2 = urllib.parse.urljoin(url, loc)
                        r = _do_request(url2)

                # Retryable status codes
                if r.status_code in (429, 502, 503, 504):
                    # if r.status_code == 429:
                    #     reset = r.headers.get("X-RateLimit-Reset")
                    #     if reset:
                    #         try:
                    #             wait = max(float(reset) - time.time(), 1.0)
                    #             time.sleep(wait)
                    #             continue
                    #         except Exception:
                    #             pass
                    if r.status_code == 429:
                        # Prefer standard header
                        ra = r.headers.get("Retry-After")
                        if ra:
                            try:
                                time.sleep(max(float(ra), 1.0))
                                continue
                            except Exception:
                                pass

                        # Some deployments use this custom header
                        reset = r.headers.get("X-RateLimit-Reset")
                        if reset:
                            try:
                                wait = max(float(reset) - time.time(), 1.0)
                                time.sleep(wait)
                                continue
                            except Exception:
                                pass

                        # Fallback: guaranteed cooldown (prevents hammering)
                        time.sleep(min(max(backoff, 10.0), 60.0))
                        continue

                    if attempt < max_tries:
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 60.0)
                        continue

                r.raise_for_status()

                if not r.content:
                    return {}
                return r.json()

            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < max_tries:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("Request failed with unknown error")


    # --------------------------
    # Response-shape helpers
    # --------------------------
    @staticmethod
    def _list_from(resp: Any, preferred_keys: List[str]) -> List[Dict[str, Any]]:
        """
        Extract a list from a response that may be dict or list.
        """
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            for k in preferred_keys:
                v = resp.get(k)
                if isinstance(v, list):
                    return v
        return []

    @staticmethod
    def _dict_from(resp: Any, preferred_keys: List[str]) -> Dict[str, Any]:
        if isinstance(resp, dict):
            for k in preferred_keys:
                v = resp.get(k)
                if isinstance(v, dict):
                    return v
        return {} if not isinstance(resp, dict) else resp

    # --- Agents ---
    def get_me(self) -> Dict[str, Any]:
        resp = self._req("GET", "/agents/me")
        # Observed shape: {"agent": {...}}
        if isinstance(resp, dict) and isinstance(resp.get("agent"), dict):
            return resp["agent"]
        return self._dict_from(resp, ["agent"])

    def get_agent_profile(self, name: str) -> Dict[str, Any]:
        # Observed shape: {"agent": {...}, ...} (you already use prof.get("agent", {}))
        resp = self._req("GET", "/agents/profile", params={"name": name})
        return resp if isinstance(resp, dict) else {}

    # --- Submolts ---
    def list_submolts(self, limit: int = 100, offset: int = 0, sort: str = "popular", *, shuffle: bool = True, no_auth: bool = True) -> Dict[str, Any]:
        # Observed shape: {"success": true, "submolts": [...], "count": ..., "total_posts": ...}
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        params = {"limit": limit, "offset": offset, "sort": sort}
        if shuffle:
            params["shuffle"] = int(time.time() * 1000)
        resp = self._req("GET", "/submolts", params=params, no_auth=no_auth)
        return resp if isinstance(resp, dict) else {"submolts": self._list_from(resp, ["submolts", "data"])}

    def get_submolt(self, name: str) -> Dict[str, Any]:
        # Observed (likely): {"submolt": {...}}
        resp = self._req("GET", f"/submolts/{name}")
        if isinstance(resp, dict) and isinstance(resp.get("submolt"), dict):
            return resp["submolt"]
        return self._dict_from(resp, ["submolt"])

    def get_moderators(self, name: str) -> List[Dict[str, Any]]:
        # Observed (likely): {"moderators": [...]}
        resp = self._req("GET", f"/submolts/{name}/moderators")
        return self._list_from(resp, ["moderators", "data"])

    # --- Posts / Comments ---
    def list_posts(self, sort: str = "new", limit: int = 50, offset: int = 0, submolt: Optional[str] = None, *, time_window: Optional[str] = None, shuffle: bool = True, no_auth: bool = True) -> Dict[str, Any]:
        # Observed shape: {"success": true, "posts":[...], "count":..., "has_more":..., "next_offset":...}
        limit = max(1, min(int(limit), 50))
        offset = max(0, int(offset))
        params: Dict[str, Any] = {"sort": sort, "limit": limit, "offset": offset}
        if time_window:
            params["time"] = time_window
        if submolt:
            params["submolt"] = submolt
        if shuffle:
            params["shuffle"] = int(time.time() * 1000)
        resp = self._req("GET", "/posts", params=params, no_auth=no_auth)
        return resp if isinstance(resp, dict) else {"posts": self._list_from(resp, ["posts", "data"])}

    def get_post(self, post_id: str) -> Dict[str, Any]:
        # Observed (likely): {"post": {...}}
        resp = self._req("GET", f"/posts/{post_id}")
        if isinstance(resp, dict) and isinstance(resp.get("post"), dict):
            return resp["post"]
        return self._dict_from(resp, ["post"])

    def get_comments(self, post_id: str, sort: str = "new", limit: int = 500, *, shuffle: bool = True, no_auth: bool = True) -> List[Dict[str, Any]]:
        # Observed: endpoint returns list directly; also support dict fallback
        params = {"sort": sort, "limit": limit}
        if shuffle:
            params["shuffle"] = int(time.time() * 1000)
        resp = self._req("GET", f"/posts/{post_id}/comments", params=params, no_auth=no_auth)
        return self._list_from(resp, ["comments", "data"])

    # --- Personalized feed ---
    def get_feed(self, sort: str = "hot", limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        # Some deployments use {"posts":[...]} others may use {"data":[...]}
        resp = self._req("GET", "/feed", params={"sort": sort, "limit": limit, "offset": offset})
        return resp if isinstance(resp, dict) else {"posts": self._list_from(resp, ["posts", "data"])}
