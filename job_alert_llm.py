import os, re, json, time, sqlite3, smtplib, hashlib, datetime as dt
from email.mime.text import MIMEText
import requests
from groq import Groq

MODEL = "groq/compound"      # Groq model with built-in web search; verify at console.groq.com/docs/models
LOOKBACK_DAYS = 7
MAX_PER_RUN = 60
TIMEOUT = 20
SLEEP_BETWEEN = 5            # seconds between categories, to respect free-tier per-minute limits
HEADERS = {"User-Agent": "Mozilla/5.0 (placement-alert)"}
DB_PATH = "seen_llm.db"      # separate from seen.db used by job_alert.py

CATEGORIES = {
    "jobs": "full-time fresher and graduate software engineer, AI/ML engineer and developer "
            "jobs in India offered by top MNCs and product companies for 2028, 2029 or 2030 batch graduates",
    "internships": "software developer, AI/ML and data internships in India at top companies "
                   "and startups, paid or with a stipend",
    "hackathons": "hackathons and coding challenges in India listed on Unstop, Devfolio, "
                  "Hackathon.io or Devpost that offer a job, internship or pre-placement offer",
    "campus": "campus recruitment drives and off-campus hiring for 2028, 2029 or 2030 batch in India",
}

PROMPT = """Search the web for {topic}.
Today is {today}. Only include opportunities posted within the last {days} days.

Prefer official company career pages and platform listings (Unstop, Devfolio, company
careers pages). Apply links must open the actual posting, not an ad or a redirect page.

Return ONLY a JSON array, with no prose and no markdown. Each item must have these keys:
- "company": organization name
- "role": job or event title
- "type": one of "job", "internship", "hackathon", "campus"
- "expected_ctc": salary or stipend as written in the source, or "Not disclosed"
- "batch": eligible graduation batch if stated (e.g. "2029"), else "Not stated"
- "posted_date": YYYY-MM-DD if known, else null
- "apply_url": direct URL to the posting

If you find nothing, return []. Never invent URLs or details. Omit anything you did not see in a search result."""

# ---- Parse model output -----------------------------------------------------
def parse_json_array(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)]

# ---- LLM web search (Groq compound model) -----------------------------------
def ask_llm(client, topic):
    prompt = PROMPT.format(topic=topic, today=dt.date.today().isoformat(), days=LOOKBACK_DAYS)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return parse_json_array(resp.choices[0].message.content or "")

# ---- Normalize, filter, validate --------------------------------------------
def normalize(raw, category):
    return {
        "company": raw.get("company") or "Unknown",
        "role": raw.get("role") or "Unknown",
        "ctc": raw.get("expected_ctc") or "Not disclosed",
        "batch": raw.get("batch") or "Not stated",
        "posted": raw.get("posted_date"),
        "url": (raw.get("apply_url") or "").strip(),
        "type": raw.get("type") or category,
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

def collect(client, categories=CATEGORIES):
    raw_jobs = []
    for i, (name, topic) in enumerate(categories.items()):
        if i:
            time.sleep(SLEEP_BETWEEN)
        try:
            for raw in ask_llm(client, topic):
                job = normalize(raw, name)
                if job["url"] and in_window(job["posted"]):
                    raw_jobs.append(job)
        except Exception as e:  # one failing category must not stop the run
            print(f"[warn] {name} search failed: {e}")

    seen, out = set(), []
    for j in raw_jobs:
        final = check_url(j["url"])
        if not final:
            continue
        j["url"] = final
        j["id"] = hashlib.sha1(final.lower().rstrip("/").encode()).hexdigest()
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        out.append(j)
    return out

# ---- Notify -----------------------------------------------------------------
def format_job(j):
    return (f"Company Name - {j['company']}\n"
            f"Expected CTC - {j['ctc']}\n"
            f"Role - {j['role']}\n"
            f"Apply Link - {j['url']}\n")

def send_email(blocks):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    coord = os.environ["COORD_EMAIL"]
    owner = os.environ.get("MAIL_TO", user)

    recipients = list(dict.fromkeys([coord, owner]))
    body = f"{len(blocks)} new opportunities from the last {LOOKBACK_DAYS} days\n\n" + "\n".join(blocks)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Placement] {len(blocks)} new opportunities"
    msg["From"] = user
    msg["To"] = coord
    if owner != coord:
        msg["Cc"] = owner

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())

# ---- Main -------------------------------------------------------------------
def main():
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")

    jobs = collect(client)
    fresh = [j for j in jobs
             if not db.execute("SELECT 1 FROM seen WHERE id=?", (j["id"],)).fetchone()]
    fresh = fresh[:MAX_PER_RUN]
    print(f"Validated {len(jobs)}, new {len(fresh)}")

    if fresh:
        send_email([format_job(j) for j in fresh])  # if this raises, nothing is marked seen
        db.executemany("INSERT OR IGNORE INTO seen (id) VALUES (?)",
                       [(j["id"],) for j in fresh])
        db.commit()
    db.close()

if __name__ == "__main__":
    main()
