import os, re, hashlib, sqlite3, smtplib, urllib.parse
from email.mime.text import MIMEText
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (placement-alert; contact: you@example.com)"}
TIMEOUT = 30
MAX_PER_RUN = 60  # caps the first run so the coordinator isn't flooded

# ---- Targets: VERIFY each token/URL by opening it in a browser first ------
COMPANIES = [
    {"name": "Razorpay", "type": "greenhouse", "token": "razorpay"},
    {"name": "Postman",  "type": "greenhouse", "token": "postman"},
    {"name": "Amazon",   "type": "amazon"},
    # Workday tenants (confirm tenant, wd number, and site name):
    # {"name": "SomeMNC", "type": "workday", "tenant": "somemnc", "wd": 5, "site": "External"},
    # Custom career page (inspect the page to find the right selector):
    # {"name": "SomeCo", "type": "html", "url": "https://example.com/careers",
    #  "selector": "a[href*='job']"},
]

ROLE_RE = re.compile(
    r"\b(intern|internship|fresher|graduate|trainee|software engineer|"
    r"software developer|sde|ai engineer|ml engineer|machine learning|"
    r"associate engineer|junior)\b", re.I)
EXCLUDE_RE = re.compile(
    r"\b(senior|sr|lead|principal|staff|manager|architect|director)\b|\d\+?\s*years", re.I)
INDIA_RE = re.compile(
    r"india|bengaluru|bangalore|hyderabad|pune|chennai|gurugram|gurgaon|"
    r"noida|delhi|mumbai|kolkata|remote", re.I)
BATCH_RE = re.compile(
    r"(batch|graduat\w*|passing[- ]out)\D{0,25}(2028|2029|2030)|"
    r"(2028|2029|2030)\s*(batch|graduates?)", re.I)
CTC_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s?[\d.,]+(?:\s*(?:-|to)\s*[\d.,]+)?\s*(?:LPA|lpa|lakhs?|lacs?|per annum)?"
    r"|[\d.,]+\s*(?:-|to)\s*[\d.,]+\s*(?:LPA|lpa|lakhs?|lacs?)", re.I)

def mk(company, title, location, url, posted="", desc=""):
    return {
        "company": company,
        "title": title.strip(),
        "location": location or "",
        "url": url,
        "posted": str(posted),
        "desc": desc or "",
        "id": hashlib.sha1(f"{company}|{title}|{url}".encode()).hexdigest(),
    }

# ---- Fetchers ---------------------------------------------------------------
def fetch_greenhouse(c):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs",
                     params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [mk(c["name"], j["title"], j["location"]["name"], j["absolute_url"],
               j.get("updated_at", ""), j.get("content", ""))
            for j in r.json().get("jobs", [])]

def fetch_lever(c):
    r = requests.get(f"https://api.lever.co/v0/postings/{c['token']}",
                     params={"mode": "json"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [mk(c["name"], j["text"], j.get("categories", {}).get("location", ""),
               j["hostedUrl"], j.get("createdAt", ""))
            for j in r.json()]

def fetch_ashby(c):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}",
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [mk(c["name"], j["title"], j.get("location", ""), j["jobUrl"],
               j.get("publishedAt", ""), j.get("descriptionPlain", ""))
            for j in r.json().get("jobs", [])]

def fetch_workday(c):
    base = f"https://{c['tenant']}.wd{c['wd']}.myworkdayjobs.com"
    out = []
    for q in ["intern", "fresher", "graduate"]:
        r = requests.post(f"{base}/wday/cxs/{c['tenant']}/{c['site']}/jobs",
                          json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q},
                          headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        for p in r.json().get("jobPostings", []):
            out.append(mk(c["name"], p["title"], p.get("locationsText", ""),
                          f"{base}/en-US/{c['site']}{p['externalPath']}", p.get("postedOn", "")))
    return out

def fetch_amazon(c):
    r = requests.get("https://www.amazon.jobs/en/search.json",
                     params={"base_query": "intern fresher software", "loc_query": "India",
                             "result_limit": 50, "sort": "recent"},
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [mk("Amazon", j["title"], j.get("location", ""),
               "https://www.amazon.jobs" + j["job_path"], j.get("posted_date", ""),
               j.get("description_short", ""))
            for j in r.json().get("jobs", [])]

def fetch_html(c):
    r = requests.get(c["url"], headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return [mk(c["name"], a.get_text(" ", strip=True), "India",
               urllib.parse.urljoin(c["url"], a.get("href", "")))
            for a in soup.select(c["selector"]) if a.get_text(strip=True)]

FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday, "amazon": fetch_amazon, "html": fetch_html}

def safe_fetch(c):
    try:
        return FETCHERS[c["type"]](c)
    except Exception as e:  # one broken source must not stop the run
        print(f"[warn] {c['name']} failed: {e}")
        return []

# ---- Filter -----------------------------------------------------------------
def extract_ctc(text):
    m = CTC_RE.search(text or "")
    return m.group(0).strip() if m else "Not disclosed"

def target(job):
    text = f"{job['title']} {job['desc']}"
    if EXCLUDE_RE.search(job["title"]):
        return False
    if job["location"] and not INDIA_RE.search(job["location"]):
        return False
    m = BATCH_RE.search(text)
    job["batch"] = (m.group(2) or m.group(3)) if m else "unspecified"
    job["ctc"] = extract_ctc(text)
    return bool(m or ROLE_RE.search(job["title"]))

# ---- Dedupe -----------------------------------------------------------------
db = sqlite3.connect("seen.db")
db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")

def is_new(job):
    return db.execute("SELECT 1 FROM seen WHERE id=?", (job["id"],)).fetchone() is None

# ---- Notify (email only) ----------------------------------------------------
def format_job(j):
    return (f"Company Name - {j['company']}\n"
            f"Expected CTC - {j['ctc']}\n"
            f"Role - {j['title']}\n"
            f"Apply Link - {j['url']}\n")


def send_email(blocks):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    coord = os.environ["COORD_EMAIL"]
    owner = os.environ.get("MAIL_TO", user)  # your own copy

    # Send to the coordinator, with you in Cc. Skip the duplicate if they're the same address.
    recipients = list(dict.fromkeys([coord, owner]))

    body = f"{len(blocks)} new fresher/intern software roles\n\n" + "\n".join(blocks)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Placement] {len(blocks)} new fresher/intern roles"
    msg["From"] = user
    msg["To"] = coord
    if owner != coord:
        msg["Cc"] = owner

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())

# ---- Main -------------------------------------------------------------------
def main():
    jobs = []
    for c in COMPANIES:
        jobs += safe_fetch(c)

    seen_ids, fresh = set(), []
    for j in jobs:
        if j["id"] in seen_ids or not target(j) or not is_new(j):
            continue
        seen_ids.add(j["id"])
        fresh.append(j)

    fresh.sort(key=lambda j: (j["batch"] == "unspecified", j["batch"], j["company"]))
    fresh = fresh[:MAX_PER_RUN]
    print(f"Fetched {len(jobs)}, new & relevant {len(fresh)}")

    if fresh:
        send_email([format_job(j) for j in fresh])  # if this raises, nothing is marked seen
        db.executemany("INSERT OR IGNORE INTO seen (id) VALUES (?)",
                       [(j["id"],) for j in fresh])
        db.commit()

if __name__ == "__main__":
    main()
