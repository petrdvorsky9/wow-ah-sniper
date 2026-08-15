# WoW Auction House Sniper

Watches World of Warcraft auction house prices and alerts you when an item drops
to or below your target price.

**Two data sources supported — Undermine Exchange is the default/main source:**

| Source | What it gives you | Requires |
|---|---|---|
| `undermine` *(default)* | Aggregated current market price via [Undermine Exchange API](https://undermine.exchange/api.html) | Undermine API key (free Patreon) |
| `blizzard` | Raw live auction listings directly from Blizzard | Battle.net developer app |

## Setup

### 1. Copy `.env.example` to `.env`

```
cp .env.example .env
```

### 2. Add credentials

**Undermine Exchange (needed for the default source):**
- Sign in with Patreon at https://undermine.exchange/ (free account is fine)
- Reveal your key at https://undermine.exchange/api.html
- Set `UNDERMINE_API_KEY` in `.env`

**Blizzard API (only needed if you use `--source blizzard`):**
- Create an application at https://develop.battle.net/ (no special scopes needed)
- Set `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET` in `.env`

### 3. Install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Usage

### Web app (search UI)

For interactive lookups without touching the CLI, run the local web app:

```powershell
.\.venv\Scripts\python webapp.py
```

Then open `http://127.0.0.1:5000` in your browser. Type an item name (e.g. `Copper Ore`),
a numeric item ID, or a Wowhead link, and it renders the same dashboard the CLI/skill
produces as a `.html` file — live in the page, no file left behind. Commodity vs.
realm-item scope is auto-detected (tries the region-wide commodity market first, falls
back to a realm lookup); use the "Advanced" section to override the region, pin a
specific realm, or skip the recipes section for a faster response.

This binds to `127.0.0.1` (localhost only) by default — it's not exposed to your
network. Pass `--port <N>` to use a different port, or `--host` to change the bind
address if you know what you're doing.

> **Note:** free-text name search is a best-effort scrape of Wowhead's search results
> and occasionally picks an unexpected item variant (e.g. a bonus-id twin with the same
> name). If that happens, paste the numeric item ID or the item's Wowhead link instead
> — that path is always exact.

The landing page also shows a **Midnight Flasks overview** beneath the search box: current
EU price and a "good day to buy/sell today" signal (derived from the same weekday
buy/sell-pattern logic as the main dashboard) for the higher-quality (Rank 2 / ilvl 295)
version of each of the four Midnight combat flasks — Thalassian Resistance, the Magisters,
the Blood Knights, and the Shattered Sun. The lower-ilvl Rank 1 twins are intentionally
excluded. This is cached server-side for 10 minutes (`FLASK_OVERVIEW_TTL_SECONDS` in
`webapp.py`) so repeat visits to the landing page don't refetch on every request.

### Deploying publicly

The web app can be published so other people can use it, via the `Dockerfile` in this
folder (works unmodified on either platform below — it reads the `PORT` env var with
an 8080 fallback). A few things worth knowing before you do, regardless of platform:

- **The Undermine API key becomes shared** — every visitor's requests go through your
  key, so `/report` is rate-limited to 30 requests/hour per visitor IP (`webapp.py`'s
  `@limiter.limit(...)`) to keep any one person from exhausting it. Adjust to taste.
- **Wowhead scraping doesn't scale infinitely** — heavy public traffic increases the
  odds of hitting Wowhead's bot detection. Fine for casual/shared use; if this gets
  serious traffic, look at adding a persistent (disk/Redis) cache in front of
  `fetch_wowhead_item_meta`/`fetch_wowhead_reagent_for`.
- The container runs a single gunicorn worker (with threads, not processes) on
  purpose — that keeps the in-memory caches (`_cached_item_meta`, the flask overview
  cache, etc.) actually shared across requests. If you ever need to scale to multiple
  machines, those caches — and the rate limiter's storage — would need to move to
  something shared (e.g. Redis) instead.

**Option A: Render.com (`render.yaml`) — genuinely free, no card required**
*(full step-by-step walkthrough: [`DEPLOY_RENDER.md`](DEPLOY_RENDER.md))*

Render's free "Hobby" web service gives 512MB RAM / 0.1 CPU, 750 free instance-hours
per workspace per month (a personal/low-traffic tool like this stays well within that,
and within the 5GB/month bandwidth and 500 build-minutes/month included limits too —
realistically **$0/month**). The trade-off: it spins down after 15 minutes with no
requests and takes ~30-60s to wake back up on the next one. If that cold start ever
bothers you, the next tier up (`Starter`, ~$7/month) stays always-on.

```
1. Push this repo to a GitHub/GitLab remote (Render deploys from git, not a local folder)
2. Render dashboard -> New -> Blueprint -> select the repo (reads render.yaml automatically)
3. When prompted, set the UNDERMINE_API_KEY secret (never commit a real key)
4. Deploy — future git pushes to your default branch auto-deploy
```

Your app will be live at `https://<service-name>.onrender.com` with free HTTPS.

**Option B: Fly.io (`fly.toml`) — pay-as-you-go, no free tier for new accounts**

Fly dropped its free tier for new accounts in late 2024 — you'll need a card on file
and pay per-second of actual machine runtime. `fly.toml` here is already set to scale
to zero when idle (`auto_stop_machines`), so for light personal use this tends to land
around $1-3/month rather than the ~$30/month "always-on" numbers you'll see quoted —
but it's not literally free the way Render's Hobby plan is.

```powershell
# 1. Install flyctl: https://fly.io/docs/flyctl/install/
# 2. Sign in
fly auth login

# 3. Create/rename the app on Fly (reads fly.toml; pick a unique name if prompted)
fly launch --no-deploy

# 4. Set your API key as a secret (never put it in fly.toml or the image)
fly secrets set UNDERMINE_API_KEY=your-key-here

# 5. Build and deploy
fly deploy
```

Your app will be live at `https://<app-name>.fly.dev` with free HTTPS. Re-run
`fly deploy` after any code change. `auto_stop_machines` in `fly.toml` scales the
machine to zero when idle so you're not paying for runtime nobody's using — the first
request after a quiet spell just takes a few extra seconds to wake back up.

### One-off price check (CLI)

Commodities (stackable trade goods — ore, herbs, cloth, etc.) are priced region-wide:

```powershell
# Undermine (default)
.\.venv\Scripts\python sniper.py check --item-id 251285 --region eu --commodity

# Blizzard source
.\.venv\Scripts\python sniper.py check --item-id 251285 --region eu --commodity --source blizzard
```

Non-commodity items (gear, mounts, pets) are per realm:

```powershell
.\.venv\Scripts\python sniper.py check --item-id 118852 --region eu --realm drakthul
```

Find item IDs on [Wowhead](https://www.wowhead.com) — the ID is in the URL,
e.g. `wowhead.com/item=251285/petrified-root` → `251285`.

### Watch mode (continuous sniping)

Edit `watchlist.yaml` to add items and your target buy prices, then run:

```powershell
.\.venv\Scripts\python sniper.py watch --interval 300
```

This polls every 5 minutes and prints `<<< DEAL!` plus a terminal bell whenever a
watched item's price is at or below `max_price_gold`.

To use Blizzard as the default source for the whole watchlist:

```powershell
.\.venv\Scripts\python sniper.py watch --source blizzard
```

Or set `source: blizzard` on individual items in `watchlist.yaml` to mix sources.

> **Tip:** Undermine's data refreshes roughly once an hour (matching Blizzard's AH
> snapshot cadence), so polling much faster won't get you newer data — it just burns
> your rate-limit budget (3,000 points/hour per Undermine key). Blizzard's endpoint
> also updates ~hourly.

## Files

| File | Purpose |
|---|---|
| `undermine_client.py` | Undermine Exchange API client (main/default source) |
| `blizzard_client.py` | Blizzard Game Data API client (alternative source) |
| `sniper.py` | CLI: `check` (one-off) and `watch` (polling loop) |
| `item_report.py` | Report pipeline (price/history fetch, recipes, HTML dashboard) — used by both the CLI and the web app |
| `webapp.py` | Flask web app — search UI in front of `item_report.py`; also the entry point gunicorn imports in production |
| `watchlist.yaml` | Items to snipe with target prices and optional source override |
| `.env` | Your API credentials (never committed) |
| `Dockerfile` | Container image for deploying the web app (see "Deploying publicly" below) |
| `render.yaml` | Render.com Blueprint config, used when deploying via Render's dashboard |
| `DEPLOY_RENDER.md` | Step-by-step Render.com deployment walkthrough |
| `fly.toml` | Fly.io app config used by `fly launch`/`fly deploy` |

## Notes

- Prices are always in copper internally; the tool converts to gold/silver/copper for display.
- Blizzard realm auction dumps can be large (10k+ listings). They are cached in memory
  for 5 minutes to avoid redundant fetches during a watch cycle.
- No credentials are ever printed by this tool.
