import os
import re
import requests
from bs4 import BeautifulSoup
from typing import Dict, List, Optional

BASE_WEB = "https://www.moltbook.com"

def _get(url: str) -> str:
    headers = {"User-Agent": os.getenv("USER_AGENT", "MoltGraphCrawler/0.1")}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text

def scrape_agent_page(agent_name: str) -> Dict[str, object]:
    """
    Best-effort scrape of:
      - Human owner X handle/link (if present)
      - Similar agents (names + any visible tags)
    This is NOT guaranteed stable; keep behind SCRAPE_AGENT_HTML=1.
    """
    html = _get(f"{BASE_WEB}/u/{agent_name}")
    soup = BeautifulSoup(html, "lxml")

    out: Dict[str, object] = {}

    # Human owner: look for x.com / twitter.com links
    x_link = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "x.com/" in href or "twitter.com/" in href:
            x_link = href
            break
    if x_link:
        m = re.search(r"(x\.com|twitter\.com)/([^/?#]+)", x_link)
        if m:
            out["owner_x_handle"] = m.group(2)
            out["owner_x_url"] = x_link

    # Similar agents: find "/u/<name>" links near "Similar Agents"
    similar: List[str] = []
    text = soup.get_text(" ", strip=True)
    if "Similar Agents" in text:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/u/"):
                name = href.split("/u/")[1].split("/")[0]
                if name and name.lower() != agent_name.lower():
                    similar.append(name)
    out["similar_agents"] = sorted(set(similar))

    return out
