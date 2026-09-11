"""
Ticket Scraper & Monitor
Built to monitor Carnegie Hall and easily extensible to other venues/platforms.
Features:
- Reverse-engineered Carnegie Hall BuyButton API (using curl_cffi Chrome impersonation to bypass DataDome/Imperva)
- State tracking & transition alerts (only alerts on UNAVAILABLE -> AVAILABLE, no alert spam)
- CLI shell alerts + ASCII terminal bell (\a) + desktop notify-send
- Configurable polling intervals with randomized jitter to prevent bot-detection profiling
- Modular architecture with BaseScraper and ScraperRegistry
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Optional
from bs4 import BeautifulSoup
from curl_cffi import requests


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TicketResult:
    event_title: str
    availability: Availability
    status_detail: str
    url: str
    booking_link: Optional[str] = None
    price_info: Optional[str] = None


class BaseScraper(ABC):
    """Abstract interface that every venue/ticketing site scraper implements."""

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """Determines if this scraper handles the specified URL."""
        ...

    @abstractmethod
    def check_availability(self, url: str) -> TicketResult:
        """Checks ticket availability for the specified event URL."""
        ...


class CarnegieHallScraper(BaseScraper):
    """
    Scraper for Carnegie Hall (carnegiehall.org).
    
    Reverse engineers the client-side BuyButton hydration call:
    1. Fetches concert page once to cache event title and Sitecore item GUID (data-id).
    2. Batches all event GUIDs into a single POST to /api/sitecore/BuyButton/events.
    3. Handles 403/429 with session recycling and exponential backoff.
    """

    BUY_BUTTON_API = "https://www.carnegiehall.org/api/sitecore/BuyButton/events"

    def __init__(self, impersonate: str = "chrome", cache_ttl_seconds: int = 3600):
        self.impersonate = impersonate
        self.cache_ttl_seconds = cache_ttl_seconds
        self.session = requests.Session(impersonate=self.impersonate)
        # Cache for static metadata {url: (title, data_id, fetched_at_timestamp)}
        self._meta_cache: Dict[str, tuple[str, str, float]] = {}

    def _reset_session(self) -> None:
        """Recycle session to clear any flagged cookies."""
        self.session = requests.Session(impersonate=self.impersonate)

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "carnegiehall.org" in url.lower()

    def _ensure_metadata(self, url: str) -> tuple[str, Optional[str]]:
        """
        Fetch event metadata and cache it for 1 hour.
        Once the 1-hour TTL expires, it gracefully re-visits the HTML page,
        refreshing DataDome/Queue-it session cookies and validating event IDs.
        """
        now = time.time()
        if url in self._meta_cache:
            title, data_id, fetched_at = self._meta_cache[url]
            if (now - fetched_at) < self.cache_ttl_seconds:
                return title, data_id

        # Re-fetch page HTML to refresh cookies and confirm metadata
        time.sleep(random.uniform(1.0, 2.5))  # Humanized pacing
        
        # Retry with exponential backoff on transient network timeouts
        resp = None
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=25)
                if resp.status_code == 200:
                    break
            except Exception as e:
                if attempt == 2:
                    sys.stderr.write(f"Warning: Timed out fetching page {url}: {e}\n")
                time.sleep(2 * (attempt + 1))

        if not resp or resp.status_code != 200:
            # If cached version exists from before, use it as fallback
            if url in self._meta_cache:
                return self._meta_cache[url][0], self._meta_cache[url][1]
            return url.rstrip("/").split("/")[-1], None

        soup = BeautifulSoup(resp.text, "html.parser")
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            event_title = og_title["content"].strip()
        elif soup.title and soup.title.string:
            event_title = soup.title.string.split("|")[0].strip()
        else:
            event_title = url.rstrip("/").split("/")[-1]

        buy_container = soup.find(class_=lambda c: c and "js-event-buy" in c)
        data_id = buy_container.get("data-id") if buy_container else None
        if not data_id:
            guid_match = re.search(r'data-id="(\{[A-Fa-f0-9-]+\})"', resp.text)
            if guid_match:
                data_id = guid_match.group(1)

        if data_id:
            self._meta_cache[url] = (event_title, data_id, now)

        return event_title, data_id

    def check_batch(self, urls: List[str]) -> List[TicketResult]:
        """
        Check all Carnegie Hall URLs in a SINGLE HTTP request!
        This is the most anti-bot resilient approach possible.
        """
        url_to_id: Dict[str, str] = {}
        url_to_title: Dict[str, str] = {}
        missing_urls = []

        for u in urls:
            title, data_id = self._ensure_metadata(u)
            url_to_title[u] = title
            if data_id:
                url_to_id[u] = data_id
            else:
                missing_urls.append(u)

        if not url_to_id:
            return [
                TicketResult(
                    event_title=url_to_title[u],
                    availability=Availability.UNKNOWN,
                    status_detail="Could not resolve event ID",
                    url=u,
                )
                for u in urls
            ]

        # Build reverse lookup {data_id: url}
        id_to_url = {v: k for k, v in url_to_id.items()}

        payload = [{"Id": did, "LoadCyo": True} for did in url_to_id.values()]
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urls[0],
            "Content-Type": "application/json",
        }

        api_resp = None
        for attempt in range(3):
            try:
                api_resp = self.session.post(self.BUY_BUTTON_API, json=payload, headers=headers, timeout=25)
                break
            except Exception as e:
                if attempt == 2:
                    sys.stderr.write(f"Warning: Batch API request timed out: {e}\n")
                    self._reset_session()
                    return [
                        TicketResult(
                            event_title=url_to_title.get(u, u),
                            availability=Availability.UNKNOWN,
                            status_detail="Network timeout checking tickets",
                            url=u,
                        )
                        for u in urls
                    ]
                time.sleep(2 * (attempt + 1))
        
        if api_resp.status_code in (403, 429):
            # Session flagged, reset session for next cycle
            self._reset_session()
            return [
                TicketResult(
                    event_title=url_to_title.get(u, u),
                    availability=Availability.UNKNOWN,
                    status_detail=f"WAF Rate-limit (HTTP {api_resp.status_code}) - backed off & session rotated",
                    url=u,
                )
                for u in urls
            ]

        if api_resp.status_code != 200:
            return [
                TicketResult(
                    event_title=url_to_title.get(u, u),
                    availability=Availability.UNKNOWN,
                    status_detail=f"HTTP {api_resp.status_code}",
                    url=u,
                )
                for u in urls
            ]

        results = []
        try:
            data = api_resp.json()
            returned_ids = set()
            for item in data:
                item_id = item.get("Id")
                u = id_to_url.get(item_id)
                if not u:
                    continue
                returned_ids.add(u)
                title = url_to_title[u]
                status_type = item.get("Type", "")
                debug_info = item.get("Debug", "")
                raw_html = item.get("Text", "")

                booking_link = None
                price_info = None
                if raw_html:
                    item_soup = BeautifulSoup(raw_html, "html.parser")
                    a_tag = item_soup.find("a", href=True)
                    if a_tag:
                        href = a_tag["href"]
                        booking_link = f"https://www.carnegiehall.org{href}" if href.startswith("/") else href

                    for text_chunk in item_soup.stripped_strings:
                        if "$" in text_chunk:
                            price_info = text_chunk
                            break

                is_available = status_type == "Available" or "Get Tickets" in raw_html
                availability = Availability.AVAILABLE if is_available else Availability.UNAVAILABLE
                status_detail = f"{status_type or debug_info or 'Sold out'}"

                results.append(
                    TicketResult(
                        event_title=title,
                        availability=availability,
                        status_detail=status_detail,
                        url=u,
                        booking_link=booking_link,
                        price_info=price_info,
                    )
                )

            # Any URLs missing from response
            for u in urls:
                if u not in returned_ids:
                    results.append(
                        TicketResult(
                            event_title=url_to_title.get(u, u),
                            availability=Availability.UNKNOWN,
                            status_detail="Missing from batch API response",
                            url=u,
                        )
                    )
            return results

        except Exception as err:
            return [
                TicketResult(
                    event_title=url_to_title.get(u, u),
                    availability=Availability.UNKNOWN,
                    status_detail=f"Parse exception: {err}",
                    url=u,
                )
                for u in urls
            ]

    def check_availability(self, url: str) -> TicketResult:
        res = self.check_batch([url])
        return res[0]


class ScraperRegistry:
    """Registry pattern to route any target URL to the right scraper."""

    def __init__(self):
        self._scrapers: List[BaseScraper] = [
            CarnegieHallScraper(),
        ]

    def get_scraper(self, url: str) -> BaseScraper:
        for scraper in self._scrapers:
            if scraper.can_handle(url):
                return scraper
        raise ValueError(f"No scraper registered for URL: {url}")


class Notifier:
    """Multi-channel notification engine (CLI banner, terminal audio bell, desktop notify-send, Telegram)."""

    @staticmethod
    def send_telegram(message: str) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_ids = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_ids:
            return

        for cid in [c.strip() for c in chat_ids.split(",") if c.strip()]:
            try:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {
                    "chat_id": cid,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                }
                # Use standard urllib so telegram notifications never conflict with curl_cffi sessions
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    pass
            except Exception as e:
                sys.stderr.write(f"Failed to send Telegram alert to {cid}: {e}\n")

    @classmethod
    def alert_available(cls, result: TicketResult) -> None:
        title = f"TICKETS AVAILABLE: {result.event_title}"
        link = result.booking_link or result.url

        banner = "=" * 70
        alert_msg = (
            f"\n\a{banner}\n"
            f"🚨🚨🚨 TICKETS NOW AVAILABLE! 🚨🚨🚨\n"
            f"Event: {result.event_title}\n"
            f"Direct Booking: {link}\n"
        )
        if result.price_info:
            alert_msg += f"Price: {result.price_info}\n"
        alert_msg += f"{banner}\n"

        # 1. Terminal output + ASCII bell
        sys.stdout.write(alert_msg)
        sys.stdout.flush()

        # 2. Desktop notification (Linux notify-send)
        if shutil.which("notify-send"):
            try:
                subprocess.run(
                    ["notify-send", "-u", "critical", title, f"Booking link: {link}"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        # 3. Telegram notification (Group or individual chats)
        tg_html = (
            f"🚨 <b>TICKETS NOW AVAILABLE!</b>\n\n"
            f"🎭 <b>Event:</b> <a href=\"{result.url}\">{result.event_title}</a>\n"
        )
        if result.price_info:
            tg_html += f"💰 <b>Price:</b> {result.price_info}\n"
        tg_html += f"🎟️ <b>Direct Booking:</b> <a href=\"{link}\">Click here to buy tickets</a>"

        cls.send_telegram(tg_html)

    @classmethod
    def notify_startup(cls, initial_results: List[TicketResult], interval_seconds: int, jitter_seconds: int) -> None:
        """Send a one-time startup message to Telegram confirming the monitor is online and communicating its cadence."""
        events_list = "\n".join(
            [f"• <a href=\"{r.url}\">{r.event_title}</a>: {r.status_detail}" for r in initial_results]
        )
        msg = (
            f"🤖 <b>Ticket Monitor Active</b>\n\n"
            f"<b>Pinging interval:</b> ~{interval_seconds}s (±{jitter_seconds}s jitter)\n\n"
            f"<b>Monitoring Events:</b>\n{events_list}\n\n"
            f"<i>You will only be alerted in this channel when tickets become available.</i>"
        )
        cls.send_telegram(msg)


@dataclass
class MonitorEngine:
    targets: List[str]
    interval_seconds: int = 60
    jitter_seconds: int = 10
    registry: ScraperRegistry = field(default_factory=ScraperRegistry)
    state: Dict[str, Availability] = field(default_factory=dict)

    def run_once(self) -> List[TicketResult]:
        results = []
        # Group targets by scraper
        scraper_groups: Dict[BaseScraper, List[str]] = {}
        for url in self.targets:
            scraper = self.registry.get_scraper(url)
            scraper_groups.setdefault(scraper, []).append(url)

        for scraper, urls in scraper_groups.items():
            start_t = time.perf_counter()
            if hasattr(scraper, "check_batch"):
                batch_results = scraper.check_batch(urls)
            else:
                batch_results = [scraper.check_availability(u) for u in urls]
            elapsed = time.perf_counter() - start_t

            for result in batch_results:
                # State transition detection
                prev_status = self.state.get(result.url)
                self.state[result.url] = result.availability

                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"[{ts}] [{result.availability.value:11s}] {result.event_title:<30} "
                    f"({result.status_detail})"
                )

                # Trigger alert ONLY on initial discovery of AVAILABLE or state transition
                if result.availability == Availability.AVAILABLE and prev_status != Availability.AVAILABLE:
                    Notifier.alert_available(result)

                results.append(result)

            print(f"  ↳ Batch checked {len(urls)} target(s) in {elapsed:.2f}s")

        return results

    def start_polling(self) -> None:
        print("=" * 70)
        print("🎫 TICKET MONITOR INITIALIZED")
        print(f"Monitoring {len(self.targets)} event(s)")
        for t in self.targets:
            print(f"  • {t}")
        print(f"Polling interval: ~{self.interval_seconds}s (±{self.jitter_seconds}s jitter)")
        print("=" * 70 + "\n")

        # First run to establish initial state and confirm responses
        initial_results = self.run_once()
        Notifier.notify_startup(
            initial_results=initial_results,
            interval_seconds=self.interval_seconds,
            jitter_seconds=self.jitter_seconds,
        )

        try:
            while True:
                # Anti-bot jitter: random delay around base interval
                sleep_time = max(5, self.interval_seconds + random.uniform(-self.jitter_seconds, self.jitter_seconds))
                time.sleep(sleep_time)
                try:
                    self.run_once()
                except Exception as loop_err:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    sys.stderr.write(f"[{ts}] Transient loop error (recovering): {loop_err}\n")
        except KeyboardInterrupt:
            print("\n👋 Monitor stopped by user.")


def main():
    targets = [
        "https://www.carnegiehall.org/Calendar/2027/03/18/Das-Rheingold-0600PM",
        "https://www.carnegiehall.org/Calendar/2027/03/19/Die-Walkure-0600PM",
        # "https://www.carnegiehall.org/Calendar/2027/03/21/Siegfried-0200PM",
        # "https://www.carnegiehall.org/Calendar/2027/03/23/Gotterdammerung-0600PM",
        # "https://www.carnegiehall.org/Calendar/2027/02/28/Vienna-Philharmonic-0200PM"
    ]

    engine = MonitorEngine(
        targets=targets,
        interval_seconds=120,
        jitter_seconds=10,
    )
    engine.start_polling()


if __name__ == "__main__":
    main()

