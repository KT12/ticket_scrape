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
import random
import re
import shutil
import subprocess
import sys
import time
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
    1. Fetches concert page using curl_cffi Chrome TLS impersonation (bypasses DataDome/Imperva).
    2. Extracts event title and Sitecore item GUID (data-id) from the hero buy container.
    3. Calls /api/sitecore/BuyButton/events directly to fetch verified ticket state.
    """

    BUY_BUTTON_API = "https://www.carnegiehall.org/api/sitecore/BuyButton/events"

    def __init__(self, impersonate: str = "chrome"):
        self.impersonate = impersonate
        self.session = requests.Session(impersonate=self.impersonate)

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "carnegiehall.org" in url.lower()

    def check_availability(self, url: str) -> TicketResult:
        # Step 1: Request event landing page
        resp = self.session.get(url, timeout=15)
        if resp.status_code != 200:
            return TicketResult(
                event_title=url.rstrip("/").split("/")[-1],
                availability=Availability.UNKNOWN,
                status_detail=f"HTTP {resp.status_code} fetching event page",
                url=url,
            )

        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract event title (from og:title or title tag)
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            event_title = og_title["content"].strip()
        elif soup.title and soup.title.string:
            event_title = soup.title.string.split("|")[0].strip()
        else:
            event_title = url.rstrip("/").split("/")[-1]

        # Step 2: Extract Sitecore GUID (data-id)
        buy_container = soup.find(class_=lambda c: c and "js-event-buy" in c)
        data_id = None
        if buy_container:
            data_id = buy_container.get("data-id")

        if not data_id:
            # Fallback regex for GUID
            guid_match = re.search(r'data-id="(\{[A-Fa-f0-9-]+\})"', resp.text)
            if guid_match:
                data_id = guid_match.group(1)

        if not data_id:
            return TicketResult(
                event_title=event_title,
                availability=Availability.UNKNOWN,
                status_detail="Could not locate event ID in page HTML",
                url=url,
            )

        # Step 3: Query internal BuyButton API
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": url,
            "Content-Type": "application/json",
        }
        payload = [{"Id": data_id, "LoadCyo": True}]

        api_resp = self.session.post(self.BUY_BUTTON_API, json=payload, headers=headers, timeout=15)
        if api_resp.status_code != 200:
            return TicketResult(
                event_title=event_title,
                availability=Availability.UNKNOWN,
                status_detail=f"API error: HTTP {api_resp.status_code}",
                url=url,
            )

        try:
            data = api_resp.json()
            if not data or not isinstance(data, list):
                return TicketResult(
                    event_title=event_title,
                    availability=Availability.UNKNOWN,
                    status_detail="Empty API response",
                    url=url,
                )

            item = data[0]
            status_type = item.get("Type", "")
            debug_info = item.get("Debug", "")
            raw_html = item.get("Text", "")

            # Parse booking link if available
            booking_link = None
            price_info = None
            if raw_html:
                item_soup = BeautifulSoup(raw_html, "html.parser")
                a_tag = item_soup.find("a", href=True)
                if a_tag:
                    href = a_tag["href"]
                    booking_link = f"https://www.carnegiehall.org{href}" if href.startswith("/") else href

                # Extract price snippet if present
                for text_chunk in item_soup.stripped_strings:
                    if "$" in text_chunk:
                        price_info = text_chunk
                        break

            # Check if tickets are available
            is_available = status_type == "Available" or "Get Tickets" in raw_html

            availability = Availability.AVAILABLE if is_available else Availability.UNAVAILABLE
            status_detail = f"{status_type or debug_info or 'Sold out'}"

            return TicketResult(
                event_title=event_title,
                availability=availability,
                status_detail=status_detail,
                url=url,
                booking_link=booking_link,
                price_info=price_info,
            )

        except Exception as err:
            return TicketResult(
                event_title=event_title,
                availability=Availability.UNKNOWN,
                status_detail=f"Parse exception: {err}",
                url=url,
            )


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
    """Multi-channel notification engine (CLI banner, terminal audio bell, desktop notify-send)."""

    @staticmethod
    def alert_available(result: TicketResult) -> None:
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

        # Terminal output + ASCII bell
        sys.stdout.write(alert_msg)
        sys.stdout.flush()

        # Desktop notification (Linux notify-send)
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


@dataclass
class MonitorEngine:
    targets: List[str]
    interval_seconds: int = 60
    jitter_seconds: int = 10
    registry: ScraperRegistry = field(default_factory=ScraperRegistry)
    state: Dict[str, Availability] = field(default_factory=dict)

    def run_once(self) -> List[TicketResult]:
        results = []
        for url in self.targets:
            scraper = self.registry.get_scraper(url)
            start_t = time.perf_counter()
            result = scraper.check_availability(url)
            elapsed = time.perf_counter() - start_t

            # State transition detection
            prev_status = self.state.get(url)
            self.state[url] = result.availability

            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            color = "\033[92m" if result.availability == Availability.AVAILABLE else "\033[90m"
            reset = "\033[0m"

            print(
                f"[{ts}] [{result.availability.value:11s}] {result.event_title:<30} "
                f"({result.status_detail}) in {elapsed:.2f}s"
            )

            # Trigger alert ONLY on initial discovery of AVAILABLE or state transition
            if result.availability == Availability.AVAILABLE and prev_status != Availability.AVAILABLE:
                Notifier.alert_available(result)

            results.append(result)
        return results

    def start_polling(self) -> None:
        print("=" * 70)
        print("🎫 TICKET MONITOR INITIALIZED")
        print(f"Monitoring {len(self.targets)} event(s)")
        for t in self.targets:
            print(f"  • {t}")
        print(f"Polling interval: ~{self.interval_seconds}s (±{self.jitter_seconds}s jitter)")
        print("=" * 70 + "\n")

        try:
            while True:
                self.run_once()
                # Anti-bot jitter: random delay around base interval
                sleep_time = max(5, self.interval_seconds + random.uniform(-self.jitter_seconds, self.jitter_seconds))
                time.sleep(sleep_time)
        except KeyboardInterrupt:
            print("\n👋 Monitor stopped by user.")


def main():
    targets = [
        "https://www.carnegiehall.org/Calendar/2027/03/18/Das-Rheingold-0600PM",
        "https://www.carnegiehall.org/Calendar/2027/03/19/Die-Walkure-0600PM",
    ]

    engine = MonitorEngine(
        targets=targets,
        interval_seconds=60,
        jitter_seconds=10,
    )
    engine.start_polling()


if __name__ == "__main__":
    main()

