import os, sqlite3, smtplib, requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---- Config ----------------------------------------------------------------
ADZUNA_ID  = os.environ["ADZUNA_APP_ID"]
ADZUNA_KEY = os.environ["ADZUNA_APP_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]   # Gmail App Password, not your login
MAIL_TO    = os.environ.get("MAIL_TO", GMAIL_USER)

ADZUNA_QUERIES = [
    "fresher software developer",
    "software engineer intern",
    "ai engineer fresher",
    "machine learning intern",
    "graduate trainee software",
]
# Company job-board tokens: check boards.greenhouse.io/<token> or jobs.lever.co/<token>
GREENHOUSE_BOARDS = ["razorpay", "cred", "postman"]
LEVER_COMPANIES   = []  # add tokens here if a company uses Lever

INCLUDE  = ["fresher", "intern", "graduate", "trainee", "junior", "entry level",
            "software engineer", "software developer", "ai engineer",
            "ml engineer", "machine learning", "data scientist", "associate engineer"]
EXCLUDE  = ["senior", "sr.", "lead", "principal", "staff", "manager",
            "architect", "director", "3+ years", "5+ years"]
LOCATION = ["india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
            "gurugram", "gurgaon", "noida", "delhi", "mumbai", "remote"]

# ---- Fetchers --------------------------------------------------------------
def fetch_adzuna(query):
    r = requests.get(
        "https://api.adzuna.com/v1/api/jobs/in/search/1",
        params={"app_id": ADZUNA_ID, "app_key": ADZUNA_KEY, "what": query,
                "results_per_page": 50, "max_days_old": 7},
        timeout=30)
    r.raise_for_status()
    return [{"id": f"adz-{j['id']}", "title": j["title"],
             "company": j["company"]["display_name"],
             "location": j["location"]["display_name"],
             "url": j["redirect_url"]} for j in r.json().get("results", [])]

def fetch_greenhouse(board):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", timeout=30)
    r.raise_for_status()
    return [{"id": f"gh-{board}-{j['id']}", "title": j["title"], "company": board,
             "location": j["location"]["name"], "url": j["absolute_url"]}
            for j in r.json().get("jobs", [])]

def fetch_lever(company):
    r = requests.get(f"https://api.lever.co/v0/postings/{company}",
                     params={"mode": "json"}, timeout=30)
    r.raise_for_status()
    return [{"id": f"lv-{company}-{j['id']}", "title": j["text"], "company": company,
             "location": j.get("categories", {}).get("location", ""),
             "url": j["hostedUrl"]} for j in r.json()]

def safe(fn, arg):
    try:
        return fn(arg)
    except Exception as e:           # one failing source must not stop the run
        print(f"[warn] {fn.__name__}({arg}) failed: {e}")
        return []

# ---- Filter ----------------------------------------------------------------
def relevant(job):
    t, loc = job["title"].lower(), job["location"].lower()
    return (any(k in t for k in INCLUDE)
            and not any(x in t for x in EXCLUDE)
            and any(l in loc for l in LOCATION))

# ---- Dedupe ----------------------------------------------------------------
db = sqlite3.connect("seen.db")
db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")

def is_new(job):
    return db.execute("SELECT 1 FROM seen WHERE id=?", (job["id"],)).fetchone() is None

# ---- Mail ------------------------------------------------------------------
def send_digest(jobs):
    rows = "".join(
        f"<tr><td>{j['title']}</td><td>{j['company']}</td>"
        f"<td>{j['location']}</td>"
        f"<td><a href='{j['url']}'>Apply</a></td></tr>" for j in jobs)
    html = (f"<h2>{len(jobs)} new fresher / intern roles</h2>"
            "<table border='1' cellpadding='6' cellspacing='0'>"
            "<tr><th>Role</th><th>Company</th><th>Location</th><th>Link</th></tr>"
            f"{rows}</table>")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Job Alert] {len(jobs)} new fresher/intern roles"
    msg["From"], msg["To"] = GMAIL_USER, MAIL_TO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.sendmail(GMAIL_USER, MAIL_TO, msg.as_string())

# ---- Main ------------------------------------------------------------------
def main():
    jobs = []
    for q in ADZUNA_QUERIES:
        jobs += safe(fetch_adzuna, q)
    for b in GREENHOUSE_BOARDS:
        jobs += safe(fetch_greenhouse, b)
    for c in LEVER_COMPANIES:
        jobs += safe(fetch_lever, c)

    seen_ids, fresh = set(), []
    for j in jobs:
        if j["id"] in seen_ids or not relevant(j) or not is_new(j):
            continue
        seen_ids.add(j["id"])
        fresh.append(j)

    print(f"Fetched {len(jobs)}, new & relevant {len(fresh)}")
    if fresh:
        send_digest(fresh)
        # Mark as seen only after the mail succeeds, so nothing is lost on failure
        db.executemany("INSERT OR IGNORE INTO seen (id) VALUES (?)",
                       [(j["id"],) for j in fresh])
        db.commit()

if __name__ == "__main__":
    main()
