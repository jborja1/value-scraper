# Reddit Research Scraper

A read-only, rate-limit-compliant Python tool for scraping posts by flair from any
subreddit. Built for personal investment research — extracts stock ticker mentions,
scores post sentiment, detects domain-expert comments, and exports CSV / HTML / JSON
reports.

---

## Why this tool exists (for Reddit API application)

This script was built to support personal, non-commercial investment research. The
goal is to read public posts in subreddits like r/ValueInvesting, identify which
stocks domain experts are discussing, and cross-reference those ideas with fundamental
analysis.

**The tool is strictly read-only:**
- It never posts, votes, comments, or modifies any Reddit data
- It only reads publicly visible posts and comments
- It identifies itself with a well-formed, honest `User-Agent` string
- It respects the 60 req/min rate limit with built-in delays and exponential backoff
- It uses PRAW's `read_only = True` mode, which prevents any write operations at
  the library level

**Expected usage volume:** Low. Typically 50–150 API calls per run, run manually a
few times per week. Well within the free tier.

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/reddit-scraper.git
cd reddit-scraper
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Get Reddit API credentials

1. Go to [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps)
2. Click **"are you a developer? create an app..."**
3. Choose type: **script**
4. Set redirect URI to: `http://localhost:8080`
5. Copy your **client ID** (shown under the app name) and **client secret**

### 4. Configure credentials

```bash
cp .env.example .env
# Edit .env and fill in your values — this file is in .gitignore
```

Your `.env` should look like:

```
REDDIT_CLIENT_ID=abc123xyz
REDDIT_CLIENT_SECRET=secretvalue
REDDIT_USERNAME=your_reddit_username
```

> **Security note:** `.env` is listed in `.gitignore` and will never be committed.
> Never hardcode credentials in the script itself.

---

## Usage

```bash
# Default: r/ValueInvesting, "Stock Analysis" flair, last 14 days
python reddit_scraper.py

# Custom subreddit and timespan
python reddit_scraper.py --subreddit investing --days 7

# Different flair, more posts, custom output directory
python reddit_scraper.py --subreddit SecurityAnalysis --flair "DD" --days 30 --limit 200 --out ./reports

# Suppress output, CSV only (no HTML/JSON)
python reddit_scraper.py --quiet --no-html --no-json

# Sort by top posts instead of newest
python reddit_scraper.py --sort top --days 7
```

### All arguments

| Argument | Default | Description |
|---|---|---|
| `--subreddit` | `ValueInvesting` | Subreddit name (no `r/` prefix) |
| `--flair` | `Stock Analysis` | Flair filter string |
| `--days` | `14` | How many days back to look |
| `--limit` | `100` | Max posts to fetch (1–500) |
| `--comments` | `10` | Top N comments to read per post |
| `--min-score` | `5` | Min comment upvotes to include |
| `--sort` | `new` | Sort order: `new`, `top`, `hot`, `relevance` |
| `--out` | `./output` | Output directory |
| `--no-html` | — | Skip HTML report generation |
| `--no-json` | — | Skip raw JSON export |
| `--quiet` | — | Suppress all progress output |

---

## Output files

Each run produces three files in the output directory, timestamped to avoid overwrites:

| File | Description |
|---|---|
| `{subreddit}_{days}d_{timestamp}.csv` | Flat table of all posts — easy to open in Excel |
| `{subreddit}_{days}d_{timestamp}_raw.json` | Full structured data including all comments |
| `{subreddit}_{days}d_{timestamp}_report.html` | Visual report with ticker frequency chart and post cards |

---

## How it detects expert signals

Comments are scanned for phrases like `"I work in"`, `"as a [profession]"`,
`"in my experience"`, etc. Posts containing these are flagged in the report with an
**expert** badge, making it easy to find domain-insider perspectives.

---

## Rate limiting and Reddit compliance

- **User-Agent:** Identifies itself as `script:reddit-research-scraper:v2.0 (by u/<username>; personal research tool, read-only, non-commercial)` — the format required by Reddit's API rules.
- **Request delay:** 1 second between posts (well under the 60 req/min limit).
- **Backoff:** Automatically waits and retries on `429 Too Many Requests` responses with exponential backoff.
- **Read-only mode:** `reddit.read_only = True` is set at the PRAW level — the client is physically incapable of writing data.
- **No scraping loops:** Uses PRAW's `subreddit.search()` with cursor-based pagination rather than hammering listing endpoints.

---

## License

MIT — personal and commercial use permitted. See [LICENSE](LICENSE).
