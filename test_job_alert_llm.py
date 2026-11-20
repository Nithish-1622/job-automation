"""
Tests job_alert_llm.py (Gemini + Google Search).
Returns validated opportunities posted in the last 7 days. Sends no email, does not touch seen_llm.db.

Needs GEMINI_API_KEY for the live run.
Run:  python test_job_alert_llm.py            (live)
      python test_job_alert_llm.py --offline  (no API calls)
"""
import os, sys, json, datetime as dt

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import job_alert_llm as core

LOOKBACK_DAYS = 7
OUTPUT_FILE = os.path.join(REPO, "test_job_alert_llm_output.json")


def run_offline():
    today = dt.date.today()
    sample = [
        {"company": "Razorpay", "role": "SWE Intern (2029 batch)", "type": "internship",
         "expected_ctc": "Not disclosed", "batch": "2029",
         "posted_date": (today - dt.timedelta(days=2)).isoformat(),
         "apply_url": "https://razorpay.com/careers/example-1"},
        {"company": "OldCo", "role": "Graduate Engineer", "type": "job",
         "expected_ctc": "₹10 LPA", "batch": "2028",
         "posted_date": (today - dt.timedelta(days=30)).isoformat(),
         "apply_url": "https://oldco.example/job/2"},
        {"company": "NoDate", "role": "Hackathon", "type": "hackathon",
         "expected_ctc": "Not disclosed", "batch": "Not stated",
         "posted_date": None, "apply_url": "https://unstop.com/example-3"},
    ]
    kept = []
    for raw in sample:
        job = core.normalize(raw, raw["type"])
        keep = bool(job["url"]) and core.in_window(job["posted"])
        print(f"{'keep' if keep else 'drop':5} {job['company']:10} posted={job['posted']}")
        if keep:
            kept.append(job)

    assert len(kept) == 2, "30-day-old job should be dropped; undated job kept"
    print("\n" + core.format_job(kept[0]))
    print("Offline checks passed.")


def run_live():
    from google import genai
    client = genai.Client()                      # reads GEMINI_API_KEY
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=LOOKBACK_DAYS)

    jobs = core.collect(client)                  # search + date filter + URL check
    dated = [j for j in jobs if j["posted"]]
    undated = [j for j in jobs if not j["posted"]]
    outside = [j for j in dated if str(j["posted"])[:10] < cutoff.isoformat()]

    print(f"Window: {cutoff} to {today}")
    print(f"Validated: {len(jobs)}  |  dated: {len(dated)}  |  undated: {len(undated)}\n")

    for j in jobs:
        print(core.format_job(j) + f"Posted: {j['posted'] or 'unknown'}\n")

    assert not outside, f"{len(outside)} job(s) outside the window slipped through"

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    print(f"Saved {OUTPUT_FILE}")


if __name__ == "__main__":
    if "--offline" in sys.argv:
        run_offline()
    else:
        run_live()
