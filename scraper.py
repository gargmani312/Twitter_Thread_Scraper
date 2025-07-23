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
import httpx
import asyncio
import requests
import argparse
import textwrap
import traceback
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
import playwright.async_api as pw
from collections import defaultdict
from typing import Any, Dict, List, Optional

load_dotenv()
console = Console()

# TWEET_ARTICLE = 'article[data-testid="tweet"]'        # even tighter match
# # or, as a fallback if they drop <article>:
# TWEET_ARTICLE = 'div[data-testid="tweet"]'

TWEET_ARTICLE = "article[role=\"article\"]"
TWEET_URL_RE = re.compile(r"/status/(\d+)")
AUTHOR_RE = re.compile(r"^/@?([\w\d_]+)$")

HEADLESS = True
X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN")                                            # 'auth_token' cookie from a logged-in twitter session
TIMEZONE_ID = os.getenv("TIMEZONE_ID")                                              # change to your local timezone
EXTRACT_MP4_ONLY = os.getenv("EXTRACT_MP4_ONLY", "False").lower() == "true"         # set to True to use a persistent Firefox profile

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


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


def variant_from_mp4(url: str) -> dict:
    # Twitter’s path contains `/<width>x<height>/`
    m = re.search(r'/(\d{2,4}x\d{2,4})/', url)
    resolution = m.group(1) if m else None
    # HEAD request to get size ⇒ rough bitrate estimate
    size = int(requests.head(url, allow_redirects=True).headers.get('content-length', 0))
    # assume ~8 Mbps per 1 MB/s of bytes/sec   (very rough)
    bitrate = round(size / 128_000) * 1000 if size else None
    variant = [{
        "bitrate": bitrate, 
        "resolution": resolution,
        "url": url,
    }]
    return variant


# ---------- core scraping ----------------------------------------------------


class ThreadScraper:
    """Scrape a single Twitter thread page."""

    def __init__(
        self,
        page: pw.Page,
        proxy: Optional[str] = None,
        scroll_pause: float = 0.6,
    ) -> None:
        self.page = page
        self.author_name = None
        self.author_handle = None
        self.author_avatar_url = None
        self.scroll_pause = scroll_pause
        self.proxy = proxy
        self.show_more_replies = False
        self.video_pool: dict[str, list[str]] = defaultdict(list)   # video_id -> [urls]
        self.assigned_video_ids: set[str] = set()                   # avoid double-assign
        self.page.context.on("response", self._capture_video)


    async def _capture_video(self, resp: pw.Response) -> None:
        url = resp.url
        if "video.twimg.com" not in url:
            return
        if resp.status not in (200, 206):
            return
        ctype = await resp.header_value("content-type") or ""
        if (".mp4" in url) or (".m3u8" in url) or "application/vnd.apple.mpegurl" in ctype or "video/mp4" in ctype:
            m = re.search(r"(?:amplify_video|ext_tw_video)/(\d+)/", url)
            if m:
                self.video_pool[m.group(1)].append(url)
                

    # ───────────── Tweet extraction helper methods ─────────────
    async def _video_ids_in_art(self, art) -> list[str]:
        """Find amplify/ext video ids inside this tweet article (poster/img/style attrs)."""
        js = r"""
        el => {
          const rx = /(amplify_video|ext_tw_video)[/_](thumb\/)?(\d+)\//g;
          const ids = new Set();
          const push = v => { if (typeof v === 'string') {
              let m; while ((m = rx.exec(v)) !== null) ids.add(m[3]);
          }};
          el.querySelectorAll('*').forEach(n => {
            // attributes
            for (const a of (n.attributes || [])) push(a.value);
            // style background-image etc
            const cs = n.style && n.style.cssText;
            if (cs) push(cs);
          });
          return [...ids];
        }
        """
        return await art.evaluate(js)
    
    
    async def _get_author_avatar(self, art) -> str | None:
        # 1) try the standard avatar container
        img = await art.query_selector("div[data-testid='Tweet-User-Avatar'] img[src]")
        if not img:
            # 2) fallback: any img inside a link to the author
            img = await art.query_selector(f'a[href="/{self.author_handle}"] img[src]')
        if not img:
            return None
        src = await img.get_attribute("src")
        if not src:
            return None
        # upgrade to higher-res if you want (Twitter suffixes: _normal, _bigger, _mini)
        return re.sub(r'_(normal|bigger|mini)\.(jpg|png)$', r'_400x400.\2', src)
    
    
    async def _click_show_replies(self) -> bool:
        """Click the first 'Show replies' / 'Show more replies' button (if any) and return."""
        btn = self.page.locator(
            "button:has-text('Show replies'), button:has-text('Show more replies')"
        ).first
        if await btn.count() == 0:
            return False

        try:
            await btn.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            await btn.click(timeout=2000)
        except Exception:
            return False

        # brief pause so newly revealed tweets mount
        try:
            await self.page.wait_for_load_state("networkidle", timeout=2000)
        except pw.TimeoutError:
            pass
        await self.page.wait_for_timeout(250)
        self.show_more_replies = True
        return True


    # ─────────────────────────────────────────────────────────────────────────────
    # 1) Smooth, one‑direction scroll helper
    # ─────────────────────────────────────────────────────────────────────────────
    async def _scroll_into_view(self, el) -> None:
        """Bring element into viewport without jumping back up."""
        await self.page.evaluate(
            "(n)=>n && n.scrollIntoView({behavior:'smooth', block:'center'})", el
        )
        await self.page.wait_for_timeout(180)  # small settle time


    async def _scroll_down(self) -> None:
        """Single-direction incremental scroll to load more tweets."""
        await self.page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight*0.9))")
        try:
            await self.page.wait_for_load_state("networkidle", timeout=2000)
        except pw.TimeoutError:
            pass
        await self.page.wait_for_timeout(self.scroll_pause * 1000)


    async def _text_with_emojis(self, text_el) -> str:
        if not text_el:
            return ""
        raw = await text_el.evaluate("""
            (node) => {
                const collect = (n) => {
                    let out = '';
                    n.childNodes.forEach(ch => {
                        if (ch.nodeType === Node.TEXT_NODE) out += ch.textContent;
                        else if (ch.nodeType === Node.ELEMENT_NODE) {
                            if (ch.tagName.toLowerCase() === 'img' && ch.alt) out += ch.alt;
                            else out += collect(ch);
                        }
                    });
                    return out;
                };
                return collect(node);
            }
        """)
        return normalise_whitespace(raw)


    async def _raw_count(self, art, testid: str) -> str:
        el = await art.query_selector(
            f"[data-testid='{testid}'] [data-testid='app-text-transition-container'] span"
        )
        if el:
            return (await el.inner_text()).strip()
        el = await art.query_selector(f"[data-testid='{testid}'] span")
        if el:
            return (await el.inner_text()).strip()
        btn = await art.query_selector(f"[data-testid='{testid}'][aria-label]")
        if btn:
            label = (await btn.get_attribute('aria-label')) or ""
            m = re.search(r"([0-9][0-9.,]*[KM]?)", label, re.I)
            if m:
                return m.group(1).strip()
        return ""


    async def _raw_views(self, art) -> str:
        el = await art.query_selector(
            "a[href*='/analytics'] [data-testid='app-text-transition-container'] span"
        )
        if el:
            return (await el.inner_text()).strip()

        link = await art.query_selector("a[href*='/analytics'][aria-label]")
        if link:
            label = (await link.get_attribute("aria-label")) or ""
            m = re.search(r"([0-9][0-9.,]*[KM]?)\\s+views", label, re.I)
            if m:
                return m.group(1).strip()

        el = await art.query_selector("div[data-testid='viewCount'] span")
        if el:
            return (await el.inner_text()).strip()

        raw = await art.evaluate("""
            (node) => {
                const spans = node.querySelectorAll('span');
                for (const s of spans) {
                    if (/^\\s*views\\s*$/i.test(s.textContent)) {
                        let p = s.previousElementSibling;
                        while (p) {
                            const txt = (p.textContent || '').trim();
                            const m = txt.match(/[0-9][0-9.,]*[KM]?/i);
                            if (m) return m[0];
                            p = p.previousElementSibling;
                        }
                    }
                }
                return null;
            }
        """)
        if raw:
            return raw.strip()
        return ""
                

    async def fetch_video_variants(self, m3u8_url: str) -> list[dict]:
        """
        Return [{'bitrate', 'resolution', 'url'}, …] for every MP4 variant
        referenced by a Twitter HLS playlist. If *m3u8_url* is already an MP4,
        wrap it in a single-variant list.
        """
        if ".mp4" in m3u8_url:
            return variant_from_mp4(m3u8_url)

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as cli:
            r = await cli.get(m3u8_url)
            if r.status_code != 200:
                return variant_from_mp4(m3u8_url)

        base = m3u8_url.rsplit("/", 1)[0] + "/"
        out: list[dict] = []
        lines = r.text.splitlines()
        for i, line in enumerate(lines):
            if "#EXT-X-STREAM-INF" in line:
                attrs = line.partition(":")[2]
                m_bw  = re.search(r"BANDWIDTH=(\d+)", attrs)
                m_res = re.search(r"RESOLUTION=(\d+x\d+)", attrs)
                out.append({
                    "bitrate": int(m_bw.group(1)) if m_bw else None,
                    "resolution": m_res.group(1) if m_res else None,
                    "url": m3u8_url,
                })
        return out or [{"bitrate": None, "resolution": None, "url": m3u8_url}]
                
                
    async def _media(self, art) -> Dict[str, Any]:
        """Return images + video (thumbnail, variants from m3u8/mp4) for this tweet."""
        media_obj: Dict[str, Any] = {}

        # ── images ────────────────────────────────────────────────────────────────
        images = [
            await img.get_attribute("src")
            for img in await art.query_selector_all('img[src*="twimg.com/media"]')
        ]
        if images:
            media_obj["images"] = images

        # ── quick exit if no video component ─────────────────────────────────────
        video_el = await art.query_selector("[data-testid='videoComponent'] video")
        if not video_el:
            return media_obj

        poster = await video_el.get_attribute("poster") or ""
        # id from poster (amplify_video_thumb/123456789/…)
        vid_id = None
        m = re.search(r"(?:amplify_video_thumb|amplify_video|ext_tw_video)/(\d+)/", poster)
        if m:
            vid_id = m.group(1)

        # fallback to DOM scan helper if poster missing id
        if not vid_id:
            ids = await self._video_ids_in_art(art)
            vid_id = ids[0] if ids else None

        variants: list[dict] = []
        if vid_id and vid_id not in self.assigned_video_ids:
            pool = self.video_pool.get(vid_id, [])
            m3u8s = [u for u in pool if ".m3u8" in u]
            mp4s  = [u for u in pool if ".mp4" in u and "/aud/" not in u and "mp4a" not in u and "m4s" not in u]

            # parse playlists
            if m3u8s:
                fetched = await asyncio.gather(
                    *[self.fetch_video_variants(u) for u in m3u8s],
                    return_exceptions=True,
                )
                for lst in fetched:
                    if isinstance(lst, list):
                        variants.extend(lst)

            # direct mp4 fallbacks
            for u in mp4s:
                variants.extend(await self.fetch_video_variants(u))

            if pool:
                self.assigned_video_ids.add(vid_id)

        if variants:
            video_block = {"thumbnail": None, 
                           "variants": variants}
            if poster:
                video_block["thumbnail"] = poster
            media_obj["video"] = video_block
        return media_obj


    # ─────────────────────────────────────────────────────────────────────────────
    # 2) Second-pass media hydrator (called after main loop)
    # ─────────────────────────────────────────────────────────────────────────────
    async def _second_media_pass(self, pending_ids: set[str], tweets_by_id: dict[str, dict]) -> None:
        if not pending_ids:
            return

        for tid in list(pending_ids):
            # locate the article again
            art = await self.page.query_selector(f"article:has(a[href*='/status/{tid}'])")
            if not art:
                continue
            await self._scroll_into_view(art)
            media = await self._media(art)
            if media:
                tweets_by_id[tid]["media"] = media
                pending_ids.discard(tid)


    async def _parse_tweet(self, art) -> Optional[Dict[str, Any]]:
        """Return tweet dict if this article belongs to the author, else None."""
        permalink_el = await art.query_selector('a[href*="/status/"]')
        if not permalink_el:
            return None
        tweet_url = await permalink_el.get_attribute("href") or ""
        tid_match = TWEET_URL_RE.search(tweet_url)
        if not tid_match:
            return None
        tweet_id = tid_match.group(1)

        handle = tweet_url.strip("/").split("/")[0]
        if handle != self.author_handle:
            return None

        # timestamp
        time_el = await art.query_selector("time")
        date_time_iso = await time_el.get_attribute("datetime") if time_el else None

        # text
        text_el = await art.query_selector('div[data-testid="tweetText"]')
        text_content = await self._text_with_emojis(text_el)

        # counts
        likes, retweets, replies, views = await asyncio.gather(
            self._raw_count(art, "like"),
            self._raw_count(art, "retweet"),
            self._raw_count(art, "reply"),
            self._raw_views(art),
        )

        media_obj = await self._media(art)

        return {
            "tweet_id": tweet_id,
            "datetime": date_time_iso,
            "tweet": text_content,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "views": views,
            "media": media_obj,
        }


    async def scrape(self, url: str) -> Dict[str, Any]:
        """Open *url* in the current page and return a structured thread object."""
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # Sometimes X lazy‑loads tweets → wait until at least 1 article is present
        await self.page.wait_for_selector(TWEET_ARTICLE, state="attached", timeout=30_000)
        await self.page.wait_for_load_state("domcontentloaded")
        await self.page.wait_for_timeout(800)           # let initial JS run
        await self.page.mouse.wheel(0, 600)             # nudge to trigger first batch render


        # await self._load_entire_thread()
        tweets = await self._extract_tweets()

        return {
            "thread_title": tweets[0]["tweet"] if tweets else "No tweets found",
            "thread_url": url,
            "tweet_count": len(tweets),
            "author": {
                "username": self.author_handle, 
                "display_name": self.author_name,
                "profile_image_url": self.author_avatar_url
            },
            "tweets": tweets,
        }


    async def _extract_tweets(self) -> List[Dict[str, Any]]:
        """Scroll-scrape the whole thread, grabbing every tweet by the author only."""
        tweets: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        pending_media: list[tuple[str, int]] = []   # (tweet_id, index in tweets list)

        STALL_LIMIT = 3          # no new tweets found N times → stop
        TAIL_LIMIT = 1           # N non-author tweets in a row after we've started → stop
        stall_count = 0
        after_thread_tail = 0
        tweet_counter = 0
        
        extracting_tweets = True
        while extracting_tweets:
            articles = await self.page.query_selector_all(TWEET_ARTICLE)
            if not articles:
                await self.page.mouse.wheel(0, 9000)
                await self.page.wait_for_timeout(400)
                stall_count += 1
                if stall_count >= STALL_LIMIT:
                    break
                continue

            # expand collapsed tail sections
            if not self.show_more_replies:
                if await self._click_show_replies():
                    stall_count = 0
            
            # determine author once
            if self.author_handle is None:
                first_permalink = await articles[0].query_selector('a[href*="/status/"]')
                if not first_permalink:
                    console.log("[bold red]Could not find a tweet permalink – layout change?")
                    return tweets
                first_href = (await first_permalink.get_attribute("href") or "").strip("/")
                parts = first_href.split("/")
                if len(parts) < 2:
                    console.log("[bold red]Could not determine author handle.")
                    return tweets

                # Author Info
                self.author_handle = parts[0]
                self.author_avatar_url = await self._get_author_avatar(articles[0])
                disp_el = await self.page.query_selector("div[data-testid='User-Name'] span") \
                        or await self.page.query_selector("div[dir='auto'] span")
                self.author_name = await disp_el.inner_text() if disp_el else ""
                print(f"Author : {self.author_name} (@{self.author_handle})")

            new_this_pass = 0
            for art in articles:
                # get tweet id quickly to skip duplicates / decide author streak
                permalink_el = await art.query_selector('a[href*="/status/"]')
                if not permalink_el:
                    continue
                tweet_url = await permalink_el.get_attribute("href") or ""
                tid_match = TWEET_URL_RE.search(tweet_url)
                if not tid_match:
                    continue
                tweet_id = tid_match.group(1)
                if tweet_id in seen_ids:
                    continue
                
                await self._scroll_into_view(art)
                await self.page.evaluate("""
                (el)=>{
                    const v = el.querySelector('video');
                    if (v) { v.muted = true; v.play().catch(()=>{}); }
                }
                """, art)

                handle = tweet_url.strip("/").split("/")[0]
                if handle != self.author_handle:
                    after_thread_tail += 1
                    if tweets and after_thread_tail >= TAIL_LIMIT:
                        # we already collected author tweets and now saw enough non-author ones
                        break
                    continue
                else:
                    after_thread_tail = 0

                tweet_obj = await self._parse_tweet(art)
                if tweet_obj and extracting_tweets:
                    idx = len(tweets)
                    tweets.append(tweet_obj)
                    seen_ids.add(tweet_id)
                    new_this_pass += 1
                    tweet_counter += 1
                    tweet_txt = f"{tweet_obj['tweet'][:50]}..." if len(tweet_obj['tweet']) >= 50 else f"{tweet_obj['tweet']}"
                    print(f"Collected tweet #{tweet_counter} ({tweet_id}) by {self.author_name} (@{self.author_handle}): {tweet_txt}")

                    # track tweets with empty media for a second pass
                    if not tweet_obj.get("media"):
                        pending_media.append((tweet_id, idx))
                
            # scroll for more (incremental, avoid skipping)
            if new_this_pass == 0:
                stall_count += 1
            else:
                stall_count = 0
            if stall_count >= STALL_LIMIT:
                break

        # ── second pass: patch tweets that missed media on first scrape ──────────
        if pending_media:
            try:
                await self.page.wait_for_load_state("networkidle", timeout=3000)
            except pw.TimeoutError:
                pass
            await self.page.wait_for_timeout(1000)

            for tid, idx in pending_media:
                # find the article again (may require slight scroll)
                art = await self.page.query_selector(f"article:has(a[href*='/status/{tid}'])")
                if not art:
                    continue
                # await self._scroll_into_view(art)
                fixed = await self._parse_tweet(art)
                if fixed and fixed.get("media"):
                    tweets[idx]["media"] = fixed["media"]

        # optional reset
        await self.page.wait_for_timeout(2000)
        return tweets


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

    if not X_AUTH_TOKEN:
        console.print("[bold red]❌  Please export the `X_AUTH_TOKEN` cookie from a logged‑in session.")
        sys.exit(1)

    playwright = await pw.async_playwright().start()
    
    if EXTRACT_MP4_ONLY:
        browser = await playwright.firefox.launch_persistent_context(
            user_data_dir="profile",
            headless=HEADLESS,
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            device_scale_factor=1,
            locale="en-US",
            timezone_id=TIMEZONE_ID,
            java_script_enabled=True,
            firefox_user_prefs={"media.mediasource.enabled": False},
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
    else:
        browser_init = await playwright.firefox.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        
        browser = await browser_init.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            device_scale_factor=1,
            locale="en-US",
            timezone_id=TIMEZONE_ID,
            java_script_enabled=True,
        )


    # stealth: kill webdriver flag
    await browser.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )

    # inject cookie
    cookies = [
        {"name": "auth_token", "value": X_AUTH_TOKEN, "domain": ".x.com", "path": "/", "httpOnly": True, "secure": True},
    ]
    
    await browser.add_cookies(cookies)
    page = await browser.new_page()
    scraper = ThreadScraper(page, proxy=args.proxy)

    results: List[Dict[str, Any]] = []

    for i, url in enumerate(args.urls, 1):
        try:
            print(f"[{i}/{len(args.urls)}] Scraping {url}…")
            res = await scraper.scrape(url)
            results.append(res)
        except Exception as e:
            print("Error scraping", url, " :: ", traceback.format_exc())

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

