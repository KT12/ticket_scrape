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

import atexit
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from bs4 import BeautifulSoup
from curl_cffi import requests


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TicketResult:
    event_title: str
    availability: Availability
    status_detail: str
    url: str
    booking_link: str | None = None
    price_info: str | None = None


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
        self._meta_cache: dict[str, tuple[str, str, float]] = {}

    def _reset_session(self) -> None:
        """Recycle session to clear any flagged cookies."""
        self.session = requests.Session(impersonate=self.impersonate)

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "carnegiehall.org" in url.lower()

    def _ensure_metadata(self, url: str) -> tuple[str, str | None]:
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

    def check_batch(self, urls: list[str]) -> list[TicketResult]:
        """
        Check all Carnegie Hall URLs in a SINGLE HTTP request!
        This is the most anti-bot resilient approach possible.
        """
        url_to_id: dict[str, str] = {}
        url_to_title: dict[str, str] = {}
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
        self._scrapers: list[BaseScraper] = [
            CarnegieHallScraper(),
        ]

    def get_scraper(self, url: str) -> BaseScraper:
        for scraper in self._scrapers:
            if scraper.can_handle(url):
                return scraper
        raise ValueError(f"No scraper registered for URL: {url}")


class Notifier:
    """Multi-channel notification engine (CLI banner, terminal audio bell, desktop notify-send, Telegram)."""

    _original_bot_name: str | None = None

    @staticmethod
    def _call_telegram_api(method: str, payload: dict) -> dict | None:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            return None
        url = f"https://api.telegram.org/bot{token}/{method}"
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as e:
            sys.stderr.write(f"Telegram API {method} error: {e}\n")
            return None

    @classmethod
    def send_telegram(cls, message: str) -> None:
        chat_ids = os.getenv("TELEGRAM_CHAT_ID")
        if not chat_ids:
            return

        for cid in [c.strip() for c in chat_ids.split(",") if c.strip()]:
            cls._call_telegram_api(
                "sendMessage",
                {
                    "chat_id": cid,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )

    @classmethod
    def update_bot_status(cls, is_active: bool, status_note: str = "", count: int = 0) -> None:
        """
        Updates the Telegram bot's profile name and description so users can see
        real-time status (active/stopped/errors) without chat message spam.
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            return

        # Fetch base name if not cached
        if cls._original_bot_name is None:
            me_resp = cls._call_telegram_api("getMyName", {})
            if me_resp and me_resp.get("ok"):
                raw_name = me_resp.get("result", {}).get("name", "")
                # Strip out any previous status indicators
                clean_name = re.sub(r"\s*\[.*\]\s*$", "", raw_name).strip()
                cls._original_bot_name = clean_name or "Ticket Monitor"
            else:
                cls._original_bot_name = "Ticket Monitor"

        base_name = cls._original_bot_name

        if is_active:
            badge = " [🟢 Active]"
            allowed_base = 64 - len(badge)
            trimmed_base = base_name[:allowed_base] if len(base_name) + len(badge) > 64 else base_name
            new_name = f"{trimmed_base}{badge}"
            short_desc = f"🟢 Active | Tracking {count} event(s)\nLast checked: {status_note}"
        else:
            badge = " [🔴 Stopped]"
            allowed_base = 64 - len(badge)
            trimmed_base = base_name[:allowed_base] if len(base_name) + len(badge) > 64 else base_name
            new_name = f"{trimmed_base}{badge}"
            short_desc = f"🔴 Offline: {status_note}" if status_note else "🔴 Monitor is currently offline."

        # Keep short_description under 120 chars
        short_desc = short_desc[:120]

        cls._call_telegram_api("setMyName", {"name": new_name})
        cls._call_telegram_api("setMyShortDescription", {"short_description": short_desc})

    @classmethod
    def alert_available(cls, result: TicketResult) -> None:
        title = f"TICKETS AVAILABLE: {result.event_title}"
        link = result.booking_link or result.url

        banner = "=" * 70
        alert_msg = (
            f"\n\a{banner}\n🚨🚨🚨 TICKETS NOW AVAILABLE! 🚨🚨🚨\nEvent: {result.event_title}\nDirect Booking: {link}\n"
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
            f'🚨 <b>TICKETS NOW AVAILABLE!</b>\n\n🎭 <b>Event:</b> <a href="{result.url}">{result.event_title}</a>\n'
        )
        if result.price_info:
            tg_html += f"💰 <b>Price:</b> {result.price_info}\n"
        tg_html += f'🎟️ <b>Direct Booking:</b> <a href="{link}">Click here to buy tickets</a>'

        cls.send_telegram(tg_html)


@dataclass
class MonitorEngine:
    targets: list[str]
    interval_seconds: int = 60
    jitter_seconds: int = 10
    max_consecutive_errors: int = 5
    registry: ScraperRegistry = field(default_factory=ScraperRegistry)
    state: dict[str, Availability] = field(default_factory=dict)
    consecutive_errors: int = 0

    def run_once(self) -> list[TicketResult]:
        results = []
        cycle_has_errors = False
        error_reasons = []

        # Group targets by scraper
        scraper_groups: dict[BaseScraper, list[str]] = {}
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
                # Check for HTTP 4xx / 5xx or rate limit errors
                detail = result.status_detail or ""
                has_http_err = "HTTP 4" in detail or "HTTP 5" in detail
                if has_http_err or "WAF Rate-limit" in detail or "Network timeout" in detail:
                    cycle_has_errors = True
                    error_reasons.append(detail)

                # State transition detection
                prev_status = self.state.get(result.url)
                self.state[result.url] = result.availability

                ts = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{ts}] [{result.availability.value:11s}] {result.event_title:<30} ({result.status_detail})")

                # Trigger alert ONLY on initial discovery of AVAILABLE or state transition
                if result.availability == Availability.AVAILABLE and prev_status != Availability.AVAILABLE:
                    Notifier.alert_available(result)

                results.append(result)

            print(f"  ↳ Batch checked {len(urls)} target(s) in {elapsed:.2f}s")

        # Circuit breaker error counter management
        if cycle_has_errors:
            self.consecutive_errors += 1
            reason_str = error_reasons[0] if error_reasons else "Errors detected"
            warn_msg = (
                f"  ⚠️ Warning: Error cycle encountered "
                f"({self.consecutive_errors}/{self.max_consecutive_errors}): {reason_str}"
            )
            print(warn_msg)
        else:
            if self.consecutive_errors > 0:
                print("  ✅ Recovered from consecutive errors after successful check.")
            self.consecutive_errors = 0

        return results

    def start_polling(self) -> None:
        print("=" * 70)
        print("🎫 TICKET MONITOR INITIALIZED")
        print(f"Monitoring {len(self.targets)} event(s)")
        for t in self.targets:
            print(f"  • {t}")
        print(f"Polling interval: ~{self.interval_seconds}s (±{self.jitter_seconds}s jitter)")
        print(f"Circuit breaker limit: {self.max_consecutive_errors} consecutive failures")
        print("=" * 70 + "\n")

        shutdown_reason = "Manual stop"

        # Register exit hook to ensure offline status is reflected on any shutdown
        def _cleanup():
            try:
                Notifier.update_bot_status(is_active=False, status_note=shutdown_reason)
            except Exception:
                pass

        atexit.register(_cleanup)

        # Handle SIGTERM gracefully
        def _sigterm_handler(signum, frame):
            nonlocal shutdown_reason
            shutdown_reason = "SIGTERM received"
            sys.exit(0)

        try:
            signal.signal(signal.SIGTERM, _sigterm_handler)
        except (ValueError, AttributeError):
            pass

        try:
            while True:
                self.run_once()
                now_str = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")

                # Check circuit breaker
                if self.consecutive_errors >= self.max_consecutive_errors:
                    shutdown_reason = f"Circuit breaker tripped ({self.consecutive_errors} consecutive errors)"
                    print(f"\n🚨 CRITICAL: {shutdown_reason}! Stopping scraper to prevent ban.")
                    break

                # Update bot profile status with latest timestamp
                Notifier.update_bot_status(
                    is_active=True,
                    status_note=now_str,
                    count=len(self.targets),
                )

                # Anti-bot jitter: random delay around base interval
                sleep_time = max(5, self.interval_seconds + random.uniform(-self.jitter_seconds, self.jitter_seconds))
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            shutdown_reason = "Stopped by user (Ctrl+C)"
            print("\n👋 Monitor stopped by user.")
        except Exception as e:
            shutdown_reason = f"Crashed: {e}"
            print(f"\n💥 Fatal error: {e}")
            raise
        finally:
            print(f"Updating bot status to offline ({shutdown_reason})...")
            Notifier.update_bot_status(is_active=False, status_note=shutdown_reason)


def main():
    targets = [
        "https://www.carnegiehall.org/Calendar/2027/03/18/Das-Rheingold-0600PM",
        "https://www.carnegiehall.org/Calendar/2027/03/19/Die-Walkure-0600PM",
        "https://www.carnegiehall.org/Calendar/2027/03/21/Siegfried-0200PM",
        "https://www.carnegiehall.org/Calendar/2027/03/23/Gotterdammerung-0600PM",
        # "https://www.carnegiehall.org/Calendar/2027/02/28/Vienna-Philharmonic-0200PM"
    ]

    engine = MonitorEngine(
        targets=targets,
        interval_seconds=150,
        jitter_seconds=15,
        max_consecutive_errors=5,
    )
    engine.start_polling()


if __name__ == "__main__":
    main()
