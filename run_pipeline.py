#!/usr/bin/env python3
"""
Job Search Pipeline for Yotam Kadishay
Searches for Engineering Manager / Director / VP jobs, scores them,
and updates scored_jobs.json (source of truth) + docs/scored_jobs.json.

Usage:
  python run_pipeline.py          # full run + git push
  python run_pipeline.py --no-push  # full run, no git push
  python run_pipeline.py --score-only  # re-score existing jobs only
"""

import json
import os
import re
import sys
import uuid
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = "config.json"
PROFILE_PATH = "profile.md"
JOBS_PATH = "scored_jobs.json"
DOCS_DIR = "docs"
DOCS_JOBS_PATH = os.path.join(DOCS_DIR, "scored_jobs.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_profile() -> str:
    with open(PROFILE_PATH) as f:
        return f.read()


def load_existing_jobs() -> list[dict]:
    for path in [JOBS_PATH, DOCS_JOBS_PATH]:
        try:
            with open(path) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return []


def extract_json_array(text: str) -> list:
    """Extract the first JSON array from a text blob (handles markdown code blocks)."""
    # Strip markdown code fences first
    stripped = re.sub(r'```(?:json)?\s*', '', text).strip()

    # Try largest array match (greedy) first for complete results
    for src in [stripped, text]:
        match = re.search(r'\[[\s\S]*\]', src)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

    # Last resort: find first '[' to last ']' in entire text
    start = text.find('[')
    end = text.rfind(']')
    if start != -1 and end > start:
        try:
            result = json.loads(text[start:end+1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return []


def extract_json_object(text: str) -> dict:
    """Extract the first JSON object from a text blob."""
    match = re.search(r'\{[\s\S]*?\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Web Search
# ---------------------------------------------------------------------------
def web_search(query: str, count: int = 10) -> str:
    """Execute web search via Brave Search API."""
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        print(f"    [WARN] BRAVE_API_KEY not set — skipping: {query[:60]}")
        return f"[Search unavailable — no BRAVE_API_KEY] Query: {query}"

    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            params={
                "q": query,
                "count": count,
                "freshness": "pm",   # past month
                "text_decorations": False,
                "search_lang": "en",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get("web", {}).get("results", []):
            results.append(
                f"Title: {r.get('title', '')}\n"
                f"URL: {r.get('url', '')}\n"
                f"Snippet: {r.get('description', '')}\n"
                f"Age: {r.get('age', 'unknown')}"
            )
        return "\n\n---\n\n".join(results) if results else "No results found."
    except requests.RequestException as e:
        return f"[Search error: {e}]"


# ---------------------------------------------------------------------------
# Step 1: Search Jobs (Claude Opus + web_search)
# ---------------------------------------------------------------------------
def search_jobs(config: dict, profile: str) -> list[dict]:
    """Use Claude Opus with web_search tool to find current job postings."""
    print("  Using Claude Opus + Brave Search...")
    client = anthropic.Anthropic()
    roles = ", ".join(config["target_roles"])
    locations = ", ".join(config["locations"])

    search_tool = {
        "name": "web_search",
        "description": (
            "Search the web for currently open job postings. "
            "Use specific queries targeting job boards and company career pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"}
            },
            "required": ["query"],
        },
    }

    system = f"""You are a job search assistant helping find senior engineering leadership roles.
Your goal: find CURRENTLY OPEN job postings (last 30 days) that match the target criteria.

Use web_search multiple times with varied queries to find diverse, real postings.
Good search queries include:
- "[role title] site:greenhouse.io"
- "[role title] site:lever.co"
- "[role title] site:ashbyhq.com"
- "[role title] Israel remote [year]"
- Company-specific: "[company] Director Engineering careers"

After searching, compile results into a JSON array ONLY (no other text):
[{{"company":"...","title":"...","location":"...","url":"...","posted":"YYYY-MM-DD","description":"2-3 sentence summary"}}]

Rules:
- Each URL must be a DIRECT link to a specific job posting (not a homepage or search page)
- Only include genuinely open positions (not expired)
- If exact posted date is unknown, estimate based on search result age
- Return 8–15 results"""

    extra_queries = config.get("search_queries_extra", [])
    user_msg = f"""Find CURRENTLY OPEN jobs (posted last 30 days) for: {roles}
Acceptable locations: {locations}, or fully Remote.

Search queries to try:
1. Director Engineering remote AI startup site:greenhouse.io
2. VP Engineering Israel OR remote site:lever.co
3. VP R&D generative AI startup
4. Director Engineering platform infrastructure remote
5. Engineering Manager AI startup Israel
{chr(10).join(f"{i+6}. {q}" for i, q in enumerate(extra_queries))}

After searching (use at least 4-5 searches), return ONLY a JSON array of 8-15 jobs."""

    messages = [{"role": "user", "content": user_msg}]
    final_text = ""

    for _round in range(12):  # max tool-call rounds
        resp = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8192,
            system=system,
            tools=[search_tool],
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if hasattr(block, "text"):
                    final_text += block.text
            print(f"  Opus response length: {len(final_text)} chars")
            print(f"  Opus response preview: {final_text[:300]!r}")
            break

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use" and block.name == "web_search":
                    query = block.input.get("query", "")
                    print(f"    Searching: {query[:70]}")
                    result = web_search(query)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            print(f"  [WARN] Unexpected stop_reason: {resp.stop_reason}")
            break

    # Extract JSON — try direct parse first
    jobs = extract_json_array(final_text)
    print(f"  Direct extraction: {len(jobs)} jobs")

    # Cleanup pass: if Opus returned a narrative instead of JSON, ask Haiku to reformat
    if not jobs and final_text.strip():
        print("  Asking Haiku to reformat Opus response as JSON...")
        try:
            cleanup = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract all job postings from the text below. "
                        "Return ONLY a valid JSON array — no markdown fences, no explanation. "
                        "Start your response with [ and end with ].\n"
                        "Required fields per job: company, title, location, url, posted (YYYY-MM-DD), description.\n\n"
                        f"Text:\n{final_text[:6000]}"
                    )
                }]
            )
            cleanup_text = cleanup.content[0].text.strip()
            print(f"  Haiku cleanup preview: {cleanup_text[:200]!r}")
            jobs = extract_json_array(cleanup_text)
            print(f"  Cleanup pass found {len(jobs)} jobs")
        except Exception as e:
            print(f"  [WARN] Cleanup pass failed: {e}")

    # Last resort: if still no jobs, ask Opus directly for just the JSON
    if not jobs:
        print("  Last resort: asking Opus to output JSON only...")
        try:
            retry = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=4096,
                system="You output only valid JSON arrays. No text before or after the array.",
                messages=[
                    *messages,
                    {"role": "assistant", "content": final_text or "I found several job postings."},
                    {"role": "user", "content": (
                        "Output ONLY the JSON array of job postings you found. "
                        "Format: [{\"company\":\"...\",\"title\":\"...\",\"location\":\"...\","
                        "\"url\":\"...\",\"posted\":\"YYYY-MM-DD\",\"description\":\"...\"},...]\n"
                        "Start with [ immediately."
                    )}
                ]
            )
            retry_text = retry.content[0].text.strip()
            print(f"  Retry preview: {retry_text[:200]!r}")
            jobs = extract_json_array(retry_text)
            print(f"  Retry found {len(jobs)} jobs")
        except Exception as e:
            print(f"  [WARN] Last-resort retry failed: {e}")
    now = utcnow_iso()
    for job in jobs:
        job.setdefault("id", str(uuid.uuid4()))
        job.setdefault("scraped_at", now)
        job.setdefault("source", "pipeline")
        job.setdefault("posted", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    print(f"  Found {len(jobs)} jobs via Opus search")
    return jobs


# ---------------------------------------------------------------------------
# Step 1b: ATS Scraper (Greenhouse / Lever / Ashby)
# ---------------------------------------------------------------------------
def scrape_ats(config: dict) -> list[dict]:
    """Scrape public ATS APIs for open EM/Director/VP roles."""
    companies = config.get("ats_companies", [])
    target_keywords = [r.lower() for r in config["target_roles"]] + [
        "engineering manager", "director", "vp", "head of engineering", "r&d"
    ]
    jobs = []
    now = utcnow_iso()
    today = datetime.now(timezone.utc)
    cutoff = today - timedelta(days=config.get("max_days_old", 30))

    headers = {"User-Agent": "JobSearchBot/1.0"}

    for company in companies:
        try:
            # --- Greenhouse ---
            gh_url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
            r = requests.get(gh_url, headers=headers, timeout=10)
            if r.ok:
                for job in r.json().get("jobs", []):
                    title = job.get("title", "").lower()
                    if not any(kw in title for kw in target_keywords):
                        continue
                    updated = job.get("updated_at", "")
                    posted = updated[:10] if updated else today.strftime("%Y-%m-%d")
                    try:
                        if datetime.strptime(posted, "%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff:
                            continue
                    except ValueError:
                        pass
                    loc = ", ".join(o.get("name", "") for o in job.get("offices", [])) or "Unknown"
                    jobs.append({
                        "id": str(uuid.uuid4()),
                        "company": company.replace("-", " ").title(),
                        "title": job.get("title", ""),
                        "location": loc,
                        "url": job.get("absolute_url", ""),
                        "posted": posted,
                        "description": job.get("content", "")[:500].replace("<[^>]+>", ""),
                        "scraped_at": now,
                        "source": "ats_greenhouse",
                    })
                continue

        except requests.RequestException:
            pass

        try:
            # --- Lever ---
            lv_url = f"https://api.lever.co/v0/postings/{company}?mode=json"
            r = requests.get(lv_url, headers=headers, timeout=10)
            if r.ok:
                for job in r.json():
                    title = job.get("text", "").lower()
                    if not any(kw in title for kw in target_keywords):
                        continue
                    created_ms = job.get("createdAt", 0)
                    created_dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                    if created_dt < cutoff:
                        continue
                    posted = created_dt.strftime("%Y-%m-%d")
                    loc = job.get("categories", {}).get("location", "Unknown")
                    jobs.append({
                        "id": str(uuid.uuid4()),
                        "company": company.replace("-", " ").title(),
                        "title": job.get("text", ""),
                        "location": loc,
                        "url": job.get("hostedUrl", ""),
                        "posted": posted,
                        "description": job.get("descriptionPlain", "")[:500],
                        "scraped_at": now,
                        "source": "ats_lever",
                    })
                continue
        except requests.RequestException:
            pass

        try:
            # --- Ashby ---
            ash_url = "https://jobs.ashbyhq.com/api/non-user-facing/posting-board/job-board/list"
            r = requests.post(
                ash_url,
                json={"organizationHostedJobsPageName": company},
                headers=headers,
                timeout=10,
            )
            if r.ok:
                for job in r.json().get("jobPostings", []):
                    title = job.get("title", "").lower()
                    if not any(kw in title for kw in target_keywords):
                        continue
                    published = job.get("publishedDate", "")[:10] or today.strftime("%Y-%m-%d")
                    try:
                        if datetime.strptime(published, "%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff:
                            continue
                    except ValueError:
                        pass
                    loc = job.get("locationName", "Unknown")
                    slug = job.get("id", "")
                    url = f"https://jobs.ashbyhq.com/{company}/{slug}"
                    jobs.append({
                        "id": str(uuid.uuid4()),
                        "company": company.replace("-", " ").title(),
                        "title": job.get("title", ""),
                        "location": loc,
                        "url": url,
                        "posted": published,
                        "description": job.get("descriptionSocial", "")[:500],
                        "scraped_at": now,
                        "source": "ats_ashby",
                    })
        except requests.RequestException:
            pass

    # --- Comeet (popular Israeli ATS) ---
    for company in config.get("comeet_companies", []):
        try:
            co_url = f"https://www.comeet.com/jobs/{company}/all"
            r = requests.get(co_url, headers={**headers, "Accept": "application/json"}, timeout=10)
            if r.ok:
                data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
                for job in data.get("positions", []):
                    title = job.get("name", "").lower()
                    if not any(kw in title for kw in target_keywords):
                        continue
                    posted = job.get("date_added", today.strftime("%Y-%m-%d"))[:10]
                    loc = job.get("location", {}).get("city", "Israel")
                    slug = job.get("uid", "")
                    url = f"https://www.comeet.com/jobs/{company}/{slug}"
                    jobs.append({
                        "id": str(uuid.uuid4()),
                        "company": company.replace("-", " ").title(),
                        "title": job.get("name", ""),
                        "location": loc,
                        "url": url,
                        "posted": posted,
                        "description": job.get("details", "")[:500],
                        "scraped_at": now,
                        "source": "ats_comeet",
                    })
        except requests.RequestException:
            pass

    print(f"  ATS scraper found {len(jobs)} additional jobs")
    return jobs


# ---------------------------------------------------------------------------
# Step 2: Score Jobs (Claude Haiku)
# ---------------------------------------------------------------------------
def score_jobs(jobs: list[dict], profile: str, config: dict) -> list[dict]:
    """Score jobs using Claude Haiku. Returns jobs with scoring fields added."""
    client = anthropic.Anthropic()
    locations = config["locations"]
    loc_str = ", ".join(locations)
    scored = []

    for i, job in enumerate(jobs):
        print(f"  Scoring {i+1}/{len(jobs)}: {job.get('company','?')} — {job.get('title','?')}")
        prompt = f"""Evaluate this job for the following candidate. Return JSON only, no other text.

## Candidate Profile
{profile}

## Job Being Evaluated
Company: {job.get('company', '')}
Title: {job.get('title', '')}
Location: {job.get('location', '')}
Posted: {job.get('posted', '')}
Description: {job.get('description', '')}

## Acceptable Locations (no relocation needed)
{loc_str}, or fully global remote (e.g. "Remote", "Worldwide", "Distributed" — NOT country-specific remote like "Remote - United States")

## Return JSON (no other text):
{{"fit_score": <integer 1-10>, "score_reason": "<1-2 sentences>", "ai_opener": "<personalized 1-sentence cover letter opener>", "location_ok": <true or false>, "relocation_required": <true or false>}}

Location rules:
- location_ok: true  → Israel, Tel Aviv, or fully global/worldwide remote
- location_ok: false → any other location (specific country/city outside Israel, country-specific remote like "Remote - US", regional like "AMER"/"EMEA")
- relocation_required: true  → location_ok is false BUT it's a real city/country (candidate could relocate)
- relocation_required: false → location_ok is true (no relocation needed)
- Do NOT set fit_score to 0 for relocation jobs — score on merit; the UI will label them "RELOCATION"

Fit score rules:
- Score 8-10: strong match on level + domain (AI, infra, platform) + company stage
- Score 5-7: good match but partial domain or stage mismatch
- Score 1-4: weak match (wrong level, legacy tech, wrong domain)"""

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            result = extract_json_object(resp.content[0].text)
            if result:
                job.update(result)
            else:
                job.update({"fit_score": 5, "score_reason": "Could not parse score", "ai_opener": "", "location_ok": True, "relocation_required": False})
        except Exception as e:
            print(f"    [WARN] Scoring failed: {e}")
            job.update({"fit_score": 5, "score_reason": "Scoring error", "ai_opener": "", "location_ok": True, "relocation_required": False})

        scored.append(job)

    return scored


# ---------------------------------------------------------------------------
# Step 3: Filter
# ---------------------------------------------------------------------------
def filter_jobs(jobs: list[dict], config: dict) -> list[dict]:
    """Filter by score, location, and recency."""
    min_score = config.get("min_score", 4)
    max_days = config.get("max_days_old", 30)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)

    passed, rejected = [], []
    for job in jobs:
        score = job.get("fit_score", 0)
        if score < min_score:
            rejected.append(f"{job.get('company','?')}: score {score} < {min_score}")
            continue
        # Allow relocation jobs through; only hard-reject if location_ok=False AND relocation_required=False
        if not job.get("location_ok", True) and not job.get("relocation_required", False):
            rejected.append(f"{job.get('company','?')}: location not OK and no relocation")
            continue
        posted_str = job.get("posted", "")
        if posted_str:
            try:
                posted_dt = datetime.strptime(posted_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if posted_dt < cutoff:
                    rejected.append(f"{job.get('company','?')}: posted {posted_str} too old")
                    continue
            except ValueError:
                pass
        passed.append(job)

    if rejected:
        print(f"  Filtered out {len(rejected)} jobs:")
        for r in rejected[:10]:
            print(f"    - {r}")
        if len(rejected) > 10:
            print(f"    ... and {len(rejected)-10} more")

    return passed


# ---------------------------------------------------------------------------
# Step 4: Merge
# ---------------------------------------------------------------------------
def merge_jobs(new_jobs: list[dict], existing_jobs: list[dict]) -> list[dict]:
    """
    Merge strategy:
    - NEVER delete existing jobs (preserves manual + initial_status entries)
    - Add new jobs not already present (dedup by URL, then company+title)
    - New pipeline jobs get initial_status = 'saved' if not set
    """
    existing_urls = {j.get("url", "").strip().rstrip("/") for j in existing_jobs if j.get("url")}
    existing_keys = {
        f"{j.get('company','').lower()}|{j.get('title','').lower()}"
        for j in existing_jobs
    }

    merged = list(existing_jobs)
    added = 0

    for job in new_jobs:
        url = job.get("url", "").strip().rstrip("/")
        key = f"{job.get('company','').lower()}|{job.get('title','').lower()}"

        if (url and url in existing_urls) or key in existing_keys:
            continue

        # New job — set defaults
        job.setdefault("initial_status", "saved")
        merged.append(job)
        if url:
            existing_urls.add(url)
        existing_keys.add(key)
        added += 1

    print(f"  Merged: +{added} new, {len(existing_jobs)} existing preserved → {len(merged)} total")

    # Sort: manual/high-score first, then by score desc, then posted desc
    def sort_key(j):
        source = j.get("source", "pipeline")
        manual_first = 0 if source == "manual" else 1
        score = -(j.get("fit_score") or 0)
        try:
            posted_ts = -datetime.strptime(j["posted"], "%Y-%m-%d").timestamp()
        except (ValueError, KeyError):
            posted_ts = 0
        return (manual_first, score, posted_ts)

    merged.sort(key=sort_key)
    return merged


# ---------------------------------------------------------------------------
# Step 5: Save
# ---------------------------------------------------------------------------
def save_jobs(jobs: list[dict]) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)
    data = json.dumps(jobs, indent=2, ensure_ascii=False)
    with open(JOBS_PATH, "w", encoding="utf-8") as f:
        f.write(data)
    with open(DOCS_JOBS_PATH, "w", encoding="utf-8") as f:
        f.write(data)
    print(f"  Saved {len(jobs)} jobs → {JOBS_PATH} + {DOCS_JOBS_PATH}")


# ---------------------------------------------------------------------------
# Step 6: Git Push
# ---------------------------------------------------------------------------
def git_push() -> None:
    try:
        subprocess.run(["git", "config", "user.name", "Job Pipeline Bot"], check=True)
        subprocess.run(["git", "config", "user.email", "pipeline@github-actions.com"], check=True)
        subprocess.run(["git", "add", JOBS_PATH, DOCS_JOBS_PATH], check=True)

        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
        if diff.returncode != 0:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            msg = f"chore: update job listings {ts} [skip ci]"
            subprocess.run(["git", "commit", "-m", msg], check=True)
            subprocess.run(["git", "push"], check=True)
            print("  Pushed to GitHub Pages")
        else:
            print("  No changes to push")
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] Git push failed: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("  Job Search Pipeline")
    print(f"  {utcnow_iso()}")
    print("=" * 60)

    config = load_config()
    profile = load_profile()
    existing_jobs = load_existing_jobs()
    print(f"\nLoaded {len(existing_jobs)} existing jobs from {JOBS_PATH}")

    score_only = "--score-only" in sys.argv
    no_push = "--no-push" in sys.argv or score_only

    if score_only:
        print("\n[SCORE-ONLY MODE] Re-scoring existing unscored jobs...")
        unscored = [j for j in existing_jobs if "fit_score" not in j]
        if not unscored:
            print("  All jobs already scored.")
            return
        scored = score_jobs(unscored, profile, config)
        scored_ids = {j["id"] for j in scored}
        merged = [j for j in existing_jobs if j.get("id") not in scored_ids] + scored
        save_jobs(merged)
        return

    # Step 1: Search
    print("\n[1/5] Searching for jobs (Claude Opus + Brave Search)...")
    new_jobs = search_jobs(config, profile)

    # Step 1b: ATS scrape
    print("\n[1b/5] Scraping ATS boards (Greenhouse / Lever / Ashby)...")
    ats_jobs = scrape_ats(config)
    all_new = new_jobs + ats_jobs

    # Step 2: Score
    print(f"\n[2/5] Scoring {len(all_new)} new jobs (Claude Haiku)...")
    scored = score_jobs(all_new, profile, config)

    # Step 3: Filter
    print("\n[3/5] Filtering jobs...")
    filtered = filter_jobs(scored, config)
    print(f"  {len(filtered)}/{len(scored)} passed filters")

    # Step 4: Merge
    print("\n[4/5] Merging with existing jobs...")
    merged = merge_jobs(filtered, existing_jobs)

    # Step 5: Save
    print("\n[5/5] Saving...")
    save_jobs(merged)

    # Step 6: Push
    if not no_push:
        print("\n[6/6] Git push...")
        git_push()

    print(f"\n{'='*60}")
    print(f"  Done! Total jobs in tracker: {len(merged)}")
    print(f"  New high-quality jobs added this run: {len(filtered)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
