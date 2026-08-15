# Deploying to Render.com

Step-by-step walkthrough for putting the WoW AH Sniper web app live on Render's free
"Hobby" plan — no credit card required. Render builds and runs the app from the
`Dockerfile` + `render.yaml` already in this repo, so most of this is just clicking
through the dashboard.

## Before you start

- [ ] This repo is pushed to GitHub (`github.com/petrdvorsky9/wow-ah-sniper`) — done.
- [ ] You have an Undermine Exchange API key (from https://undermine.exchange/api.html,
      free with a Patreon sign-in). You'll paste this into Render, never into the repo.
- [ ] A free [Render.com](https://render.com) account, signed up via your GitHub account
      (this makes step 2 below a one-click repo picker instead of a manual URL paste).

## Steps

### 1. Sign in to Render and connect GitHub

Go to [dashboard.render.com](https://dashboard.render.com) and sign in / sign up using
**"Sign up with GitHub"** so Render can see your repos without extra setup.

### 2. Create a new Blueprint

- Click **New +** (top right) → **Blueprint**.
- Pick the `petrdvorsky9/wow-ah-sniper` repo from the list (search if it's not visible;
  you may need to click "Configure account" to grant Render access to it).
- Render finds `render.yaml` at the repo root automatically and shows you a preview of
  the one service it defines (`wow-ah-sniper`, a Docker web service on the free plan).

### 3. Set the API key secret

- Render will prompt for any environment variable marked `sync: false` in `render.yaml`
  — that's `UNDERMINE_API_KEY`.
- Paste in your real Undermine Exchange API key here. **Never** put the real key in
  `render.yaml` or any committed file — this is the one place it's safe to enter it.

### 4. Approve and deploy

- Click **Apply** / **Create New Resources** to confirm the Blueprint.
- Render builds the Docker image (installs `requirements.txt`, bakes in the app) and
  starts the container. First build typically takes 2–4 minutes — watch the live log
  in the dashboard.
- Once it says **Live**, your app is reachable at:
  `https://wow-ah-sniper.onrender.com` (or `https://<name>.onrender.com` if Render
  had to append a suffix because the name was taken).

### 5. Verify it works

- Open the URL above — you should see the search box and the Midnight Flasks overview.
- Try a search (e.g. `Copper Ore`) to confirm the Undermine API key made it through and
  a full report renders.

## Updating the app later

Every `git push` to `master` on GitHub triggers an automatic redeploy on Render — no
manual steps needed. Just:

```powershell
git add -A
git commit -m "your change"
git push
```

Watch the deploy progress in the Render dashboard under the service's **Events** tab.

## Things to know about the free plan

| | |
|---|---|
| RAM / CPU | 512 MB / 0.1 CPU — plenty for this app (Flask + gunicorn, in-memory caches, no DB) |
| Cost | $0/month realistically — 750 free instance-hours/month, 5 GB bandwidth, 500 build-minutes/month all comfortably cover personal/light-shared use |
| Cold start | Spins down after 15 minutes idle; next request takes ~30–60s to wake it back up. Upgrade to the `Starter` plan (~$7/month) if that ever bothers you — just change `plan: free` to `plan: starter` in `render.yaml` and push |
| Region | `render.yaml` pins `region: frankfurt` (closest to the EU auction data this app queries) |
| HTTPS | Free and automatic, no setup needed |

## Rate limiting note

Once this is public, every visitor's requests share your one Undermine API key, so
`/report` is capped at 30 requests/hour per visitor IP (`webapp.py`'s `@limiter.limit(...)`).
Adjust that number if you want it looser/stricter for your use case.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Blueprint creation fails to find `render.yaml` | Make sure you're pointing at the `wow-ah-sniper` repo, not `ai-fun` or another repo — `render.yaml` must be at the repo root (it is, in this repo) |
| Build fails on `pip install` | Check the build log for the exact package/error; `requirements.txt` pins minimum versions, a transient PyPI issue is the most common cause — just retry the deploy |
| App is live but every price lookup errors out | `UNDERMINE_API_KEY` secret is missing/wrong — check it under the service's **Environment** tab in the Render dashboard |
| Site loads slowly on first visit | Normal cold start after 15 min idle (see table above) — it'll be fast on the next request |
| Wowhead-sourced data (recipes, item names) intermittently fails | Wowhead's bot detection occasionally blocks scraping under load; the report still renders with a "temporarily unavailable" note for that section rather than failing outright |
