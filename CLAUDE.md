# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An automated AI job search engine for Yotam Kadishay (Engineering Manager / Director / VP roles in Israel or global remote). The pipeline runs on GitHub Actions, scrapes and scores jobs via Claude, and publishes a Material 3 PWA tracker to GitHub Pages.

## Running the Pipeline

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export BRAVE_API_KEY=...

python run_pipeline.py              # full run + git push
python run_pipeline.py --no-push    # full run, skip git push
python run_pipeline.py --score-only # re-score existing jobs without searching
```

Trigger via GitHub Actions: **Actions → Job Search Pipeline → Run workflow**

## Architecture

```
run_pipeline.py          Pipeline (search → scrape → score → filter → merge → push)
config.json              Target roles, locations, company lists, scoring thresholds
profile.md               Yotam's profile used as scoring context for Claude Haiku
scored_jobs.json         Source of truth for jobs (root copy)
docs/scored_jobs.json    GitHub Pages copy (kept in sync by pipeline)
docs/index.html          Single-file Material 3 PWA (~1600 lines, no build step)
docs/sw.js               Service worker — network-first for HTML/JSON, cache-first otherwise
docs/manifest.json       PWA manifest
.github/workflows/
  pipeline.yml           Cron job (07:00 UTC Sun–Thu) — runs pipeline, pushes scored_jobs.json
  deploy.yml             Deploys docs/ to GitHub Pages; triggers on workflow_run after pipeline
```

### Pipeline Steps (run_pipeline.py)

1. **search_jobs()** — Claude Opus agentic loop with `web_search` tool (Brave Search API). Returns 8–15 job postings as JSON. Has 3-tier fallback: direct extraction → Haiku reformat → Opus JSON-only retry.
2. **scrape_ats()** — Hits public APIs for all companies in `config.json`: Greenhouse (`boards-api.greenhouse.io/v1/boards/{slug}/jobs`), Lever (`api.lever.co/v0/postings/{slug}`), Ashby (`jobs.ashbyhq.com/api/non-user-facing/posting-board/job-board/list`), Comeet.
3. **score_jobs()** — Claude Haiku scores each new job 1–10 against `profile.md`. Returns `fit_score`, `score_reason`, `ai_opener`, `location_ok`, `relocation_required`.
4. **filter_jobs()** — Rejects: score < `min_score` (4), `location_ok=false` (non-Israel/non-global-remote), posted > 30 days ago.
5. **merge_jobs()** — Append-only: never deletes existing jobs. Deduplicates by URL and `company|title` key. Sorts manual/high-score first.
6. **git_push()** — Commits with `[skip ci]` message; deploy is triggered separately via `workflow_run` on the pipeline workflow.

### PWA Tracker (docs/index.html)

Single HTML file, no build step. Key design decisions:
- **Job data** loaded from `scored_jobs.json` at runtime via `fetch()`
- **User state** (status, notes, interview rounds, manual jobs) stored in `localStorage` under `jf_*` keys — never written back to the repo
- **Statuses**: `open` (default), `applied`, `interview`, `offer`, `rejected`, `irrelevant`. Jobs with `irrelevant` status are hidden from all views except the 🚫 Irrelevant filter.
- **AI features** (cover letter, interview prep, voice sim, company dossier, CV tailoring, Maya coach) call the Anthropic API directly from the browser using `streamClaude()` with SSE streaming. Requires user to enter their API key in Settings.
- **Default view**: Open filter is active on load.

## Key Config Fields

| Field | Purpose |
|-------|---------|
| `ats_companies` | Slugs for Greenhouse/Lever/Ashby scraping (~170 Israeli tech companies) |
| `comeet_companies` | Slugs for Comeet scraping |
| `search_queries_extra` | Additional Brave Search queries for Opus |
| `min_score` | Minimum fit score to keep (default: 4) |
| `max_days_old` | Maximum job age in days (default: 30) |

## Location Filtering

Only Israel-based or fully global/worldwide remote jobs are kept. The scorer sets `location_ok: true` only for jobs explicitly in Israel or with no country-specific remote restriction. The filter in `filter_jobs()` hard-rejects anything with `location_ok: false`.

## Bumping the Service Worker

Any change to `docs/index.html` or `docs/sw.js` that should be immediately visible to users requires bumping the cache version in `docs/sw.js`:
```js
const CACHE = 'jobfinder-vN';  // increment N
```
`index.html` and `scored_jobs.json` use network-first fetching, so UI changes are always fresh after the SW updates.

## Deploy Flow

Pipeline commit uses `[skip ci]` → `deploy.yml` is triggered via `workflow_run` (not push), so it fires after the pipeline succeeds even with `[skip ci]` commits. Direct pushes to `docs/**` also trigger deploy.
