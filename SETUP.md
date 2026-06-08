# Job Finder — Setup Guide

## 1. Create GitHub Repo

```bash
cd /Users/kadishay/Code/finder
git init
git add .
git commit -m "feat: initial job finder setup"
gh repo create job-finder --public --push --source=.
```

## 2. Add GitHub Secrets

In your repo → **Settings → Secrets and variables → Actions**, add:

| Secret | Value | Required? |
|--------|-------|-----------|
| `ANTHROPIC_API_KEY` | `sk-ant-…` | ✅ Required |
| `BRAVE_API_KEY` | From [brave.com/search/api](https://brave.com/search/api) | Recommended (free tier available) |

## 3. Enable GitHub Pages

Repo → **Settings → Pages** → Source: **GitHub Actions**

## 4. Run Pipeline Manually (First Time)

Repo → **Actions → Job Search Pipeline → Run workflow**

Or locally:
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export BRAVE_API_KEY=...
python run_pipeline.py --no-push
```

## 5. Open the Tracker

After pipeline runs and Pages deploys:
`https://<your-username>.github.io/job-finder/`

In Settings (⚙️), add your **Anthropic API key** — needed for AI features in the browser.

## 6. Schedule

Pipeline runs automatically **3× daily, Sunday–Thursday** (07:00, 13:00, 19:00 UTC).
Each run searches for new jobs, scores them, and pushes updates.

---

## Architecture Summary

```
run_pipeline.py
  ├── search_jobs()      Claude Opus + Brave Search → real job postings
  ├── scrape_ats()       Greenhouse / Lever / Ashby public APIs
  ├── score_jobs()       Claude Haiku → fit score 1-10 + reasoning
  ├── filter_jobs()      score ≥ 4, location OK, posted ≤ 30 days
  ├── merge_jobs()       never deletes existing / manual jobs
  └── git push           updates docs/scored_jobs.json → GitHub Pages

docs/index.html          Material 3 PWA tracker
  ├── loads scored_jobs.json from GitHub Pages
  ├── state in localStorage (status, notes, interview rounds)
  └── AI buttons → direct Anthropic API calls from browser
```

## Customization

Edit `config.json` to change:
- `target_roles` — job titles to search for
- `locations` — acceptable locations
- `min_score` — minimum fit score (default: 4)
- `max_days_old` — how recent jobs must be (default: 30)
- `ats_companies` — companies to check on ATS boards

Edit `profile.md` to update your profile for scoring.
