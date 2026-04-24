# LA Property Lookup

Web app + GitHub Actions scraper for any LA property address. Pulls zoning, assessor, permits, code enforcement, and certificate of occupancy from ZIMAS and LADBS.

**Live at:** `https://palgorhythm.github.io/la-property-lookup`

## Architecture

```
Browser (GitHub Pages)
  → POST /lookup     → Cloudflare Worker  → triggers GitHub Actions workflow
  → GET  /result/:id → Cloudflare Worker  → polls output/{id}.md in this repo
                                                ↑
                                          GitHub Actions
                                          (Playwright scrape of ZIMAS + LADBS)
                                          commits result to output/
```

- **Frontend:** `index.html` served via GitHub Pages — just an address field, no auth required
- **Worker:** `cloudflare/worker.js` proxies requests to GitHub API, keeps the PAT server-side
- **Scraper:** `.github/workflows/lookup.yml` runs on Ubuntu, installs Playwright, scrapes both sites, commits the markdown result
- **Cleanup:** `.github/workflows/cleanup.yml` runs every 6 hours, deletes result files older than 24 hours

## Deployment

### First-time setup

**1. Deploy the Cloudflare Worker**

```bash
cd cloudflare
npm install wrangler --save-dev
npx wrangler login
npx wrangler deploy
```

Set your GitHub PAT as a secret (needs `repo` + `workflow` scopes):

```bash
npx wrangler secret put GITHUB_PAT
```

Note the deployed URL (e.g. `https://la-property-lookup.<your-subdomain>.workers.dev`).

**2. Update `index.html` with the worker URL**

Replace `REPLACE_WITH_WORKER_URL` in `index.html` with your worker URL, then push to main.

**3. Enable GitHub Pages**

In the repo settings → Pages → set source to **Deploy from branch**, branch `main`, folder `/` (root).

The site will be live at `https://palgorhythm.github.io/la-property-lookup`.

### Updates

To redeploy the worker after changes:

```bash
cd cloudflare && npx wrangler deploy
```

To rotate the GitHub PAT:

```bash
cd cloudflare && npx wrangler secret put GITHUB_PAT
```

## Local CLI

```bash
pip install -r requirements.txt
playwright install chromium
python lookup.py "1923 Preston Ave" --save report.md
```

Flags: `--output json`, `--headed` (visible browser), `--screenshots`.

## Output

Reports are structured markdown: zoning, TOC tier, assessor data, hazards, all permits (linked to LADBS detail pages), code enforcement cases, certificate of occupancy, and a raw text dump. See `1923_preston_ave.md` and `1815_park_dr.md` for examples.

Lookups take ~2–4 minutes (Playwright navigates and waits for AJAX on both city sites). Results are cached in `output/` for 24 hours.
