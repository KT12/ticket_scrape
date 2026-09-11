# Ticket Scraper & Availability Monitor

A lightweight, anti-bot resilient ticket availability monitor built to track sold-out concerts and notify instantly upon ticket release.

## Features
- **Direct API Wire Protocol**: Bypasses slow/brittle headless browsers by reverse-engineering Carnegie Hall's internal Sitecore BuyButton endpoint.
- **Anti-Bot WAF Bypass**: Uses `curl_cffi` Chrome TLS impersonation to bypass DataDome and Imperva Incapsula blocks.
- **Multi-Event Monitoring**: Pre-configured to monitor Wagner's *Das Rheingold* and *Die Walküre*.
- **Extensible Architecture**: Uses the Strategy and Registry design patterns (`BaseScraper`, `CarnegieHallScraper`, `ScraperRegistry`) to support additional venues/ticketing providers easily.
- **Smart Notification & Deduplication**: Emits visual CLI banners, ASCII terminal bells (`\a`), and desktop notifications (`notify-send`) on state transitions (`UNAVAILABLE` -> `AVAILABLE`) without spamming.
- **Anti-Profiling Jitter**: Includes randomized sleep intervals to prevent fingerprinting.

## Quickstart

Run with `uv`:

```bash
# Optional: Set Telegram alerts (works with both personal chat IDs and group chat IDs)
export TELEGRAM_BOT_TOKEN="123456789:ABCdef..."
export TELEGRAM_CHAT_ID="-1001234567890"  # Or comma-separated: "12345,67890"

# Run the monitor loop
uv run python main.py
```

To run as a background service or in tmux:
```bash
tmux new -s tickets "uv run python main.py"
```


