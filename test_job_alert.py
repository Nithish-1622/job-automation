"""
Tests job_alert.py (company career-page scraper).
Returns opportunities posted in the last 7 days. Sends no email, does not touch seen.db.

Run:  python test_job_alert.py
"""
import os, sys, re, json, tempfile, datetime as dt

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# job_alert.py opens seen.db at import time, so import it from a temp folder
# to keep the real seen.db untouched.
os.chdir(tempfile.mkdtemp())
import job_alert as core

LOOKBACK_DAYS = 7
OUTPUT_FILE = os.path.join(REPO, "test_job_alert_output.json")


def parse_posted(value, today):
    """Turn the different date formats used by each career site into a date."""
    if value in (None, "", "None"):
        return None
    s = str(value).strip()

    if re.fullmatch(r"\d{13}", s):                       # Lever: epoch milliseconds
        return dt.datetime.fromtimestamp(int(s) / 1000, dt.timezone.utc).date()
    if re.fullmatch(r"\d{10}", s):                       # epoch seconds
        return dt.datetime.fromtimestamp(int(s), dt.timezone.utc).date()

    m = re.search(r"(\d+)\+?\s+Days?\s+Ago", s, re.I)    # Workday: "Posted 3 Days Ago"
    if m:
        return today - dt.timedelta(days=int(m.group(1)))
    if re.search(r"today|just posted", s, re.I):
        return today
    if re.search(r"yesterday", s, re.I):
        return today - dt.timedelta(days=1)

    for fmt in ("%B %d, %Y", "%b %d, %Y"):              # Amazon: "November 10, 2026"
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    try:                                                 # ISO: "2026-11-18T10:00:00Z"
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def run_offline():
    today = dt.date.today()
    cases = {
        "2026-11-18T10:00:00Z": today - dt.timedelta(days=2),
        str(int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).timestamp() * 1000)):
            today - dt.timedelta(days=3),
        "Posted 5 Days Ago": today - dt.timedelta(days=5),
        "Posted Today": today,
        "November 10, 2026": dt.date(2026, 11, 10),
        "garbage": None,
    }
    for raw, expected in cases.items():
        got = parse_posted(raw, today)
        status = "ok" if got == expected or expected is None and got is None else "MISMATCH"
        print(f"{status:9} {raw!r:30} -> {got}")
    print("Offline date parsing check finished.\n")


def run_live():
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=LOOKBACK_DAYS)

    jobs, per_company = [], {}
    for c in core.COMPANIES:
        fetched = core.safe_fetch(c)
        per_company[c["name"]] = len(fetched)
        jobs += fetched

    # Same relevance filter the real script uses (roles, India, batch)
    relevant, seen_ids = [], set()
    for j in jobs:
        if j["id"] in seen_ids or not core.target(j):
            continue
        seen_ids.add(j["id"])
        j["posted_date"] = parse_posted(j["posted"], today)
        relevant.append(j)

    in_window = [j for j in relevant
                 if j["posted_date"] and cutoff <= j["posted_date"] <= today]
    undated = [j for j in relevant if j["posted_date"] is None]

    print(f"Window: {cutoff} to {today}")
    print("Fetched per company:", per_company)
    print(f"Relevant: {len(relevant)}  |  posted in last {LOOKBACK_DAYS} days: "
          f"{len(in_window)}  |  date unknown: {len(undated)}\n")

    for j in sorted(in_window, key=lambda x: x["posted_date"], reverse=True):
        print(core.format_job(j) + f"Posted: {j['posted_date']}\n")

    if undated:
        print("Date unknown (not counted, check manually):")
        for j in undated:
            print(f"  - {j['company']}: {j['title']} -> {j['url']}")

    assert all(cutoff <= j["posted_date"] <= today for j in in_window), "window check failed"

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(in_window, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nSaved {OUTPUT_FILE}")


if __name__ == "__main__":
    if "--offline" in sys.argv:
        run_offline()
    else:
        run_live()
