from groq import Groq

def run_live():
    client = Groq()                              # reads GROQ_API_KEY
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
