"""
reddit_scraper.py
=================
A respectful, read-only Reddit research tool that scrapes posts by flair
from any subreddit, extracts ticker mentions, scores sentiment, and
exports CSV / HTML / JSON reports.

Designed to comply with Reddit's API Terms of Service and Responsible
Builder Policy:
  - Read-only OAuth2 scope (never writes, votes, or posts)
  - Identifies itself with a well-formed User-Agent
  - Respects the 60 req/min rate limit with automatic backoff
  - Fetches only public data (no private subreddits, no DMs)
  - Cursor-based pagination (no hammering search repeatedly)

QUICK START
-----------
1. Copy .env.example → .env and fill in your Reddit API credentials.
2. Install dependencies:
       pip install praw pandas python-dotenv
3. Run:
       python reddit_scraper.py
       python reddit_scraper.py --subreddit wallstreetbets --days 7
       python reddit_scraper.py --subreddit investing --flair "Discussion" --days 30 --limit 200

ARGUMENTS
---------
  --subreddit   Subreddit name without r/ prefix  (default: ValueInvesting)
  --flair       Flair filter string               (default: Stock Analysis)
  --days        How many days back to look        (default: 14)
  --limit       Max posts to fetch (1-500)        (default: 100)
  --comments    Top N comments to read per post   (default: 10)
  --min-score   Min comment upvotes to include    (default: 5)
  --sort        Search sort: new|top|hot          (default: new)
  --out         Output directory                  (default: ./output)
  --no-html     Skip HTML report generation
  --no-json     Skip raw JSON export
  --quiet       Suppress progress output
"""

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import praw
import pandas as pd
from dotenv import load_dotenv
from praw.exceptions import RedditAPIException
from prawcore.exceptions import RequestException, ResponseException, TooManyRequests

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)

# ── Ticker extraction ──────────────────────────────────────────────────────────
# Matches $TICKER or bare TICKER (2-5 uppercase letters)
TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|\b([A-Z]{2,5})\b")

EXCLUDED = {
    # Articles / prepositions
    "I", "A", "AN", "THE", "AND", "OR", "BUT", "FOR", "NOR", "SO", "YET",
    "IN", "ON", "AT", "BY", "TO", "OF", "UP", "IF", "IS", "IT", "BE",
    "DO", "GO", "NO", "AS", "AM", "PM", "OK", "US", "EU", "UK",
    # Finance jargon (not tickers)
    "EPS", "PE", "PB", "PS", "PEG", "FCF", "DCF", "ROE", "ROA", "ROIC",
    "YOY", "QOQ", "TTM", "ATH", "ATL", "NAV", "AUM", "CAGR", "EBIT",
    "EBITDA", "SBC", "EV", "DD",
    # Reddit slang
    "IMO", "TBH", "OP", "OC", "FYI", "AFAIK", "YMMV", "TLDR", "AMA",
    "LOL", "LMAO", "WTF", "FWIW", "IIRC", "TIL", "ELI",
    # Corporate roles
    "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "SEC", "FED", "GDP", "CPI",
    "ML", "AI", "RE", "IT", "HR", "PR", "IR", "QA", "NA", "NY", "US",
    # Common words that look like tickers
    "ALL", "NEW", "NOW", "OLD", "OWN", "RUN", "SET", "BUY", "SELL",
    "HOLD", "LONG", "SHORT", "BULL", "BEAR", "PUTS", "CALL", "CALLS",
    "HIGH", "LOW", "RISK", "SAFE", "GOOD", "BEST", "MORE", "LESS",
    "WILL", "BEEN", "HAVE", "DOES", "WERE", "THEY", "WITH", "FROM",
    "THAT", "THIS", "WHAT", "WHEN", "THEN", "THAN", "VERY", "ALSO",
    "JUST", "STILL", "EVEN", "MOST", "SOME", "MANY", "MUCH", "SUCH",
    "EDIT", "NOTE", "ALSO", "NEXT", "LAST", "EACH", "BOTH", "ONLY",
    "INTO", "OVER", "THAN", "THEM", "WELL", "YOUR", "HAVE", "BEEN",
    "CASH", "DEBT", "LOSS", "GAIN", "RATE", "TERM", "YEAR", "WEEK",
    "SAME", "CASE", "AREA", "BACK", "REAL", "MADE", "KNOW", "TAKE",
    "HERE", "DOES", "LIKE", "TIME", "MAKE", "LOOK", "COME", "WORK",
    "PART", "SHOW", "NEED", "FEEL", "GOES", "DAYS", "NEWS", "MOVE",
}

BULLISH_WORDS = [
    "undervalued", "buy", "bullish", "upside", "growth", "cheap", "discount",
    "strong", "moat", "compounding", "catalyst", "outperform", "recommend",
    "accumulate", "opportunity", "attractive", "conviction", "long", "dip",
    "oversold", "margin of safety", "intrinsic value", "hidden gem",
]
BEARISH_WORDS = [
    "overvalued", "sell", "bearish", "downside", "decline", "expensive",
    "risky", "debt", "trap", "avoid", "short", "cut", "miss", "warning",
    "concern", "weak", "headwind", "competitive pressure", "dying", "obsolete",
    "overbought", "bubble", "fraud", "baghold",
]

EXPERT_PHRASES = [
    "i work", "i am a", "i'm a", "as a ", "in my field",
    "in my industry", "professionally", "in my experience",
    "i've worked", "i work in", "my job", "my career",
    "in my sector", "i work at", "i work for",
]

# ── Rate-limit aware request helper ───────────────────────────────────────────
# Reddit allows ~60 authenticated requests/minute.
# PRAW handles most throttling automatically, but we add an explicit
# inter-request delay and exponential backoff on 429s.

REQUEST_DELAY   = 1.0   # seconds between posts (stays well under 60/min)
MAX_RETRIES     = 5
BACKOFF_BASE    = 2     # seconds; doubles each retry


def _with_backoff(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) with exponential backoff on rate-limit errors."""
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except TooManyRequests:
            wait = BACKOFF_BASE ** attempt
            logger.warning("Rate limited — waiting %ss (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
            time.sleep(wait)
        except (RequestException, ResponseException) as exc:
            wait = BACKOFF_BASE ** attempt
            logger.warning("Network error: %s — retrying in %ss", exc, wait)
            time.sleep(wait)
    raise RuntimeError("Max retries exceeded. Reddit may be temporarily unavailable.")


# ── Core functions ─────────────────────────────────────────────────────────────

def build_reddit_client(client_id: str, client_secret: str, username: str) -> praw.Reddit:
    """
    Create a READ-ONLY Reddit client.

    User-Agent format required by Reddit:
        <platform>:<app_id>:<version> (by u/<reddit_username>)
    Docs: https://github.com/reddit-archive/reddit/wiki/API#rules
    """
    user_agent = (
        f"script:reddit-research-scraper:v2.0 (by u/{username}; "
        f"personal research tool, read-only, non-commercial)"
    )
    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
        ratelimit_seconds=300,   # PRAW will pause up to 5 min if rate-limited
    )
    reddit.read_only = True      # Explicitly enforce read-only mode
    return reddit


def extract_tickers(text: str) -> list[str]:
    """Extract probable stock tickers from free text."""
    found = set()
    for m in TICKER_RE.finditer(text):
        ticker = m.group(1) or m.group(2)
        if ticker and ticker not in EXCLUDED and len(ticker) >= 2:
            found.add(ticker)
    return sorted(found)


def score_sentiment(text: str) -> tuple[int, str]:
    """Return (numeric_score, label). Positive = bullish, negative = bearish."""
    tl = text.lower()
    bull = sum(1 for w in BULLISH_WORDS if w in tl)
    bear = sum(1 for w in BEARISH_WORDS if w in tl)
    score = bull - bear
    if score > 2:
        label = "Bullish"
    elif score > 0:
        label = "Leaning Bullish"
    elif score < -2:
        label = "Bearish"
    elif score < 0:
        label = "Leaning Bearish"
    else:
        label = "Neutral"
    return score, label


def _time_filter_for_days(days: int) -> str:
    """Map a lookback window to the closest PRAW time_filter value."""
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 365:
        return "year"
    return "all"


def scrape_subreddit(
    reddit: praw.Reddit,
    subreddit_name: str,
    flair_filter: str,
    lookback_days: int,
    post_limit: int,
    top_comments: int,
    min_comment_score: int,
    sort: str,
    quiet: bool,
) -> tuple[list[dict], Counter]:
    """
    Fetch posts matching flair_filter from subreddit_name.

    Uses PRAW's subreddit.search() which is the correct, rate-limit-friendly
    way to filter by flair (as opposed to scraping listing pages repeatedly).
    """
    sub       = reddit.subreddit(subreddit_name)
    cutoff_ts = (datetime.datetime.utcnow() - datetime.timedelta(days=lookback_days)).timestamp()
    tf        = _time_filter_for_days(lookback_days)

    posts_data: list[dict]    = []
    ticker_counter: Counter   = Counter()
    skipped_old               = 0

    if not quiet:
        print(f"\n  Searching r/{subreddit_name} | flair: \"{flair_filter}\" | "
              f"last {lookback_days} days | sort: {sort} | limit: {post_limit}\n")

    search_gen = _with_backoff(
        sub.search,
        query=f'flair:"{flair_filter}"',
        sort=sort,
        time_filter=tf,
        limit=post_limit,
    )

    for post in search_gen:
        # Skip posts outside the requested window
        if post.created_utc < cutoff_ts:
            skipped_old += 1
            continue

        post_date = datetime.datetime.utcfromtimestamp(post.created_utc)
        if not quiet:
            print(f"    [{post_date.strftime('%Y-%m-%d')}] {post.title[:72]}...")

        full_text              = f"{post.title} {post.selftext}"
        post_tickers           = extract_tickers(full_text)
        sentiment_score, s_label = score_sentiment(full_text)

        # ── Fetch comments ─────────────────────────────────────────────────────
        post.comment_sort = "top"
        _with_backoff(post.comments.replace_more, limit=0)

        comment_records: list[dict] = []
        expert_signals:  list[dict] = []
        all_tickers = list(post_tickers)

        for comment in list(post.comments)[:top_comments]:
            if not hasattr(comment, "score"):
                continue
            if comment.score < min_comment_score:
                continue

            c_tickers          = extract_tickers(comment.body)
            c_score, c_label   = score_sentiment(comment.body)
            all_tickers.extend(c_tickers)

            comment_records.append({
                "author":           str(comment.author),
                "score":            comment.score,
                "sentiment":        c_label,
                "tickers":          c_tickers,
                "preview":          comment.body[:250].replace("\n", " "),
            })

            # Detect expert/domain signals
            body_lower = comment.body.lower()
            if any(phrase in body_lower for phrase in EXPERT_PHRASES):
                expert_signals.append({
                    "author":  str(comment.author),
                    "score":   comment.score,
                    "excerpt": comment.body[:350].replace("\n", " "),
                    "tickers": c_tickers,
                })

        # Aggregate tickers
        unique_tickers = sorted(set(all_tickers))
        for t in unique_tickers:
            ticker_counter[t] += 1

        posts_data.append({
            "date":                    post_date.strftime("%Y-%m-%d"),
            "title":                   post.title,
            "url":                     f"https://reddit.com{post.permalink}",
            "score":                   post.score,
            "upvote_ratio":            round(post.upvote_ratio, 2),
            "num_comments":            post.num_comments,
            "flair":                   post.link_flair_text or "",
            "author":                  str(post.author),
            "sentiment_score":         sentiment_score,
            "sentiment_label":         s_label,
            "tickers":                 ", ".join(unique_tickers),
            "ticker_count":            len(unique_tickers),
            "expert_signal_count":     len(expert_signals),
            "top_comments":            comment_records,
            "expert_comments":         expert_signals,
            "body_preview":            post.selftext[:350].replace("\n", " ") if post.selftext else "(link post)",
        })

        time.sleep(REQUEST_DELAY)   # polite inter-request delay

    if not quiet and skipped_old:
        print(f"\n    (Skipped {skipped_old} posts outside the {lookback_days}-day window)")

    return posts_data, ticker_counter


# ── Export helpers ─────────────────────────────────────────────────────────────

def export_csv(posts_data: list[dict], out_dir: Path, stem: str) -> Path:
    rows = [{
        "Date":             p["date"],
        "Title":            p["title"],
        "Author":           p["author"],
        "Score":            p["score"],
        "Upvote Ratio":     p["upvote_ratio"],
        "Comments":         p["num_comments"],
        "Flair":            p["flair"],
        "Sentiment":        p["sentiment_label"],
        "Tickers":          p["tickers"],
        "Expert Signals":   p["expert_signal_count"],
        "URL":              p["url"],
        "Body Preview":     p["body_preview"],
    } for p in posts_data]

    path = out_dir / f"{stem}.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


def export_json(posts_data: list[dict], out_dir: Path, stem: str) -> Path:
    path = out_dir / f"{stem}_raw.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(posts_data, f, indent=2, ensure_ascii=False, default=str)
    return path


def export_html(
    posts_data: list[dict],
    ticker_counter: Counter,
    out_dir: Path,
    stem: str,
    subreddit: str,
    flair: str,
    days: int,
) -> Path:
    top_tickers = ticker_counter.most_common(25)

    ticker_rows = "\n".join(
        f"<tr><td><code>${t}</code></td>"
        f"<td><div class='bar' style='width:{min(c*18,260)}px'></div></td>"
        f"<td>{c}</td></tr>"
        for t, c in top_tickers
    )

    sentiment_colors = {
        "Bullish":         ("#d4edda", "#155724"),
        "Leaning Bullish": ("#d1ecf1", "#0c5460"),
        "Neutral":         ("#e2e3e5", "#383d41"),
        "Leaning Bearish": ("#fff3cd", "#856404"),
        "Bearish":         ("#f8d7da", "#721c24"),
    }

    post_cards = ""
    for p in sorted(posts_data, key=lambda x: x["score"], reverse=True):
        sc_bg, sc_fg = sentiment_colors.get(p["sentiment_label"], ("#e2e3e5", "#383d41"))

        expert_html = ""
        for ex in p["expert_comments"]:
            t_str = " ".join(f"<code>${t}</code>" for t in ex["tickers"]) or "—"
            expert_html += f"""
            <div class="expert-quote">
              <span class="author">u/{ex['author']}</span>
              <span class="escore">↑{ex['score']}</span>
              {t_str}
              <p>"{ex['excerpt']}"</p>
            </div>"""

        expert_badge = (
            f'<span class="badge badge-expert">expert x{p["expert_signal_count"]}</span>'
            if p["expert_signal_count"] > 0 else ""
        )

        ticker_pills = " ".join(
            f'<code>{t}</code>'
            for t in p["tickers"].split(", ") if t
        )

        post_cards += f"""
        <div class="post-card">
          <div class="post-header">
            <div>
              <span class="post-date">{p['date']}</span>
              <h3><a href="{p['url']}" target="_blank" rel="noopener">{p['title']}</a></h3>
            </div>
            <div class="badges">
              <span class="badge">↑ {p['score']}</span>
              <span class="badge">💬 {p['num_comments']}</span>
              <span class="badge" style="background:{sc_bg};color:{sc_fg}">{p['sentiment_label']}</span>
              {expert_badge}
            </div>
          </div>
          {f'<p class="body-preview">{p["body_preview"]}</p>' if p["body_preview"] != "(link post)" else ""}
          {f'<div class="tickers">{ticker_pills}</div>' if ticker_pills else ""}
          {expert_html}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>r/{subreddit} — {flair} Digest</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         max-width:940px;margin:40px auto;padding:0 20px;
         color:#1a1a1a;background:#f7f7f7;line-height:1.6}}
    h1{{font-size:22px;font-weight:600;margin-bottom:4px}}
    h2{{font-size:15px;font-weight:600;margin:28px 0 10px;
        border-bottom:1px solid #ddd;padding-bottom:6px;color:#333}}
    h3{{font-size:14px;font-weight:500;margin:4px 0}}
    h3 a{{color:#0066cc;text-decoration:none}}
    h3 a:hover{{text-decoration:underline}}
    .meta{{font-size:13px;color:#777;margin-bottom:24px}}
    table{{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:24px}}
    td,th{{padding:6px 12px;border:1px solid #ddd;text-align:left;vertical-align:middle}}
    th{{background:#f0f0f0;font-weight:500}}
    .bar{{height:10px;background:#4a9eff;border-radius:3px}}
    code{{background:#eef2ff;padding:1px 5px;border-radius:3px;
          font-size:12px;font-family:monospace;color:#3730a3}}
    .post-card{{background:#fff;border:1px solid #e0e0e0;border-radius:8px;
               padding:16px;margin:10px 0}}
    .post-header{{display:flex;justify-content:space-between;
                  align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:8px}}
    .post-date{{font-size:11px;color:#999;display:block;margin-bottom:2px}}
    .badges{{display:flex;gap:6px;flex-wrap:wrap;align-items:center}}
    .badge{{font-size:11px;padding:2px 8px;border-radius:12px;
            background:#f0f0f0;color:#444;white-space:nowrap}}
    .badge-expert{{background:#d4edda;color:#155724}}
    .body-preview{{font-size:13px;color:#555;margin:6px 0}}
    .tickers{{margin:6px 0;font-size:13px}}
    .expert-quote{{background:#fffbea;border-left:3px solid #f59e0b;
                   padding:8px 12px;margin:8px 0;border-radius:0 4px 4px 0}}
    .expert-quote .author{{font-weight:600;font-size:13px}}
    .expert-quote .escore{{font-size:12px;color:#888;margin:0 8px}}
    .expert-quote p{{font-size:13px;color:#555;margin-top:4px;font-style:italic}}
  </style>
</head>
<body>
  <h1>r/{subreddit} — {flair} Digest</h1>
  <p class="meta">
    Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} &nbsp;·&nbsp;
    Last {days} days &nbsp;·&nbsp;
    {len(posts_data)} posts scraped
  </p>

  <h2>Most-mentioned tickers</h2>
  <table>
    <tr><th>Ticker</th><th>Frequency</th><th>Posts</th></tr>
    {ticker_rows}
  </table>

  <h2>Posts — sorted by upvotes</h2>
  {post_cards}
</body>
</html>"""

    path = out_dir / f"{stem}_report.html"
    path.write_text(html, encoding="utf-8")
    return path


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="reddit_scraper",
        description="Read-only Reddit research scraper — fetches posts by flair, "
                    "extracts tickers, scores sentiment, exports reports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--subreddit",  default="ValueInvesting",  help="Subreddit name (no r/ prefix)")
    p.add_argument("--flair",      default="Stock Analysis",  help="Flair text to filter by")
    p.add_argument("--days",       type=int,   default=14,    help="Lookback window in days")
    p.add_argument("--limit",      type=int,   default=100,   help="Max posts to fetch (1-500)")
    p.add_argument("--comments",   type=int,   default=10,    help="Top N comments per post")
    p.add_argument("--min-score",  type=int,   default=5,     help="Min comment upvotes to include")
    p.add_argument("--sort",       default="new",             help="Sort: new | top | hot",
                   choices=["new", "top", "hot", "relevance"])
    p.add_argument("--out",        default="./output",        help="Output directory")
    p.add_argument("--no-html",    action="store_true",       help="Skip HTML report")
    p.add_argument("--no-json",    action="store_true",       help="Skip raw JSON export")
    p.add_argument("--quiet",      action="store_true",       help="Suppress progress output")
    return p.parse_args()


def load_credentials() -> tuple[str, str, str]:
    """
    Load Reddit API credentials from environment variables or a .env file.

    Required variables:
        REDDIT_CLIENT_ID      — from reddit.com/prefs/apps
        REDDIT_CLIENT_SECRET  — from reddit.com/prefs/apps
        REDDIT_USERNAME       — your Reddit username (for User-Agent)
    """
    load_dotenv()   # reads .env in the current directory if present

    client_id     = os.getenv("REDDIT_CLIENT_ID",     "")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
    username      = os.getenv("REDDIT_USERNAME",      "")

    missing = [k for k, v in [
        ("REDDIT_CLIENT_ID",     client_id),
        ("REDDIT_CLIENT_SECRET", client_secret),
        ("REDDIT_USERNAME",      username),
    ] if not v]

    if missing:
        print("\n[ERROR] Missing credentials. Create a .env file with:\n")
        for k in missing:
            print(f"    {k}=your_value_here")
        print("\nSee .env.example for a template. "
              "Get your credentials at: https://www.reddit.com/prefs/apps\n")
        sys.exit(1)

    return client_id, client_secret, username


def main() -> None:
    args   = parse_args()
    client_id, client_secret, username = load_credentials()

    # Build output directory and file stem
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    safe_sub  = re.sub(r"[^\w]", "_", args.subreddit)
    safe_days = args.days
    stem      = f"{safe_sub}_{safe_days}d_{timestamp}"

    # ── Auth ──────────────────────────────────────────────────────────────────
    reddit = build_reddit_client(client_id, client_secret, username)

    if not args.quiet:
        print("=" * 62)
        print("  Reddit Research Scraper  (read-only)")
        print("=" * 62)
        print(f"  Subreddit : r/{args.subreddit}")
        print(f"  Flair     : {args.flair}")
        print(f"  Lookback  : {args.days} days")
        print(f"  Limit     : {args.limit} posts")
        print(f"  Sort      : {args.sort}")
        print(f"  Output    : {out_dir.resolve()}")
        print("=" * 62)

    # ── Scrape ────────────────────────────────────────────────────────────────
    try:
        posts_data, ticker_counter = scrape_subreddit(
            reddit          = reddit,
            subreddit_name  = args.subreddit,
            flair_filter    = args.flair,
            lookback_days   = args.days,
            post_limit      = args.limit,
            top_comments    = args.comments,
            min_comment_score = args.min_score,
            sort            = args.sort,
            quiet           = args.quiet,
        )
    except RedditAPIException as exc:
        print(f"\n[ERROR] Reddit API error: {exc}")
        sys.exit(1)
    except RuntimeError as exc:
        print(f"\n[ERROR] {exc}")
        sys.exit(1)

    if not posts_data:
        print(f"\n[WARN] No posts found. Try --days {args.days * 2} or a different --flair.")
        sys.exit(0)

    if not args.quiet:
        print(f"\n  Found {len(posts_data)} posts.\n")
        print("  Top tickers:")
        for ticker, count in ticker_counter.most_common(15):
            bar = "█" * min(count * 3, 32)
            print(f"    ${ticker:<6s} {bar} {count}")
        print()

    # ── Export ────────────────────────────────────────────────────────────────
    csv_path = export_csv(posts_data, out_dir, stem)
    if not args.quiet:
        print(f"  [CSV]  {csv_path}")

    if not args.no_json:
        json_path = export_json(posts_data, out_dir, stem)
        if not args.quiet:
            print(f"  [JSON] {json_path}")

    if not args.no_html:
        html_path = export_html(
            posts_data, ticker_counter, out_dir, stem,
            args.subreddit, args.flair, args.days,
        )
        if not args.quiet:
            print(f"  [HTML] {html_path}")

    if not args.quiet:
        print(f"\n  Done. Open {stem}_report.html to browse results.\n")


if __name__ == "__main__":
    main()
