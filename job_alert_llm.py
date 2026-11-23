import os, re, json, time, sqlite3, smtplib, hashlib, datetime as dt
from email.mime.text import MIMEText
from urllib.parse import urlparse
import requests
from groq import Groq
import job_alert as direct   # Level 1: company career-board APIs (deterministic)

MODEL = "openai/gpt-oss-120b"   # verified with browser_search on your account
LOOKBACK_DAYS = 7
MAX_PER_RUN = 60
MAX_PER_DOMAIN = 8              # stops one site (e.g. Unstop) from filling the email
TIMEOUT = 20
SLEEP_BETWEEN = 5
HEADERS = {"User-Agent": "Mozilla/5.0 (placement-alert)"}
DB_PATH = "seen_llm.db"         # separate from seen.db used by job_alert.py

# Level 2 to 4: each level searches only its named sources
LEVELS = {
    "L2": "Work at a Startup (workatastartup.com, Y Combinator) and career pages of "
          "Y Combinator and well-known Indian startups",
    "L3": "Internshala, Cutshort, Instahyre and Unstop job listings",
    "L4": "MLH (mlh.io), Devfolio, Hackathon.io and Devpost hackathons that offer a "
          "pre-placement offer, job or internship",
}

PROMPT = """Search the web for software engineer, AI/ML and developer roles in India
for fresher, intern, or 2028/2029/2030 batch graduate opportunities.

Use ONLY these sources: {sources}
Today is {today}. Only include items posted within the last {days} days.

Return ONLY a JSON array, with no prose and no markdown. Each item must have these keys:
- "company": organization name
- "role": job or event title
- "type": one of "job", "internship", "hackathon", "campus"
- "expected_ctc": salary or stipend as written in the source, or "Not disclosed"
- "batch": eligible graduation batch if stated (e.g. "2029"), else "Not stated"
- "posted_date": YYYY-MM-DD if known, else null
- "apply_url": direct URL to the posting

Return at most 10 items. If you find nothing, return []. Never invent URLs or details."""

CITATION_RE = re.compile(r"【[^】]*】")

# ---- Parse model output -----------------------------------------------------
def parse_json_array(text):
    text = CITATION_RE.sub("", text or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        if start == -1:
            return []
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]

# ---- LLM web search (Level 2 to 4) ------------------------------------------
def ask_llm(client, sources):
    prompt = PROMPT.format(sources=sources, today=dt.date.today().isoformat(), days=LOOKBACK_DAYS)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "browser_search"}],
        reasoning_effort="low",
        max_completion_tokens=4096,
    )
    return parse_json_array(resp.choices[0].message.content or "")

# ---- Level 1: company career boards (direct, from job_alert.py) -------------
def to_iso_date(v):
    s = str(v or "").strip()
    if re.fullmatch(r"\d{13}", s):                       # Lever epoch milliseconds
        return dt.datetime.fromtimestamp(int(s) / 1000, dt.timezone.utc).date().isoformat()
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else None

def collect_direct():
    out = []
    for c in direct.COMPANIES:
        for j in direct.safe_fetch(c):
            if not direct.target(j):
                continue
            out.append({
                "company": j["company"],
                "role": j["title"],
                "ctc": j.get("ctc", "Not disclosed"),
                "batch": j.get("batch", "Not stated"),
                "posted": to_iso_date(j.get("posted")),
                "url": j["url"],
                "type": "job",
                "tier": "L1",
                "id": "",
            })
    return out

# ---- Normalize, filter, validate --------------------------------------------
def normalize(raw, tier):
    return {
        "company": raw.get("company") or "Unknown",
        "role": raw.get("role") or "Unknown",
        "ctc": raw.get("expected_ctc") or "Not disclosed",
        "batch": raw.get("batch") or "Not stated",
        "posted": raw.get("posted_date"),
        "url": (raw.get("apply_url") or "").strip(),
        "type": raw.get("type") or "job",
        "tier": tier,
        "id": "",  # set in collect() after the URL is checked
    }

def in_window(posted, days=LOOKBACK_DAYS):
    """Keep jobs posted within the window. Unknown dates are kept, then URL-checked."""
    if not posted:
        return True
    try:
        d = dt.date.fromisoformat(str(posted)[:10])
    except ValueError:
        return True
    return dt.date.today() - d <= dt.timedelta(days=days)

def check_url(url):
    """Follow redirects and return the final URL if the page is reachable, else None."""
    if not url.startswith("https://"):
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                         allow_redirects=True, stream=True)
        final, status = r.url, r.status_code
        r.close()
    except requests.RequestException:
        return None
    # 403/429 usually means bot protection on a real page, so keep those
    if status < 400 or status in (403, 429):
        return final
    return None

def cap_per_domain(jobs):
    """Limit LLM results per site. Level 1 company boards are never capped."""
    counts, kept = {}, []
    for j in jobs:
        if j["tier"] == "L1":
            kept.append(j)
            continue
        d = urlparse(j["url"]).netloc.lower()
        if counts.get(d, 0) >= MAX_PER_DOMAIN:
            continue
        counts[d] = counts.get(d, 0) + 1
        kept.append(j)
    return kept

def collect(client):
    raw = collect_direct()                      # Level 1 first, so it fills the list first
    print(f"L1 direct: {len(raw)} relevant listings")

    for i, (tier, sources) in enumerate(LEVELS.items()):
        if i:
            time.sleep(SLEEP_BETWEEN)
        try:
            for r in ask_llm(client, sources):
                j = normalize(r, tier)
                if j["url"] and in_window(j["posted"]):
                    raw.append(j)
        except Exception as e:  # one failing level must not stop the run
            print(f"[warn] {tier} search failed: {e}")

    out, seen = [], set()
    for j in raw:
        final = check_url(j["url"])
        if not final:
            continue
        j["url"] = final
        j["id"] = hashlib.sha1(final.lower().rstrip("/").encode()).hexdigest()
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        out.append(j)
    return cap_per_domain(out)

# ---- Notify -----------------------------------------------------------------
def format_job(j):
    return (f"Company Name - {j['company']}\n"
            f"Expected CTC - {j['ctc']}\n"
            f"Role - {j['role']}\n"
            f"Apply Link - {j['url']}\n")

TEST_MODE = os.environ.get("TEST_MODE") == "1"

def send_email(blocks, tier_counts, test=False):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    owner = os.environ.get("MAIL_TO", user)

    if test:
        to, cc, recipients = owner, None, [owner]
        prefix = "[TEST] "
    else:
        coord = os.environ["COORD_EMAIL"]
        to, cc = coord, (owner if owner != coord else None)
        recipients = list(dict.fromkeys([coord, owner]))
        prefix = ""

    summary = ", ".join(f"{t}: {n}" for t, n in tier_counts.items())
    body = (f"{len(blocks)} opportunities from the last {LOOKBACK_DAYS} days\n"
            f"({summary})\n\n" + "\n".join(blocks))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"{prefix}[Placement] {len(blocks)} opportunities"
    msg["From"] = user
    msg["To"] = to
    if cc:
        msg["Cc"] = cc

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())

# ---- Main -------------------------------------------------------------------
def main():
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")

    jobs = collect(client)
    if TEST_MODE:
        fresh = jobs                       # every 7-day result, ignoring history
    else:
        fresh = [j for j in jobs
                 if not db.execute("SELECT 1 FROM seen WHERE id=?", (j["id"],)).fetchone()]
    fresh = fresh[:MAX_PER_RUN]

    tier_counts = {}
    for j in fresh:
        tier_counts[j["tier"]] = tier_counts.get(j["tier"], 0) + 1
    print(f"Mode: {'TEST' if TEST_MODE else 'LIVE'} | Validated {len(jobs)}, "
          f"to send {len(fresh)}, by tier: {tier_counts}")

    if fresh:
        send_email([format_job(j) for j in fresh], tier_counts, test=TEST_MODE)
        if not TEST_MODE:                  # only live runs record what was sent
            db.executemany("INSERT OR IGNORE INTO seen (id) VALUES (?)",
                           [(j["id"],) for j in fresh])
            db.commit()
    db.close()


if __name__ == "__main__":
    main()
