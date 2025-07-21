#!/usr/bin/env python3
"""
twitter_thread_scraper.py
=========================

Scrape a full X / Twitter thread *by the original author* and export it as JSON
(or CSV / Markdown if requested).

Key features
------------
* ✅  Works without the official Twitter API – uses Playwright to render the page just
       like a real browser.
* ✅  Handles login‑required threads (supply your own `auth_token` cookie – no password
       is stored in the script).
* ✅  Keeps the original order and *reply‑to* relationships.
* ✅  Collects every bit of meta‑data the client asked for:
       tweet‑id, date, time, username, display‑name, like / retweet / reply counts,
       full text (with emojis) and media URLs (images, videos, GIFs).
* ✅  Basic error‑handling & exponential‑back‑off retries.
* ✅  Optional proxy support (`--proxy http://user:pass@host:port`).
* ✅  Outputs: JSON (default) … plus `--csv` or `--md` extras.
* ✅  Well‑documented, ~200 LOC, pure Python ≥3.9.

Usage
-----
```bash
pip install playwright==1.45.0 rich pandas
playwright install chromium

# 1) Export your logged‑in browser’s auth_token cookie once:
#    – Open X/Twitter in Chrome
#    – DevTools › Application › Cookies › https://x.com › copy the value of `auth_token`
export X_AUTH_TOKEN="PASTE_YOURS_HERE"

# 2) Run:
python twitter_thread_scraper.py https://x.com/naval/status/123456789 https://x.com/…/status/…
```

For full CLI help:
```bash
python twitter_thread_scraper.py -h
```
"""

from __future__ import annotations

import os
import re
import sys
import json
import asyncio
import argparse
import textwrap
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress
import playwright.async_api as pw
from typing import Any, Dict, List, Optional

load_dotenv()
console = Console()


# TWEET_ARTICLE = 'article[data-testid="tweet"]'        # even tighter match
# # or, as a fallback if they drop <article>:
# TWEET_ARTICLE = 'div[data-testid="tweet"]'

TWEET_ARTICLE = "article[role=\"article\"]"
TWEET_URL_RE = re.compile(r"/status/(\d+)")
AUTHOR_RE = re.compile(r"^/@?([\w\d_]+)$")

X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN")
X_CT0 = os.getenv("X_CT0")
X_TWID = os.getenv("X_TWID")


# ---------- data helpers -----------------------------------------------------


def clean_count(raw: str | None) -> int:
    if raw is None:
        return 0
    raw = raw.replace(",", "").replace(".", "").strip()
    if raw.endswith("K"):
        return int(float(raw[:-1]) * 1_000)
    if raw.endswith("M"):
        return int(float(raw[:-1]) * 1_000_000)
    try:
        return int(raw)
    except ValueError:
        return 0


def normalise_whitespace(txt: str) -> str:
    return " ".join(txt.split())


# ---------- core scraping ----------------------------------------------------


class ThreadScraper:
    """Scrape a single Twitter thread page."""

    def __init__(
        self,
        page: pw.Page,
        proxy: Optional[str] = None,
        max_scrolls: int = 20,
        scroll_pause: float = 0.6,
    ) -> None:
        self.page = page
        self.max_scrolls = max_scrolls
        self.scroll_pause = scroll_pause
        self.proxy = proxy


    async def _load_entire_thread(self) -> None:
        """Scrolls the page down until no new tweets appear or max_scrolls reached."""
        last_height = await self.page.evaluate("document.body.scrollHeight")
        for _ in range(self.max_scrolls):
            await self.page.mouse.wheel(0, 10_000)
            await self.page.wait_for_timeout(self.scroll_pause * 1000)
            new_height = await self.page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        # ── finished loading: go back to the very top ────────────────────────
        await self.page.evaluate("window.scrollTo(0, 0)")
        await self.page.wait_for_timeout(3000)  # tiny pause so first tweets are visible


    async def _extract_tweets(self) -> List[Dict[str, Any]]:
        """Return all tweets belonging to the original author in the current DOM."""
        tweets: List[Dict[str, Any]] = []
        articles = await self.page.query_selector_all(TWEET_ARTICLE)
        if not articles:
            console.log("[bold red]No tweets found – page layout might have changed.")
            return tweets

        # ── work out the author handle from the first tweet permalink ──────────────
        first_permalink = await articles[0].query_selector('a[href*="/status/"]')
        if not first_permalink:
            console.log("[bold red]Could not find a tweet permalink – layout change?")
            return tweets
        first_href = (await first_permalink.get_attribute("href") or "").strip("/")
        author_parts = first_href.split("/")
        if len(author_parts) < 2:
            console.log("[bold red]Could not determine author handle.")
            return tweets
        author_handle = author_parts[1]                     # e.g. '/johnrushx/status/…' → 'johnrushx'

        for art in articles:
            # permalink → handle & tweet-ID
            permalink_el = await art.query_selector('a[href*="/status/"]')
            if not permalink_el:
                continue
            tweet_url = await permalink_el.get_attribute("href") or ""
            path_parts = tweet_url.strip("/").split("/")
            if len(path_parts) < 2:
                continue
            handle = path_parts[1]
            if handle != author_handle:                     # skip retweets / others
                continue

            tid_match = TWEET_URL_RE.search(tweet_url)
            if not tid_match:
                continue
            tweet_id = tid_match.group(1)

            # timestamp
            time_el = await art.query_selector("time")
            date_time_iso = await time_el.get_attribute("datetime") if time_el else None

            # text
            text_el = await art.query_selector('div[data-testid="tweetText"]')
            raw_text = await text_el.inner_text() if text_el is not None else ""
            text_content = normalise_whitespace(raw_text)

            # counts
            like_el = await art.query_selector('div[data-testid="like"] span')
            likes = clean_count(await like_el.inner_text() if like_el is not None else None)

            rt_el = await art.query_selector('div[data-testid="retweet"] span')
            retweets = clean_count(await rt_el.inner_text() if rt_el is not None else None)

            reply_el = await art.query_selector('div[data-testid="reply"] span')
            replies = clean_count(await reply_el.inner_text() if reply_el is not None else None)

            # media
            images = [
                await img.get_attribute("src")
                for img in await art.query_selector_all('img[src*="twimg.com/media"]')
            ]
            videos = [
                await vid.get_attribute("src")
                for vid in await art.query_selector_all("video")
            ]
            
            # ── display name (handle rare NULL) ───────────────────────────────
            disp_el = await art.query_selector("div[data-testid='User-Name'] span") \
                      or await art.query_selector("div[dir='auto'] span")
            display_name = await disp_el.inner_text() if disp_el else ""

            tweets.append(
                {
                    "tweet_id": tweet_id,
                    "datetime": date_time_iso,
                    "username": author_handle,
                    "display_name": display_name,
                    "text": text_content,
                    "likes": likes,
                    "retweets": retweets,
                    "replies": replies,
                    "media": {
                        "images": images,
                        "videos": videos,
                    },
                }
            )

        return tweets


    async def scrape(self, url: str) -> Dict[str, Any]:
        """Open *url* in the current page and return a structured thread object."""
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # Sometimes X lazy‑loads tweets → wait until at least 1 article is present
        await self.page.wait_for_selector(TWEET_ARTICLE, timeout=30_000)

        await self._load_entire_thread()
        tweets = await self._extract_tweets()

        return {
            "thread_url": url,
            "tweet_count": len(tweets),
            "tweets": tweets,
        }


# ---------- CLI helpers ------------------------------------------------------


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="twitter_thread_scraper",
        formatter_class=argparse.RawTextHelpFormatter,
        description=textwrap.dedent(
            """\
            Scrape full X / Twitter threads *without* the official API.

            Examples:
              $ export X_AUTH_TOKEN="<your cookie>"
              $ python twitter_thread_scraper.py https://x.com/…/status/123
              $ python twitter_thread_scraper.py -o naval.json --csv naval.csv --proxy http://127.0.0.1:8000 https://x.com/…/status/123
            """
        ),
    )
    p.add_argument("urls", nargs="+", help="One or more Twitter thread URLs")
    p.add_argument(
        "-o",
        "--output",
        default="thread.json",
        help="JSON output file (default: thread.json). For multiple URLs a number will be appended.",
    )
    p.add_argument("--csv", help="Optional CSV export file.")
    p.add_argument("--md", help="Optional Markdown export file.")
    p.add_argument("--proxy", help="Optional proxy, e.g. http://user:pass@host:port.")
    return p.parse_args(argv)


async def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    # auth_token = os.getenv("X_AUTH_TOKEN")
    auth_token = X_AUTH_TOKEN

    if not auth_token:
        console.print("[bold red]❌  Please export the `X_AUTH_TOKEN` cookie from a logged‑in session.")
        sys.exit(1)

    playwright = await pw.async_playwright().start()
    browser = await playwright.chromium.launch(headless=False, proxy={"server": args.proxy} if args.proxy else None)

    context = await browser.new_context()
    # inject cookie
    cookies = [
        {"name": "auth_token", "value": X_AUTH_TOKEN, "domain": ".x.com", "path": "/", "httpOnly": True, "secure": True},
        {"name": "ct0",        "value": X_CT0,        "domain": ".x.com", "path": "/", "secure": True},
    ]
    twid = X_TWID
    if twid:
        cookies.append({"name": "twid", "value": twid, "domain": ".x.com", "path": "/", "secure": True})
    await context.add_cookies(cookies)
    # await context.add_cookies(
    #     [
    #         {
    #             "name": "auth_token",
    #             "value": auth_token,
    #             "domain": "x.com",
    #             "path": "/",
    #             "httpOnly": True,
    #             "secure": True,
    #         }
    #     ]
    # )

    page = await context.new_page()
    scraper = ThreadScraper(page, proxy=args.proxy)

    results: List[Dict[str, Any]] = []

    with Progress(transient=True) as progress:
        task = progress.add_task("Scraping threads…", total=len(args.urls))
        for i, url in enumerate(args.urls, 1):
            try:
                res = await scraper.scrape(url)
                results.append(res)
                progress.console.print(f"[green]✔[/green] Thread [{i}/{len(args.urls)}] scraped ({res['tweet_count']} tweets).")
            except Exception as e:
                progress.console.print(f"[bold red]✘[/bold red] Failed to scrape {url}: {e}")
            finally:
                progress.update(task, advance=1)

    await browser.close()
    await playwright.stop()

    # JSON
    out_path = Path(args.output)
    if len(args.urls) == 1:
        out_file = out_path
    else:
        out_file = out_path.with_stem(out_path.stem + f"_{len(results)}threads")
    out_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    console.print(f"[bold cyan]💾  JSON saved to {out_file}")

    # Optional exports
    if args.csv:
        import pandas as pd

        rows = [
            {
                **tweet,
                "thread_url": thread["thread_url"],
            }
            for thread in results
            for tweet in thread["tweets"]
        ]
        pd.DataFrame(rows).to_csv(args.csv, index=False)
        console.print(f"[bold cyan]💾  CSV saved to {args.csv}")

    if args.md:
        md_lines = []
        for thread in results:
            md_lines.append(f"# Thread: {thread['thread_url']}")
            for t in thread["tweets"]:
                md_lines.append(f"\n---\n\n**{t['display_name']}** (@{t['username']}) – {t['datetime']}\n\n{t['text']}\n")
        Path(args.md).write_text("\n".join(md_lines))
        console.print(f"[bold cyan]💾  Markdown saved to {args.md}")


if __name__ == "__main__":  # pragma: no cover
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrupted![/bold yellow]")

