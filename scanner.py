#!/usr/bin/env python3
"""Job Radar: a remote job scanner that scores postings against your own resumes.

Usage:
  python3 scanner.py scan --resumes resume_texts.json --out OUT_DIR --date 2026-09-19 \
      [--db-dir DB_DUMP] [--limit 20] [--targets targets.json] [--companies companies.json]

  --db-dir is a folder of JSON documents from previous runs (any key-value store can produce it):
      DB_DUMP/days/<date>.json      previous daily lists (for dedupe and repeats)
      DB_DUMP/status/<YYYY-MM>.json per-job statuses set on the tracker page
      DB_DUMP/resumes/<id>.json     cached resume texts (fallback when no --resumes file)

Writes to OUT_DIR:
  day.json            the document to store at days/<date>
  meta.json           the document to store at meta/summary
  resume_writes/*.json  resume docs that are new or changed (store at resumes/<id>)
  summary.md          a short human-readable summary
  candidates.json     everything that passed the remote/title filters (for debugging)
"""
import argparse, concurrent.futures as cf, datetime as dt, hashlib, html, json, os, re, sys, time
import xml.etree.ElementTree as ET

import requests

H = {"User-Agent": os.environ.get("JOB_RADAR_UA", "Mozilla/5.0 (job-radar; set JOB_RADAR_UA to add a contact)")}
TIMEOUT = 30


# ----------------------------------------------------------------------------- helpers
def strip_html(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = html.unescape(s)  # greenhouse double-escapes
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


SHIFT_SUFFIX = re.compile(
    r"\s*[-–|:]\s*(overnight|night|day|evening|weekend|weekday|rotating)s?\b.*$"
    r"|\s*\((?:[^)]*\b(?:am|pm|et|pt|ct|mt|est|pst|cst|shift|mon|tue|wed|thu|fri|sat|sun)\b[^)]*)\)", re.I)


def job_id(company, title, url):
    # shift and schedule suffixes ("- Night Shift (12a-9a ET, Tue-Fri)") are dropped so one role
    # posted five times for five schedules dedupes to a single entry
    key = norm(company) + "|" + norm(SHIFT_SUFFIX.sub("", title or ""))
    return hashlib.sha1(key.encode()).hexdigest()[:14]


def parse_date(v):
    """Return a date for many input shapes; None when unknown."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            if v > 1e12:
                v = v / 1000
            return dt.datetime.utcfromtimestamp(v).date()
        s = str(v)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return dt.date.fromisoformat(m.group(1))
        m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", s)  # 9/21/2026 (fractionaljobs detail pages)
        if m:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        m = re.match(r"\s*([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})", s)  # September 21, 2026
        if m:
            try:
                return dt.datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
            except ValueError:
                pass
        m = re.search(r"posted\s+(\d+)\+?\s+day", s, re.I)
        if m:
            return dt.date.today() - dt.timedelta(days=int(m.group(1)))
        if re.search(r"posted (today|yesterday)", s, re.I):
            return dt.date.today() - dt.timedelta(days=1 if "yesterday" in s.lower() else 0)
        if re.search(r"30\+ days", s, re.I):
            return dt.date.today() - dt.timedelta(days=31)
    except Exception:
        return None
    return None


def money(v):
    try:
        v = float(v)
        return v if v > 0 else None
    except Exception:
        return None


def fmt_pay(lo, hi, period=None, text=None):
    if text:
        return text
    if not lo and not hi:
        return ""
    def f(x):
        if x is None:
            return None
        if x >= 1000:
            return f"${x/1000:.0f}K"
        return f"${x:.0f}"
    per = ""
    if period:
        p = str(period).lower()
        per = "/hr" if "hour" in p else ("/yr" if ("year" in p or "annual" in p) else ("/mo" if "month" in p else ""))
    elif lo and lo < 500:
        per = "/hr"
    a, b = f(lo), f(hi)
    if a and b and a != b:
        return f"{a} - {b}{per}"
    return f"{a or b}{per}"


PAY_RX = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{2,3}(?:\.\d+)?k|\d{2,3}(?:\.\d{2})?)(?:\s*(?:-|–|to)\s*\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{2,3}(?:\.\d+)?k|\d{2,3}(?:\.\d{2})?))?\s*(?:per|/|an|a)?\s*(hour|hr|hourly|year|yr|annually|annum|annual)?", re.I)


def pay_from_text(text):
    """Best-effort pay extraction from a description."""
    best = None
    for m in PAY_RX.finditer(text[:12000]):
        raw = m.group(0).strip()
        lo = m.group(1).lower().replace(",", "")
        hi = (m.group(2) or "").lower().replace(",", "")
        unit = (m.group(3) or "").lower()
        def val(x):
            if not x:
                return None
            if x.endswith("k"):
                return float(x[:-1]) * 1000
            return float(x)
        vlo, vhi = val(lo), val(hi)
        if vlo is None:
            continue
        hourly = unit.startswith("h") and 15 <= vlo <= 400
        yearly = (unit.startswith(("y", "an")) and vlo >= 20000) or (not unit and vlo >= 30000)
        if not (hourly or yearly):
            continue
        s = fmt_pay(vlo, vhi, "hour" if hourly else "year")
        if best is None or (vhi or vlo) > best[0]:
            best = ((vhi or vlo), s)
    return best[1] if best else ""


# ----------------------------------------------------------------------------- sources
def fetch_json(url, method="get", payload=None):
    r = requests.request(method, url, headers={**H, "Content-Type": "application/json"}, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def src_greenhouse(slug):
    j = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for x in j.get("jobs", []):
        loc = (x.get("location") or {}).get("name", "") or ""
        offices = " ".join(o.get("name", "") for o in x.get("offices", []) or [])
        out.append(dict(
            source="greenhouse:" + slug, company=x.get("company_name") or slug.title(), title=x.get("title", ""),
            url=x.get("absolute_url", ""), location=(loc + " " + offices).strip(), desc=strip_html(x.get("content", "")),
            posted=parse_date(x.get("first_published") or x.get("updated_at")), etype_raw="", remote_flag=None, pay=""))
    return out


def src_lever(slug):
    j = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for x in j:
        c = x.get("categories", {}) or {}
        sr = x.get("salaryRange") or {}
        pay = fmt_pay(money(sr.get("min")), money(sr.get("max")), sr.get("interval")) if sr else ""
        desc = " \n".join(filter(None, [x.get("descriptionPlain", ""), " \n".join(strip_html(l.get("content", "")) for l in x.get("lists", []) or []), x.get("additionalPlain", "")]))
        out.append(dict(
            source="lever:" + slug, company=slug.replace("-", " ").title(), title=x.get("text", ""), url=x.get("hostedUrl", ""),
            location=" ".join(filter(None, [c.get("location", ""), " ".join(c.get("allLocations", []) or []), x.get("workplaceType", "")])),
            desc=desc, posted=parse_date(x.get("createdAt")), etype_raw=c.get("commitment", "") or "",
            remote_flag=(x.get("workplaceType") == "remote") if x.get("workplaceType") else None, pay=pay,
            country=x.get("country", "")))
    return out


def src_ashby(slug):
    j = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for x in j.get("jobs", []):
        if x.get("isListed") is False:
            continue
        comp = (x.get("compensation") or {}).get("compensationTierSummary") or ""
        locs = [x.get("location", "")] + [s.get("location", "") for s in x.get("secondaryLocations", []) or []]
        wt = x.get("workplaceType") or ""
        out.append(dict(
            source="ashby:" + slug, company=slug.replace("-", " ").title(), title=x.get("title", ""), url=x.get("jobUrl", ""),
            location=" | ".join(filter(None, locs)) + (" " + wt if wt else ""), desc=x.get("descriptionPlain") or strip_html(x.get("descriptionHtml", "")),
            posted=parse_date(x.get("publishedAt")), etype_raw=x.get("employmentType", "") or "",
            remote_flag=(wt.lower() == "remote") if wt else x.get("isRemote"), pay=comp))
    return out


WD_QUERIES = ["remote nurse", "remote clinical", "utilization management", "care management", "clinical quality",
              "stars hedis", "risk adjustment", "population health", "clinical program manager", "medical review",
              "quality improvement", "value based", "clinical consultant", "nurse consultant", "health equity",
              "product manager medicare", "provider performance", "compliance", "clinical operations", "appeals nurse"]


def src_workday(tenant, wd, site, max_details=70):
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    seen = {}
    for q in WD_QUERIES:
        try:
            j = fetch_json(base + "/jobs", "post", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q})
        except Exception:
            continue
        for p in j.get("jobPostings", []):
            path = p.get("externalPath")
            if path and path not in seen:
                seen[path] = p
    # keep only postings whose title looks relevant and mentions remote somewhere
    out = []
    cands = []
    for path, p in seen.items():
        title = p.get("title", "")
        loc = p.get("locationsText", "") or ""
        if not re.search(r"remote|work from home|telecommut|virtual|nationwide|anywhere", title + " " + loc, re.I):
            continue
        if TITLE_EXCLUDE.search(title):
            continue
        cands.append((path, p))
    def detail(item):
        path, p = item
        try:
            d = fetch_json(base + path)
            info = d.get("jobPostingInfo", {})
            return dict(
                source="workday:" + tenant, company=tenant.replace("health", " Health").title(), title=info.get("title") or p.get("title", ""),
                url=info.get("externalUrl") or f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
                location=" ".join(filter(None, [info.get("location", ""), p.get("locationsText", ""), info.get("remoteType", "")])),
                desc=strip_html(info.get("jobDescription", "")), posted=parse_date(info.get("startDate") or p.get("postedOn")),
                etype_raw=info.get("timeType", "") or "", remote_flag=None, pay="")
        except Exception:
            return None
    with cf.ThreadPoolExecutor(6) as ex:
        for r in ex.map(detail, cands[:max_details]):
            if r:
                out.append(r)
    return out


def src_remotive():
    j = fetch_json("https://remotive.com/api/remote-jobs")
    out = []
    for x in j.get("jobs", []):
        out.append(dict(
            source="remotive", company=x.get("company_name", ""), title=x.get("title", ""), url=x.get("url", ""),
            location=x.get("candidate_required_location", "") or "Worldwide", desc=strip_html(x.get("description", "")),
            posted=parse_date(x.get("publication_date")), etype_raw=x.get("job_type", "") or "", remote_flag=True,
            pay=x.get("salary", "") or "", category=x.get("category", "")))
    return out


def src_remoteok():
    j = fetch_json("https://remoteok.com/api")
    out = []
    for x in j:
        if not isinstance(x, dict) or not x.get("position"):
            continue
        out.append(dict(
            source="remoteok", company=x.get("company", ""), title=x.get("position", ""), url=x.get("url", ""),
            location=x.get("location", "") or "Worldwide", desc=strip_html(x.get("description", "")),
            posted=parse_date(x.get("date")), etype_raw="", remote_flag=True,
            pay=fmt_pay(money(x.get("salary_min")), money(x.get("salary_max")), "year"), tags=x.get("tags", [])))
    return out


def src_jobicy():
    out = []
    for ind in ("healthcare", "management", "consulting", "product", "data-science", "hr", "customer-success", ""):
        try:
            url = "https://jobicy.com/api/v2/remote-jobs?count=100" + (f"&industry={ind}" if ind else "")
            j = fetch_json(url)
        except Exception:
            continue
        for x in j.get("jobs", []):
            out.append(dict(
                source="jobicy", company=x.get("companyName", ""), title=x.get("jobTitle", ""), url=x.get("url", ""),
                location=x.get("jobGeo", "") or "Anywhere", desc=strip_html(x.get("jobDescription", "")),
                posted=parse_date(x.get("pubDate")), etype_raw=" ".join(x.get("jobType", []) or []), remote_flag=True,
                pay=fmt_pay(money(x.get("salaryMin")), money(x.get("salaryMax")), x.get("salaryPeriod"))))
    return out


def src_himalayas():
    out = []
    for offset in (0, 100, 200, 300, 400):
        try:
            j = fetch_json(f"https://himalayas.app/jobs/api?limit=100&offset={offset}")
        except Exception:
            break
        jobs = j.get("jobs", [])
        if not jobs:
            break
        for x in jobs:
            out.append(dict(
                source="himalayas", company=x.get("companyName", ""), title=x.get("title", ""), url=x.get("applicationLink", ""),
                location=", ".join(x.get("locationRestrictions", []) or []) or "Worldwide", desc=strip_html(x.get("description", "")),
                posted=parse_date(x.get("pubDate")), etype_raw=x.get("employmentType", "") or "", remote_flag=True,
                pay=fmt_pay(money(x.get("minSalary")), money(x.get("maxSalary")), x.get("salaryPeriod"))))
    return out


def src_workingnomads():
    j = fetch_json("https://www.workingnomads.com/api/exposed_jobs/")
    out = []
    for x in j:
        out.append(dict(
            source="workingnomads", company=x.get("company_name", ""), title=x.get("title", ""), url=x.get("url", ""),
            location=x.get("location", "") or "Global", desc=strip_html(x.get("description", "")),
            posted=parse_date(x.get("pub_date")), etype_raw="", remote_flag=True, pay=""))
    return out


def src_wwr():
    out = []
    for cat in ("remote-management-and-finance-jobs", "remote-product-jobs", "remote-customer-support-jobs",
                "remote-all-other-remote-jobs", "remote-jobs"):
        try:
            r = requests.get(f"https://weworkremotely.com/categories/{cat}.rss", headers=H, timeout=TIMEOUT)
            root = ET.fromstring(r.content)
        except Exception:
            continue
        for it in root.iter("item"):
            t = it.findtext("title", "")
            company, _, title = t.partition(": ")
            if not title:
                title, company = t, ""
            out.append(dict(
                source="weworkremotely", company=company.strip(), title=title.strip(), url=it.findtext("link", ""),
                location=" ".join(filter(None, [it.findtext("region", ""), it.findtext("country", "")])) or "Anywhere",
                desc=strip_html(it.findtext("description", "")), posted=parse_date(it.findtext("pubDate", "")),
                etype_raw=it.findtext("type", "") or "", remote_flag=True, pay=""))
    return out


TJFG_QUERIES = ["health", "healthcare", "clinical", "nurse", "medicaid", "public health", "health equity", "policy", "care",
                "part-time", "contract", "consultant", "fractional", "interim", "nonprofit"]


def src_techjobsforgood(max_details=150):
    """Tech Jobs for Good: nonprofit / mission-driven tech board. Cards are server-rendered; job pages carry a JobPosting JSON-LD block."""
    cards = {}
    for q in TJFG_QUERIES:
        for page in (1, 2):
            try:
                r = requests.get("https://techjobsforgood.com/jobs/", params={"q": q, "remote": "true", "page": page}, headers=H, timeout=TIMEOUT)
                if r.status_code != 200:
                    break
            except Exception:
                break
            found = 0
            for m in re.finditer(r'<div class="ui raised fluid card job-card".*?<div class="extra content">', r.text, re.S):
                blk = m.group(0)
                jid = re.search(r'href="/jobs/(\d+)/', blk)
                title = re.search(r'class="header job-title"\s+title="([^"]*)"', blk)
                company = re.search(r'class="company_name"[^>]*>\s*([^<]*)<', blk)
                loc = re.search(r'class="location"\s+title="([^"]*)"', blk)
                sal = re.search(r'class="salary"\s+title="([^"]*)"', blk)
                if not (jid and title):
                    continue
                found += 1
                cards.setdefault(jid.group(1), dict(
                    title=html.unescape(title.group(1)).strip(), company=html.unescape(company.group(1)).strip() if company else "",
                    location=html.unescape(loc.group(1)).strip() if loc else "Remote", pay=html.unescape(sal.group(1)).strip() if sal else ""))
            if found < 10:
                break
    def detail(item):
        jid, c = item
        url = f"https://techjobsforgood.com/jobs/{jid}/"
        try:
            r = requests.get(url, headers=H, timeout=TIMEOUT)
            m = re.search(r'<script type="application/ld\+json">\s*(\{.*?"@type":\s*"JobPosting".*?\})\s*</script>', r.text, re.S)
            posted, desc = None, ""
            if m:
                try:
                    ld = json.loads(m.group(1))
                    posted = parse_date(ld.get("datePosted"))
                    desc = strip_html(ld.get("description", ""))
                    et = ld.get("employmentType") or ""
                except Exception:
                    et = ""
            else:
                et = ""
            if not desc:
                md = re.search(r'itemprop="description"[^>]*>(.*?)<div class="ui relaxed divided list">', r.text, re.S)
                desc = strip_html(md.group(1)) if md else ""
            return dict(source="techjobsforgood", company=c["company"], title=c["title"], url=url, location=c["location"],
                        desc=desc, posted=posted, etype_raw=et if isinstance(et, str) else " ".join(et), remote_flag=True, pay=c["pay"],
                        tags=["nonprofit"])
        except Exception:
            return None
    out = []
    items = [(k, v) for k, v in cards.items() if re.search(r"remote", v["location"], re.I)][:max_details]
    with cf.ThreadPoolExecutor(6) as ex:
        for r in ex.map(detail, items):
            if r:
                out.append(r)
    return out


def src_fractionaljobs(max_details=120):
    """fractionaljobs.io (Webflow board of fractional / part-time leadership roles). The home page renders every
    live card server-side; each job page carries the description, the added date, the outside application link
    and a hidden-unless-true 'This Role is Closed' block. Only cards whose location starts with Remote are kept."""
    r = requests.get("https://www.fractionaljobs.io/", headers=H, timeout=TIMEOUT)
    r.raise_for_status()
    cards = []
    for m in re.finditer(r'<div role="listitem" class="job-item w-dyn-item">(.*?)<div class="job-item_date">', r.text, re.S):
        blk = m.group(1)
        slug = re.search(r'href="/jobs/([^"]+)"', blk)
        h3 = [html.unescape(x).strip() for x in re.findall(r'<h3 class="text-size-regular text-inline">([^<]*)</h3>', blk)]
        h3 = [x for x in h3 if x and x != "-"]
        if not slug or len(h3) < 2:
            continue
        info = [html.unescape(x).replace("\xa0", " ").strip() for x in re.findall(r'<div class="text-inline[^"]*">([^<]*)</div>', blk.split('class="job-item_more-info"')[-1])]
        info = [x for x in info if x and x != "|"]
        hours = next((x for x in info if re.search(r"\bhrs?\b", x)), "")
        pay = next((x for x in info if re.search(r"[$€£]", x)), "")
        loc = next((x for x in info if re.search(r"^(remote|hybrid|on.?site)", x, re.I)), "")
        func = re.search(r'fs-cmsfilter-field="function">([^<]*)<', blk)
        date = re.search(r'<div class="date">([^<]*)<', blk)
        cards.append(dict(slug=slug.group(1), company=h3[0], title=h3[1], hours=hours, pay=pay, location=loc,
                          function=html.unescape(func.group(1)).strip() if func else "", date=date.group(1).strip() if date else ""))
    remote = [c for c in cards if re.match(r"remote", c["location"], re.I)][:max_details]

    def norm_pay(p):
        if not p or p.lower() == "unknown":
            return ""
        p = re.sub(r"\s*/\s*hr\b", "/hr", p)
        p = re.sub(r"\s*/\s*mo(nth)?\b", "/mo", p)
        return re.sub(r"\s*/\s*(yr|year)\b", "/yr", p)

    def detail(c):
        url = f"https://www.fractionaljobs.io/jobs/{c['slug']}"
        try:
            r = requests.get(url, headers=H, timeout=TIMEOUT)
            t = r.text
            closed = re.search(r'<div class="home-jobs_hiring-block-jobs(?![^"]*w-condition-invisible)"[^>]*>\s*<h2[^>]*>This Role is Closed', t)
            if closed:
                return None
            secs = re.findall(r'<(?:section|div) class="text-rich-text(?![^"]*w-condition-invisible)[^"]*w-richtext"[^>]*>(.*?)</(?:section|div)>', t, re.S)
            desc = strip_html("\n".join(secs))
            added = re.search(r'class="post-date[^"]*">([^<]*)<', t)
            posted = parse_date(added.group(1).strip()) if added else parse_date(c["date"])
            apply_m = re.search(r'<a href="(https?://[^"#][^"]*)"[^>]*class="button[^"]*checked-cookie-btn[^"]*"[^>]*>View Application', t)
            industry = re.search(r'Industry</h2></div><div>([^<]*)<', t)
            stage = re.search(r'Company Stage</h2></div><div>([^<]*)<', t)
            loc = c["location"].strip()
            if re.fullmatch(r"remote", loc, re.I):
                loc = "Remote, Anywhere"
            elif re.search(r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b", loc) and not re.search(r"\b(usa|us|united states)\b", loc, re.I):
                loc = loc.replace("Remote (", "Remote, USA (", 1)  # e.g. "Remote (FL or GA only)"
            head = " | ".join(x for x in [f"Fractional role: {c['hours']}/week" if c["hours"] else "Fractional role", norm_pay(c["pay"]),
                                          loc, f"Function: {c['function']}" if c["function"] else "",
                                          f"Industry: {html.unescape(industry.group(1)).strip()}" if industry else "",
                                          f"Stage: {html.unescape(stage.group(1)).strip()}" if stage else "",
                                          f"Apply: {html.unescape(apply_m.group(1))}" if apply_m else ""] if x)
            return dict(source="fractionaljobs", company=c["company"], title=c["title"], url=url, location=loc,
                        desc=head + "\n" + desc, posted=posted, etype_raw=f"fractional {c['hours']}".strip(), remote_flag=True,
                        pay=norm_pay(c["pay"]), tags=[])
        except Exception:
            return None
    out = []
    with cf.ThreadPoolExecutor(6) as ex:
        for r in ex.map(detail, remote):
            if r:
                out.append(r)
    if cards and not out and remote:
        raise RuntimeError(f"parsed {len(cards)} cards but no remote detail pages loaded")
    return out


PHIL_QUERIES = ["part-time", "part time", "contract", "fractional", "interim", "temporary", "consultant", "remote"]


def src_philanthropy(max_details=160):
    """Chronicle of Philanthropy job board (jobs.philanthropy.com, a Madgex board for the nonprofit sector).
    The RSS search feed lists 20 results a page; each job page carries a JobPosting JSON-LD block and a
    definition list with Location, Salary and Employment Type. Only postings that say they are remote are kept."""
    hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
    links = {}
    ok_pages = 0
    for q in PHIL_QUERIES:
        for page in (1, 2, 3):
            try:
                r = requests.get("https://jobs.philanthropy.com/jobsrss/", params={"keywords": q, "countrycode": "US", "Page": page}, headers=hdr, timeout=TIMEOUT)
                if r.status_code != 200:
                    break
            except Exception:
                break
            ok_pages += 1
            items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
            for it in items:
                lm = re.search(r"<link>(.*?)</link>", it, re.S)
                tm = re.search(r"<title>(.*?)</title>", it, re.S)
                if lm and tm:
                    links.setdefault(html.unescape(lm.group(1)).strip().split("?")[0], html.unescape(tm.group(1)).strip())
            if len(items) < 20:
                break
    if not ok_pages:
        raise RuntimeError("no feed pages loaded")

    def detail(item):
        url, feed_title = item
        try:
            t = requests.get(url, headers=hdr, timeout=TIMEOUT).text
            m = re.search(r'<script type="application/ld\+json">(.*?)</script>', t, re.S)
            ld = json.loads(m.group(1), strict=False) if m else {}
            dd = {strip_html(k).strip(): strip_html(v).strip() for k, v in re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", t, re.S)}
            desc = strip_html(ld.get("description", ""))
            loc = dd.get("Location", "")
            salary = dd.get("Salary", "")
            title = (ld.get("title") or feed_title.split(": ", 1)[-1]).strip()
            company = ((ld.get("hiringOrganization") or {}).get("name") or dd.get("Employer") or feed_title.split(": ", 1)[0]).strip()
            remote = (ld.get("jobLocationType") == "TELECOMMUTE" or re.search(r"working from home|remote|telecommut|virtual", loc + " " + title + " " + salary, re.I)
                      or re.search(r"(fully|100%|entirely) remote|remote[- ](position|role|opportunity|first|based)|this (role|position) is remote|work(ing)? remotely|location:\s*remote", desc, re.I))
            if not remote or re.search(r"\bhybrid\b|on.?site", loc + " " + title, re.I):
                return None
            et = ld.get("employmentType") or ""
            if isinstance(et, list):
                et = " ".join(et)
            et = (re.sub(r'[\[\]"_,]', " ", et) + " " + dd.get("Employment Type", "")).strip()
            if re.search(r"part.?time", salary, re.I) and not re.search(r"part", et, re.I):
                et += " part time"
            place = "" if re.fullmatch(r"(working from home|united states|usa?|remote)", loc.strip(), re.I) else loc
            head = " | ".join(x for x in [salary, dd.get("Employment Type", ""), f"Position type: {dd['Position Type']}" if dd.get("Position Type") else ""] if x)
            return dict(source="philanthropy", company=company, title=title, url=url,
                        location="Remote, USA" + (f" ({place})" if place else ""), desc=(head + "\n" if head else "") + desc,
                        posted=parse_date(ld.get("datePosted") or dd.get("Posted Date")), etype_raw=et, remote_flag=True,
                        pay=pay_from_text(salary) or "", tags=["nonprofit"])
        except Exception:
            return None
    out = []
    with cf.ThreadPoolExecutor(6) as ex:
        for r in ex.map(detail, list(links.items())[:max_details]):
            if r:
                out.append(r)
    return out


IDEALIST_APP = os.environ.get("IDEALIST_APP", "")
IDEALIST_KEY = os.environ.get("IDEALIST_KEY", "")  # idealist.org's public search-only Algolia credentials; copy them from the site's own page requests


def src_idealist(max_age_days=45):
    if not IDEALIST_APP or not IDEALIST_KEY:
        return []
    """idealist.org (nonprofit sector board). The site is client-rendered over Algolia; this asks the same
    published-desc index its own search page uses for remote JOB listings open to the US (or the whole world)."""
    hdr = {**H, "X-Algolia-Application-Id": IDEALIST_APP, "X-Algolia-API-Key": IDEALIST_KEY, "Content-Type": "application/json"}
    cut = int(time.time()) - max_age_days * 86400
    fields = ["name", "description", "orgName", "orgType", "url", "published", "jobType", "locationType", "remoteZone",
              "remoteState", "remoteCountry", "country", "stateStr", "city", "salaryMinimum", "salaryMaximum", "salaryPeriod",
              "salaryCurrency", "functions", "areasOfFocus", "professionalLevel", "objectID", "isFullTime"]
    out, page = [], 0
    while page < 5:
        r = requests.post(f"https://{IDEALIST_APP}-dsn.algolia.net/1/indexes/idealist7-production-published-desc/query", headers=hdr,
                          json={"query": "", "hitsPerPage": 1000, "page": page, "attributesToRetrieve": fields, "attributesToHighlight": [],
                                "filters": f"type:JOB AND locationType:REMOTE AND published > {cut}"}, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        for x in j.get("hits", []):
            zone = x.get("remoteZone") or ""
            st = x.get("remoteState") or x.get("state") or ""
            # city-pinned "remote" roles (must live near one metro) only help when that metro is in Georgia or South Carolina
            if zone == "CITY" and not (x.get("country") == "US" and st in ("GA", "SC")):
                continue
            if not (x.get("remoteCountry") == "US" or zone == "WORLD" or (zone == "CITY" and x.get("country") == "US")):
                continue
            if zone == "WORLD":
                loc = "Remote, Anywhere"
            elif zone == "STATE" and (x.get("stateStr") or x.get("remoteState")):
                loc = f"Remote, USA ({x.get('stateStr') or x.get('remoteState')} only)"
            elif zone == "CITY" and x.get("city"):
                loc = f"Remote, USA ({x.get('city')}, {x.get('stateStr') or x.get('remoteState') or ''} area)"
            else:
                loc = "Remote, USA"
            cur = x.get("salaryCurrency") or "USD"
            pay = fmt_pay(money(x.get("salaryMinimum")), money(x.get("salaryMaximum")), x.get("salaryPeriod")) if cur == "USD" else ""
            path = (x.get("url") or {}).get("en") or ""
            if not path:
                continue
            out.append(dict(
                source="idealist", company=x.get("orgName") or "Confidential organization (posted anonymously on Idealist)", title=x.get("name", ""), url="https://www.idealist.org" + path,
                location=loc, desc=strip_html(x.get("description", "")), posted=parse_date(x.get("published")),
                etype_raw=" ".join(x.get("jobType") or []).replace("_", " "), remote_flag=True, pay=pay,
                full_time_flag=bool(x.get("isFullTime")), org_type=x.get("orgType") or "",
                tags=["nonprofit"] if x.get("orgType") == "NONPROFIT" else [],
                category=" ".join((x.get("functions") or []) + (x.get("areasOfFocus") or []))))
        page += 1
        if page >= j.get("nbPages", 1):
            break
    return out


# ----------------------------------------------------------------------------- matching
TITLE_EXCLUDE = None  # set in main from targets
NP_RX = re.compile(r"(501\s?\(c\)|\b(a|the|our|as a|is a|we are a|we're a|leading|national|global|independent)\s+(non.?profit|not.for.profit)\b|\bnon.?profit (organization|health|hospital|health system|health plan|association|institute|foundation)\b|\bpublic benefit corporation\b|\bphilanthrop)", re.I)

# the focus filter (targets.json "focus") uses a stricter test than the lens: public benefit corporations do not count
NP_STRICT = re.compile(r"(501\s?\(c\)|\b(a|an|the|our|as a|is a|we are a|we're a|leading|national|global|independent|small|growing|local|international|registered)\s+(non.?profit|not.for.profit)\b|\bnon.?profit (organization|health|hospital|health system|health plan|association|institute|foundation|board|sector|client|agenc)\w*\b|\bphilanthrop|\bcharitable organi[sz]ation\b|\b(tax.exempt|fiscally sponsored)\b)", re.I)
NP_LOOSE = re.compile(r"\bnon.?profits?\b|\bnot.for.profits?\b|501\s?\(c\)", re.I)
NP_BOARDS = ("idealist", "philanthropy", "techjobsforgood")


def is_nonprofit(j):
    """True when the employer is a nonprofit: tagged by the source, named as one, or described as one in the posting."""
    if "nonprofit" in (j.get("tags") or []):
        return True
    if NP_LOOSE.search((j.get("company") or "") + " " + (j.get("title") or "")):
        return True
    desc = j.get("desc", "") or ""
    if NP_STRICT.search(desc[:6000]):
        return True
    # on the nonprofit-sector boards any plain mention of nonprofit work is enough (consulting firms and
    # anonymous postings there serve nonprofits)
    return bool(j.get("source", "").split(":")[0] in NP_BOARDS and NP_LOOSE.search(desc))


class Matcher:
    def __init__(self, targets):
        self.t = targets
        self.families = [(f["name"], f["weight"], re.compile(f["title"], re.I), f.get("context_in_title_or_health_company", False),
                          f.get("context_nonprofit_or_fractional", False)) for f in targets["families"]]
        self.health = re.compile(targets["health_context"], re.I)
        self.ex_title = re.compile(targets["exclude_title"], re.I)
        self.ex_desc = re.compile(targets["exclude_desc"], re.I)
        self.skills = [(re.compile(p, re.I), w, label) for p, w, label in targets["skills"]]
        self.emp = {k: re.compile(v, re.I) for k, v in targets["employment"].items()}
        self.us_ok = re.compile(targets["us_ok"], re.I)
        self.non_us = re.compile(targets["non_us"], re.I)
        self.onsite = re.compile(targets["onsite"], re.I)
        # development / grants / fundraising roles are never listed (targets.json "fundraising", set 2026-10-16)
        fr = targets.get("fundraising") or {}
        self.fund_title = re.compile(fr["title"], re.I) if fr.get("title") else None
        self.fund_dev_ok = re.compile(fr["development_ok"], re.I) if fr.get("development_ok") else None
        self.fund_desc = re.compile(fr["desc_terms"], re.I) if fr.get("desc_terms") else None
        self.fund_desc_min = int(fr.get("desc_min", 10))

    def fundraising_title(self, title):
        """True when the title is a development, grants or fundraising seat. A bare 'development' counts unless it is
        qualified (business development, product development, workforce development and so on)."""
        if self.fund_title is None:
            return False
        if self.fund_title.search(title):
            return True
        for m in re.finditer(r"\bdevelopment\b", title, re.I):
            if not (self.fund_dev_ok and any(o.end() == m.end() for o in self.fund_dev_ok.finditer(title))):
                return True
        return False

    def fundraising_heavy(self, j):
        """True when the posting's text is mostly about raising money: enough distinct fundraising terms in the
        first 4000 characters, with one of them in the opening lines."""
        if self.fund_desc is None:
            return False
        desc = j.get("desc", "") or ""
        hits = {m.group(0).lower() for m in self.fund_desc.finditer(desc[:4000])}
        return len(hits) >= self.fund_desc_min and bool(self.fund_desc.search(desc[:500]))

    def is_remote_us(self, j):
        loc = j.get("location", "") or ""
        title = j.get("title", "")
        head = j.get("desc", "")[:1500]
        src = j["source"]
        loc_l = loc.lower()
        if j.get("country") and j["country"] not in ("US", "USA", ""):
            return False, "non-US country"
        if self.non_us.search(loc) and not self.us_ok.search(loc):
            return False, "non-US location"
        if re.search(r"\b(on.?site|in.?person|hybrid|in.?office)\b", title, re.I) and not re.search(r"remote", title, re.I):
            return False, "onsite/hybrid"
        remote_words = re.search(r"remote|work from home|wfh|telecommut|virtual|anywhere|nationwide|distributed", loc + " " + title, re.I)
        if src.startswith(("greenhouse", "lever", "ashby", "workday")):
            if j.get("remote_flag") is False and not remote_words:
                return False, "not remote"
            if j.get("remote_flag") is None and not remote_words:
                # allow when the description clearly says fully remote near the top, or when the posting
                # anywhere states it plainly (boards like Pomelo Care label remote roles "HQ" and only say
                # "Fully remote work flexibility (within the US)" down in the benefits list)
                if not re.search(r"(fully|100%|entirely) remote|remote[- ](first|based|position|role|opportunity)|this (role|position) is remote|work remotely", head, re.I) \
                        and not re.search(r"(fully|100%|entirely) remote|remote[- ]first|remote work flexibility|remote work environment", j.get("desc", ""), re.I):
                    return False, "not remote"
            if self.onsite.search(loc) and not re.search(r"remote", loc, re.I):
                return False, "onsite/hybrid"
            if "hybrid" in loc_l and "remote" not in loc_l:
                return False, "hybrid"
            return True, ""
        # aggregators: everything is remote; the stated geography must allow the US
        if loc.strip() and not self.us_ok.search(loc):
            return False, "non-US location"
        return True, ""

    def family(self, j, health_company):
        title = j["title"]
        if self.ex_title.search(title):
            return None
        text_head = title + " " + j.get("desc", "")[:800]
        best = None
        np_ctx = None
        for name, w, rx, need_ctx, need_np in self.families:
            if rx.search(title):
                if need_ctx and not (health_company or self.health.search(title) or (len(self.health.findall(text_head)) >= 2)):
                    continue
                if need_np:
                    if np_ctx is None:
                        np_ctx = bool(is_nonprofit(j) or j.get("source") == "fractionaljobs"
                                      or "fractional" in (j.get("etype_raw") or "").lower() or NP_RX.search(j.get("desc", "")[:2500]))
                    if not np_ctx:
                        continue
                if best is None or w > best[1]:
                    best = (name, w)
        if best is None:
            return None
        # generic family still needs a real health company or strong health context
        if best[0].startswith("Other") and not (health_company or len(self.health.findall(text_head)) >= 2):
            return None
        return best

    def employment(self, j):
        raw = (j.get("etype_raw") or "").lower().replace("-", " ").replace("_", " ")
        title = j["title"].lower()
        head = re.sub(r"qualified independent contractor|independent contractor \(qic\)|contractor \(psc\)", " ", j.get("desc", "")[:3000], flags=re.I)
        if self.emp["contract"].search(raw) or self.emp["contract"].search(title):
            return "contract"
        if "part" in raw or self.emp["part_time"].search(title):
            return "part_time"
        if "full" in raw or "fulltime" in raw.replace(" ", "") or self.emp["full_time"].search(title):
            # boards sometimes file a 1099 role under "FullTime" (one board's RN advocate posting said
            # "a remote, 1099 position with flexible hours"); the posting's own words win when they are unambiguous
            if re.search(r"\b1099\b|independent contractor", head, re.I) and not re.search(r"\bw-?2\b", head, re.I):
                return "contract"
            return "full_time"
        if self.emp["contract_desc"].search(head):
            return "contract"
        if re.search(r"\bpart.?time\b", head, re.I) and not re.search(r"\bfull.?time\b", head, re.I):
            return "part_time"
        if re.search(r"\bfull.?time\b", head, re.I):
            return "full_time"
        return "unknown"

    HOURS_RX = re.compile(r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:hrs?|hours)\b|\b(\d{1,2})\s*(?:hrs?|hours)\s*(?:per|a|/|each)\s*(?:week|wk)\b", re.I)

    def part_time_ok(self, j):
        """Focus filter: keeps part-time, fractional and other reduced-hours work; a contract or temporary role
        that says it is full-time (or 35+ hours a week) is not part-time and is dropped."""
        if j.get("etype") not in ("contract", "part_time"):
            return False
        raw = (j.get("etype_raw") or "").lower().replace("-", " ").replace("_", " ")
        title = j["title"].lower()
        head = j.get("desc", "")[:3000]
        if j["etype"] == "part_time":
            return True
        if re.search(r"\bpart.?time\b|\bfractional\b", raw + " " + title) or self.emp["part_time"].search(title):
            return True
        if j.get("full_time_flag") or re.search(r"\bfull.?time\b", raw + " " + title):
            return False
        hrs = self.week_hours(j)
        if hrs:
            return hrs[1] < 35
        head_nf = re.sub(r"not (a |an )?full.?time|less than full.?time|full.?time (staff|employees?|team|equivalent)", " ", head, flags=re.I)
        if re.search(r"\bfull.?time\b|\b40 (hrs|hours)\b", head_nf, re.I) and not re.search(r"\bpart.?time\b|\bfractional\b", head, re.I):
            return False
        return True

    def week_hours(self, j):
        """(low, high) weekly hours when the posting states them near the top, else None."""
        text = (j.get("etype_raw") or "") + " | " + j["title"] + " | " + j.get("desc", "")[:1500]
        for m in self.HOURS_RX.finditer(text):
            lo, hi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(3)))
            if 2 <= lo <= hi <= 60:
                return lo, hi
        return None

    def flex_label(self, j):
        t = ((j.get("etype_raw") or "") + " " + j["title"]).lower().replace("_", " ")
        if "fractional" in t:
            base = "Fractional"
        elif "interim" in t:
            base = "Interim"
        elif j["etype"] == "part_time" or re.search(r"part.?time", t):
            base = "Temporary, part-time" if re.search(r"\b(temporary|temp|seasonal)\b", t) else "Part-time"
        elif re.search(r"consult|freelance|1099|contractor|contract", t):
            base = "Contract / consulting"
        else:
            base = "Contract / 1099"
        hrs = self.week_hours(j)
        if hrs:
            base += f" · {hrs[0]}-{hrs[1]} hrs/wk" if hrs[0] != hrs[1] else f" · {hrs[0]} hrs/wk"
        return base

    STATES = {"al":"alabama","ak":"alaska","az":"arizona","ar":"arkansas","ca":"california","co":"colorado","ct":"connecticut","de":"delaware","fl":"florida","ga":"georgia","hi":"hawaii","id":"idaho","il":"illinois","in":"indiana","ia":"iowa","ks":"kansas","ky":"kentucky","la":"louisiana","me":"maine","md":"maryland","ma":"massachusetts","mi":"michigan","mn":"minnesota","ms":"mississippi","mo":"missouri","mt":"montana","ne":"nebraska","nv":"nevada","nh":"new hampshire","nj":"new jersey","nm":"new mexico","ny":"new york","nc":"north carolina","nd":"north dakota","oh":"ohio","ok":"oklahoma","or":"oregon","pa":"pennsylvania","ri":"rhode island","sc":"south carolina","sd":"south dakota","tn":"tennessee","tx":"texas","ut":"utah","vt":"vermont","va":"virginia","wa":"washington","wv":"west virginia","wi":"wisconsin","wy":"wyoming"}

    def state_penalty(self, j):
        """Penalize remote roles pinned to states other than Georgia or South Carolina."""
        text = j["title"] + " | " + (j.get("location") or "")
        if re.search(r"nationwide|anywhere|any state|multiple states|all states|us remote|remote,? us\b|remote - us|united states", text, re.I):
            return 0
        found = set()
        for m in re.finditer(r"\b([A-Z]{2})\b", text):
            if m.group(1).lower() in self.STATES:
                found.add(m.group(1).lower())
        low = text.lower()
        for ab, name in self.STATES.items():
            if re.search(r"\b" + re.escape(name) + r"\b", low):
                found.add(ab)
        if not found:
            desc = j.get("desc", "")[:6000]
            m = re.search(r"(must (reside|live|be located) in|residents? of|located in|based in) (the state of )?([A-Za-z ,]{3,60})", desc, re.I)
            if m:
                seg = m.group(4).lower()
                for ab, name in self.STATES.items():
                    if re.search(r"\b" + re.escape(name) + r"\b", seg):
                        found.add(ab)
                if found and not (found & {"ga", "sc"}):
                    return 15
            return 0
        if found & {"ga", "sc"}:
            return 0
        return 6 if len(found) <= 3 else 0

    def skills_hits(self, text):
        hits = []
        for rx, w, label in self.skills:
            if rx.search(text):
                hits.append((w, label))
        hits.sort(reverse=True)
        return hits


# ----------------------------------------------------------------------------- resume fit
def load_resumes(path, db_dir):
    res = []
    if path and os.path.exists(path):
        d = json.load(open(path, encoding="utf-8"))
        for r in d.get("resumes", []):
            res.append({"id": r["id"], "file": r["file"], "label": r["label"], "mtimeMs": r.get("mtimeMs"), "words": r.get("words"), "text": r.get("text", "")})
        return res, "laptop"
    if db_dir and os.path.isdir(os.path.join(db_dir, "resumes")):
        for fn in os.listdir(os.path.join(db_dir, "resumes")):
            if fn.endswith(".json"):
                d = json.load(open(os.path.join(db_dir, "resumes", fn), encoding="utf-8"))
                d = d.get("data", d)
                if d.get("text"):
                    res.append({"id": d.get("id") or fn[:-5], "file": d.get("file", fn), "label": d.get("label", fn), "mtimeMs": d.get("mtimeMs"), "words": d.get("words"), "text": d["text"]})
        return res, "database"
    return res, "none"


def resume_fit(jobs, resumes):
    """Cosine similarity (TF-IDF) between each job and each resume; returns per-job (best_label, best_file, sim)."""
    if not jobs or not resumes:
        return [("", "", 0.0, -1)] * len(jobs)
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    rtexts = [r["text"] for r in resumes]
    jtexts = [(j["title"] + " ") * 3 + j.get("desc", "")[:6000] for j in jobs]
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=2, max_features=60000, sublinear_tf=True)
    vec.fit(rtexts + jtexts)
    S_full = cosine_similarity(vec.transform(jtexts), vec.transform(rtexts))
    # headline pass: job title + opening of the posting vs resume label + summary
    rhead = [(r["label"] + " ") * 4 + r["text"][:1800] for r in resumes]
    jhead = [(j["title"] + " ") * 4 + j.get("family", "") + " " + j.get("desc", "")[:1500] for j in jobs]
    vec2 = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1, max_features=40000, sublinear_tf=True)
    vec2.fit(rhead + jhead)
    S_head = cosine_similarity(vec2.transform(jhead), vec2.transform(rhead))
    S = 0.5 * S_full + 0.5 * S_head
    out = []
    for i in range(len(jobs)):
        k = int(S[i].argmax())
        out.append((resumes[k]["label"], resumes[k]["file"], float(S[i][k]), k))
    return out


# ----------------------------------------------------------------------------- four-part score
# Skills overlap 40, seniority fit 25, domain fit 20, practical fit 15 (weights and word lists live in the
# "scoring" block of targets.json; the values below are the fallbacks).
SCORING_DEFAULTS = {
    "weights": {"skills": 40, "seniority": 25, "domain": 20, "practical": 15},
    "candidate_years": 12,
    "min_seniority": 10,
    "skills_mix": {"resume_similarity": 0.45, "skill_lexicon": 0.30, "requirements_covered": 0.25},
    "level_points": {"5": 25, "4": 25, "3": 20, "2": 12, "1": 4},
    "exec_at_large_employer": 21,
    "levels": {
        "5": r"\b(executive director|chief|ceo|coo|president|vice president|vp|svp|avp|managing director|head of|head)\b",
        "4": r"\b(director|principal|senior manager|sr\.? manager|deputy)\b",
        "3": r"\b(manager|lead|leader|senior|sr\.?|consultant|consulting|advisor|adviser|strategist|supervisor|officer|architect|fractional|interim|founding|counsel)\b",
        "2": r"\b(specialist|analyst|nurse|rn|advocate|navigator|liaison|trainer|facilitator|educator|writer|reviewer|instructor|faculty|support)\b",
        "1": r"\b(intern|internship|assistant|coordinator|associate|representative|clerk|aide|entry|junior|jr\.?|apprentice|fellow)\b",
    },
    "junior_pay_hourly": 28,
    "domain_core": r"\b(maternal|perinatal|prenatal|postpartum|midwi\w+|birth(s|ing)?|doulas?|obstetric\w*|women'?s health|reproductive|abortion|deaf|hard of hearing|asl|sign language|disabilit\w+|nurs(e|es|ing)|public health|health policy|health equity|patient advoca\w+|medicaid)\b",
    "domain_health": r"\b(health ?care|health|clinical|clinics?|clinicians?|patients?|medical|hospitals?|medicare|payers?|telehealth|behavioral|mental health|diabetes|chronic|diseases?|care (management|coordination)|caregiv\w+|hospice|pharma\w*|cancer|oncolog\w+|wellness)\b",
    "domain_adjacent": r"\b(children|famil(y|ies)|youth|education|schools?|policy|advocacy|legislat\w+|government|civic|human services|community|workforce|equity|social services|housing|early childhood|parents?)\b",
    "domain_far": r"\b(animals?|strays?|pets?|veterinar\w+|wildlife|oceans?|marine|climate|environmental|conservation|coffee|arts?|museums?|theat(er|re)|music|sports?|rowing|agricultur\w+|farm\w*|ag|librar(y|ies)|faith|church(es)?|ministry|crypto|fintech)\b",
    "skill_gaps": [
        [r"bilingual[^.;]{0,40}spanish|spanish[^.;]{0,25}(required|fluen\w+|proficien\w+)|fluen\w+ in[^.;]{0,25}spanish|spanish.speaking|native spanish", "Spanish fluency"],
        [r"\b(danish|german|mandarin|cantonese|chinese|japanese|korean|portuguese|arabic|hindi|dutch|italian|russian|polish|swedish|norwegian|hebrew|vietnamese|tagalog|haitian creole)[- ](speaking|speakers?|fluen\w+|language|proficien\w+)|(fluen\w+|proficien\w+) in (danish|german|mandarin|cantonese|chinese|japanese|korean|portuguese|arabic|hindi|dutch|italian|russian|polish|swedish|norwegian|hebrew|vietnamese|tagalog|haitian creole)", "another language"],
        [r"\b(cpa|gaap|general ledger|accounts payable|bookkeep\w+|month.end close)\b", "accounting"],
        [r"\b(javascript|typescript|react|kubernetes|terraform|software development experience|computer science degree)\b", "software engineering"],
        [r"\b(juris doctor|j\.?d\.?(?= (degree|required|from))|law degree|licensed attorney|bar admission|admitted to the bar)\b", "law degree"],
        [r"\b(graphic design|adobe (creative|illustrator|indesign|photoshop|premiere)|video editing|figma)\b", "design tools"],
    ],
    "gap_cost": 0.10,
    "sim_range": [0.035, 0.13],
    "coverage_range": [0.12, 0.45],
    "lexicon_full": 14,
    "fresh_floor": 55,
    "fresh_lead": 60,
}

REQ_HEAD = re.compile(r"(qualifications?|requirements?|what you(?:'ll| will)? bring|what we(?:'re| are) looking for|who you are|you have|skills (?:and|&) experience|experience (?:and|&) skills|ideal candidate|about you|must.haves?|required (?:skills|experience)|minimum (?:skills|experience|qualifications)|competenc(?:y|ies)|you(?:'ll| will) need)", re.I)
REQ_STOP = set("""about above across after again also among and any are around based been before being both bring but can candidate candidates
demonstrated desired each either etc excellent experience experienced including ideal into its knowledge least like may more most must need needs
other our own plus preferred proven qualifications related relevant required requirements requires role should skills some strong such than that the
their them these they this those through using well were what when where which while who will with within work working would year years you your
ability able have has for from equivalent degree bachelor bachelors minimum""".split())
YEARS_RX = re.compile(r"(?:minimum of |at least |a minimum of )?\b(\d{1,2})\s*(?:\+|plus|or more)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*years?(?:['’]|\b)[^.;:!?]{0,70}?\bexperience", re.I)


def _stem(w):
    for suf in ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ers", "er", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _terms(text):
    return {_stem(w) for w in re.findall(r"[a-z][a-z\-]{3,}", text.lower()) if w not in REQ_STOP}


def requirement_text(desc):
    """The part of a posting that lists what the employer wants: the text after a 'Qualifications' style heading,
    or the whole opening when no such heading is found."""
    desc = desc or ""
    best = None
    for m in REQ_HEAD.finditer(desc):
        if m.start() > 150:          # skip a heading word used in the first line of the intro
            best = m
            break
    if best:
        return desc[best.start(): best.start() + 2200], True
    return desc[:3000], False


def requirements_covered(desc, resume_terms):
    """Share (0-1) of the distinct requirement words that the matched resume also uses."""
    text, _ = requirement_text(desc)
    want = _terms(text)
    if len(want) < 12:
        return 0.5
    return len(want & resume_terms) / len(want)


def years_required(desc):
    vals = [int(m.group(1)) for m in YEARS_RX.finditer(desc or "")]
    vals = [v for v in vals if 1 <= v <= 25]
    return max(vals) if vals else None


def _distinct(rx, text):
    return {(m.group(0) or "").lower() for m in rx.finditer(text)}


class Scorer:
    def __init__(self, targets, M, focus):
        cfg = dict(SCORING_DEFAULTS)
        cfg.update(targets.get("scoring") or {})
        self.cfg, self.M, self.focus = cfg, M, focus
        self.W = cfg["weights"]
        self.levels = [(int(k), re.compile(v, re.I)) for k, v in sorted(cfg["levels"].items(), reverse=True)]
        self.core = re.compile(cfg["domain_core"], re.I)
        self.health = re.compile(cfg["domain_health"], re.I)
        self.adjacent = re.compile(cfg["domain_adjacent"], re.I)
        self.far = re.compile(cfg["domain_far"], re.I)
        self.gaps = [(re.compile(rx, re.I), label) for rx, label in cfg["skill_gaps"]]

    SOFT = re.compile(r"prefer\w+|a plus|bonus|nice to have|desir\w+|an asset|helpful|advantage|not required|ideally|welcome", re.I)

    def _hard(self, rx, text):
        """True when the requirement text asks for the thing outright, not as a preference."""
        for m in rx.finditer(text):
            if not self.SOFT.search(text[max(0, m.start() - 50): m.end() + 70]):
                return True
        return False

    # -- skills overlap: how much of what the posting asks for shows up in the best-matching resume
    def skills(self, j, sim, hits, resume_terms):
        mix = self.cfg["skills_mix"]
        (s0, s1), (c0, c1) = self.cfg["sim_range"], self.cfg["coverage_range"]
        sim_n = max(0.0, min(1.0, (sim - s0) / (s1 - s0)))
        lex_n = min(1.0, sum(w for w, _ in hits) / float(self.cfg["lexicon_full"]))
        cov = requirements_covered(j.get("desc", ""), resume_terms)
        cov_n = max(0.0, min(1.0, (cov - c0) / (c1 - c0)))
        frac = mix["resume_similarity"] * sim_n + mix["skill_lexicon"] * lex_n + mix["requirements_covered"] * cov_n
        notes = []
        req, _ = requirement_text(j.get("desc", ""))
        gaps = [label for rx, label in self.gaps if rx.search(j["title"]) or self._hard(rx, req)]
        if gaps:
            frac = max(0.0, frac - self.cfg["gap_cost"] * min(3, len(gaps)))
            notes.append("asks for " + ", ".join(gaps))
        j["gaps"] = gaps
        j["req_cov"] = round(cov, 2)
        labels = [label for _, label in hits[:3]]
        note = (", ".join(labels) if labels else "few of your listed skills named") + f"; {round(cov * 100)}% of the requirement wording is in the matched resume"
        if notes:
            note += "; " + "; ".join(notes)
        return self.W["skills"] * frac, note

    # -- seniority fit: the level of the title against director / executive level, the years asked for, junior pay
    def level(self, title):
        t = re.sub(r"\bassociate (director|vice president|vp)\b", r"\1", title, flags=re.I)
        if re.search(r"\b(case|care) manager\b", t, re.I) and not re.search(r"\b(director|lead|supervisor|head|chief|vp|manager of|managers)\b", t, re.I):
            return 2   # case and care managers (senior ones too) carry a caseload; the title is not a management seat
        for lv, rx in self.levels:
            if rx.search(t):
                return lv
        return 2

    def seniority(self, j, pay):
        cfg = self.cfg
        lv = self.level(j["title"])
        pts = float(cfg["level_points"][str(lv)])
        names = {5: "executive title", 4: "director-level title", 3: "manager or senior consultant title", 2: "individual-contributor title", 1: "entry-level title"}
        note = [names[lv]]
        small = is_nonprofit(j) or re.search(r"\b(fractional|interim)\b", (j.get("etype_raw") or "") + " " + j["title"], re.I)
        if lv == 5 and not small:
            pts = float(cfg["exec_at_large_employer"])
            note.append("executive seat at a larger employer")
        yrs = years_required(j.get("desc", ""))
        mine = cfg["candidate_years"]
        if yrs:
            if yrs > mine + 3:
                pts -= 10; note.append(f"asks {yrs}+ years")
            elif yrs <= 2 and lv <= 3:
                pts -= 5; note.append(f"asks only {yrs} year{'s' if yrs > 1 else ''}")
            elif yrs <= 4 and lv <= 3:
                pts -= 2; note.append(f"asks {yrs} years")
            else:
                note.append(f"asks {yrs}+ years")
        m_hr = re.search(r"\$(\d{2,3})(?: - \$(\d{2,3}))?/hr", pay or "")
        if m_hr and int(m_hr.group(2) or m_hr.group(1)) < cfg["junior_pay_hourly"]:
            pts = min(pts, 8.0); note.append("pay points to a junior role")
        j["level"] = lv
        j["years_required"] = yrs
        return max(0.0, min(float(self.W["seniority"]), pts * self.W["seniority"] / 25.0)), "; ".join(note)

    # -- domain fit: the same title in the wrong industry is a different job
    def domain(self, j, lens):
        name = j["title"] + " | " + (j.get("company") or "")
        head = j.get("desc", "")[:3000]
        tc, hc = bool(self.core.search(name)), _distinct(self.core, head)
        th, hh = bool(self.health.search(name)), _distinct(self.health, head)
        ad = _distinct(self.adjacent, name + " " + head)
        far_name, far_head = bool(self.far.search(name)), _distinct(self.far, head)
        if tc or len(hc) >= 3:
            pts, tier = 20, "your home ground"
        elif len(hc) >= 2 or (hc and (th or len(hh) >= 3)):
            pts, tier = 17, "close to your home ground"
        elif th or len(hh) >= 4:
            pts, tier = 15, "health sector"
        elif hc or len(hh) >= 2:
            pts, tier = 12, "touches health"
        elif len(ad) >= 3 and not far_name:
            pts, tier = 9, "neighboring cause area"
        elif far_name or len(far_head) >= 2:
            pts, tier = 4, "different sector"
        else:
            pts, tier = 7, "general nonprofit work" if is_nonprofit(j) else "general"
        if (far_name or len(far_head) >= 3) and not (tc or th):
            pts, tier = min(pts, 5), "different sector"
        if not self.focus:
            pts = min(20, pts + (2 if "AI health tech" in lens else 0) + (2 if "nonprofit / mission-driven" in lens else 0))
        seen = sorted(hc | (hh if pts >= 12 else set()))[:3] if pts >= 12 else sorted(far_head | (ad if pts >= 9 else set()))[:3]
        note = tier + (": " + ", ".join(seen) if seen else "")
        return pts * self.W["domain"] / 20.0, note

    # -- practical fit: location, how the work is arranged, pay, travel
    def practical(self, j, pay):
        note = []
        sp = self.M.state_penalty(j)
        loc = 6 if sp == 0 else 3 if sp <= 6 else 0
        note.append("open across the US" if sp == 0 else "tied to another state" if sp <= 6 else "must live in another state")
        frac = re.search(r"\b(fractional|interim)\b", (j.get("etype_raw") or "") + " " + j["title"], re.I)
        kind = 4 if j["etype"] in ("contract", "part_time") else 2 if j["etype"] == "full_time" else 1
        note.append("fractional or interim" if frac else {"contract": "contract", "part_time": "part-time", "full_time": "full-time", "unknown": "type not stated"}[j["etype"]])
        payp = 3
        if pay:
            low = False
            m_k = re.search(r"\$(\d{2,3})K", pay)
            if m_k and int(m_k.group(1)) < 60 and "/hr" not in pay and "/mo" not in pay and j["etype"] == "full_time":
                low = True
            m_hr = re.search(r"\$(\d{2,3})(?: - \$(\d{2,3}))?/hr", pay)
            if m_hr and int(m_hr.group(2) or m_hr.group(1)) < self.cfg["junior_pay_hourly"]:
                low = True
            strong = bool(m_hr and int(m_hr.group(2) or m_hr.group(1)) >= 45) or bool(re.search(r"/mo", pay)) or bool(m_k and int(m_k.group(1)) >= 90)
            payp = 1 if low else 5 if strong else 4
            note.append("low pay" if low else "pay posted")
        else:
            note.append("pay not posted")
        pts = loc + kind + payp
        if re.search(r"travel (up to )?(5\d|6\d|7\d|8\d|9\d|100)%", j.get("desc", ""), re.I):
            pts -= 2; note.append("heavy travel")
        return max(0.0, pts) * self.W["practical"] / 15.0, ", ".join(note)


# ----------------------------------------------------------------------------- db dump readers
def read_db(db_dir, before_date=None):
    """Returns (listed: {id: {first_date, last_date, title, company}}, status: {id: status}, resume_cache: {id: mtimeMs}).
    Days on or after before_date are ignored, so re-running a date does not count its own list as already shown."""
    listed, status, rcache = {}, {}, {}
    if not db_dir or not os.path.isdir(db_dir):
        return listed, status, rcache
    ddir = os.path.join(db_dir, "days")
    if os.path.isdir(ddir):
        for fn in sorted(os.listdir(ddir)):
            if not fn.endswith(".json"):
                continue
            try:
                d = json.load(open(os.path.join(ddir, fn), encoding="utf-8"))
            except Exception:
                continue
            d = d.get("data", d)
            date = d.get("date") or fn[:-5]
            if before_date and date >= before_date:
                continue
            for j in d.get("jobs", []):
                e = listed.setdefault(j["id"], {"first_date": date, "last_date": date, "title": j.get("title"), "company": j.get("company"), "times": 0})
                e["last_date"] = max(e["last_date"], date)
                e["first_date"] = min(e["first_date"], date)
                e["times"] += 1
    sdir = os.path.join(db_dir, "status")
    if os.path.isdir(sdir):
        for fn in os.listdir(sdir):
            if not fn.endswith(".json"):
                continue
            try:
                d = json.load(open(os.path.join(sdir, fn), encoding="utf-8"))
            except Exception:
                continue
            d = d.get("data", d)
            for k, v in d.items():
                if isinstance(v, dict) and v.get("status"):
                    status[k] = v["status"]
                elif isinstance(v, str):
                    status[k] = v
    rdir = os.path.join(db_dir, "resumes")
    if os.path.isdir(rdir):
        for fn in os.listdir(rdir):
            if fn.endswith(".json"):
                try:
                    d = json.load(open(os.path.join(rdir, fn), encoding="utf-8"))
                    d = d.get("data", d)
                    rcache[d.get("id") or fn[:-5]] = d.get("mtimeMs")
                except Exception:
                    pass
    return listed, status, rcache


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("scan")
    s.add_argument("--resumes", default="")
    s.add_argument("--db-dir", default="")
    s.add_argument("--out", required=True)
    s.add_argument("--date", default=dt.date.today().isoformat())
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--min-score", type=int, default=50)
    s.add_argument("--targets", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.json"))
    s.add_argument("--companies", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "companies.json"))
    s.add_argument("--max-age-days", type=int, default=45)
    s.add_argument("--extra", default="", help="JSON list of extra postings to score alongside the fetched ones")
    s.add_argument("--workers", type=int, default=12)
    s.add_argument("--raw-cache", default="", help="tuning aid: pickle of the fetched postings; loaded when the file exists, written otherwise")
    a = ap.parse_args()
    if a.cmd != "scan":
        ap.print_help()
        sys.exit(1)

    global TITLE_EXCLUDE
    targets = json.load(open(a.targets, encoding="utf-8"))
    companies = json.load(open(a.companies, encoding="utf-8"))
    TITLE_EXCLUDE = re.compile(targets["exclude_title"], re.I)
    M = Matcher(targets)
    focus = targets.get("focus") or {}
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    # ---- fetch everything
    tasks = []
    names = {}
    src_tags = {}
    watched = set()   # "watch": true in companies.json: list every matching remote-US role, whatever the focus block says
    for b in companies["boards"]:
        fn = {"greenhouse": src_greenhouse, "lever": src_lever, "ashby": src_ashby}[b["ats"]]
        tasks.append((b["ats"] + ":" + b["slug"], fn, (b["slug"],), True))
        names[b["ats"] + ":" + b["slug"]] = b.get("name")
        src_tags[b["ats"] + ":" + b["slug"]] = b.get("tags", [])
        if b.get("watch"):
            watched.add(b["ats"] + ":" + b["slug"])
    for w in companies.get("workday", []):
        tasks.append(("workday:" + w["tenant"], src_workday, (w["tenant"], w["wd"], w["site"]), True))
        names["workday:" + w["tenant"]] = w.get("name")
        src_tags["workday:" + w["tenant"]] = w.get("tags", [])
        if w.get("watch"):
            watched.add("workday:" + w["tenant"])
    for name, fn in (("remotive", src_remotive), ("remoteok", src_remoteok), ("jobicy", src_jobicy),
                     ("himalayas", src_himalayas), ("workingnomads", src_workingnomads), ("weworkremotely", src_wwr),
                     ("techjobsforgood", src_techjobsforgood), ("fractionaljobs", src_fractionaljobs), ("idealist", src_idealist),
                     ("philanthropy", src_philanthropy)):
        tasks.append((name, fn, (), False))

    stats = {}
    raw = []
    def run(t):
        name, fn, args, health_co = t
        try:
            jobs = fn(*args)
            for j in jobs:
                j["health_company"] = health_co
                if names.get(name):
                    j["company"] = names[name]
                j["tags"] = sorted(set(j.get("tags", [])) | set(src_tags.get(name, [])))
                j["watched"] = name in watched
            return name, jobs, ""
        except Exception as e:
            return name, [], f"{type(e).__name__}: {str(e)[:120]}"
    if a.raw_cache and os.path.exists(a.raw_cache):
        import pickle
        stats, raw = pickle.load(open(a.raw_cache, "rb"))
    else:
        with cf.ThreadPoolExecutor(a.workers) as ex:
            for name, jobs, err in ex.map(run, tasks):
                stats[name] = {"fetched": len(jobs), "error": err, "matched": 0}
                raw.extend(jobs)
        if a.raw_cache:
            import pickle
            pickle.dump((stats, raw), open(a.raw_cache, "wb"))
    if a.extra and os.path.exists(a.extra):
        for x in json.load(open(a.extra, encoding="utf-8")):
            x.setdefault("source", "manual"); x.setdefault("location", "Remote, US"); x.setdefault("desc", ""); x.setdefault("etype_raw", "")
            x["posted"] = parse_date(x.get("posted")); x.setdefault("remote_flag", True); x.setdefault("pay", ""); x["health_company"] = True; x.setdefault("tags", [])
            raw.append(x)
        stats["manual"] = {"fetched": len(json.load(open(a.extra, encoding="utf-8"))), "error": "", "matched": 0}
    total_fetched = len(raw)

    # ---- filter: remote US + title family + freshness
    today = dt.date.fromisoformat(a.date)
    SC = Scorer(targets, M, focus)   # built early: the filter needs its title-level test
    also = focus.get("also_list") or {}
    also_fams = set(also.get("families") or [])
    also_min = int(also.get("min_level", 3))
    cands = []
    reject = {}
    focus_out = []   # near misses dropped by the focus filter, written to focus_rejects.json for tuning
    for j in raw:
        if not j.get("title") or not j.get("url"):
            continue
        ok, why = M.is_remote_us(j)
        if not ok:
            reject[why] = reject.get(why, 0) + 1
            continue
        # a watched employer's live postings count however old they are (evergreen roles stay open for months)
        if j.get("posted") and (today - j["posted"]).days > a.max_age_days and not j.get("watched"):
            reject["too old"] = reject.get("too old", 0) + 1
            continue
        # development, grants and fundraising seats are never listed when targets.json says so; a posting below executive
        # level that is mostly fundraising language goes the same way, whatever its title says
        if M.fundraising_title(j["title"]):
            reject["development / fundraising"] = reject.get("development / fundraising", 0) + 1
            continue
        fam = M.family(j, j.get("health_company", False))
        if not fam:
            reject["no title match"] = reject.get("no title match", 0) + 1
            continue
        if M.ex_desc.search(j.get("desc", "")[:6000]):
            reject["requires other license"] = reject.get("requires other license", 0) + 1
            continue
        j["family"], j["family_w"] = fam
        j["etype"] = M.employment(j)
        j["level"] = SC.level(j["title"])
        if M.fundraising_heavy(j):
            if j["level"] >= 5:
                j["fundraising_heavy"] = True   # an executive seat: listed, with a badge
            else:
                reject["development / fundraising"] = reject.get("development / fundraising", 0) + 1
                continue
        if j.get("watched"):
            # watched employer: full-time and for-profit roles are wanted too, so the focus filter does not apply
            cands.append(j)
            continue
        if also_fams and j["family"] in also_fams and j["level"] >= also_min and not (
                j["level"] == 3 and re.search(r"\b(coordinator|nurse|rn|reviewer|specialist|analyst|advocate|navigator|associate|assistant|representative|auditor|coder)\b", j["title"], re.I)
                and not re.search(r"\b(manager|supervisor|director|head|chief)\b", j["title"], re.I)):
            # remote clinical leadership (care management, UM, clinical ops, nurse executive and so on) at manager
            # level or above rides alongside the nonprofit part-time list, full-time and for-profit included; a
            # "lead" or "senior" in front of a staff title (Lead Clinical Appeals Coordinator) is not a management seat
            j["also_listed"] = True
            cands.append(j)
            continue
        if focus.get("part_time_only") and not M.part_time_ok(j):
            reject["full-time or hours not stated"] = reject.get("full-time or hours not stated", 0) + 1
            if j["etype"] in ("contract", "part_time") or is_nonprofit(j) and j["etype"] == "unknown":
                focus_out.append({"reason": "full-time or hours not stated", "title": j["title"], "company": j["company"], "source": j["source"], "type": j["etype"], "etype_raw": j.get("etype_raw", ""), "url": j["url"]})
            continue
        if focus.get("require_nonprofit") and not is_nonprofit(j):
            reject["not a nonprofit"] = reject.get("not a nonprofit", 0) + 1
            focus_out.append({"reason": "not a nonprofit", "title": j["title"], "company": j["company"], "source": j["source"], "type": j["etype"], "etype_raw": j.get("etype_raw", ""), "url": j["url"]})
            continue
        cands.append(j)

    # ---- dedupe (same company + title across sources; prefer the company's own board)
    pri = {"greenhouse": 0, "lever": 0, "ashby": 0, "workday": 1, "manual": 1, "fractionaljobs": -1, "idealist": 2, "philanthropy": 2, "jobicy": 2, "himalayas": 2, "remotive": 3, "workingnomads": 3, "weworkremotely": 3, "remoteok": 3}
    byid = {}
    for j in cands:
        jid = job_id(j["company"], j["title"], j["url"])
        j["id"] = jid
        p = pri.get(j["source"].split(":")[0], 5)
        if jid not in byid or p < byid[jid][0]:
            byid[jid] = (p, j)
    cands = [v[1] for v in byid.values()]

    # ---- resume fit
    resumes, resume_origin = load_resumes(a.resumes, a.db_dir)
    fits = resume_fit(cands, resumes)

    # ---- score
    listed, status, rcache = read_db(a.db_dir, a.date)
    rterms = [_terms(r["text"]) for r in resumes]
    for j, (rlabel, rfile, sim, ridx) in zip(cands, fits):
        text = j["title"] + "\n" + j.get("desc", "")
        hits = M.skills_hits(text[:15000])
        age = (today - j["posted"]).days if j.get("posted") else None
        # roles posted within the last day (today or yesterday) are the priority. Freshness is not part of the
        # score: fresh roles get reserved slots and lead the list instead.
        j["fresh"] = age is not None and age <= 1
        pay = j.get("pay") or pay_from_text(j.get("desc", ""))
        # ---- lenses (badges on the page; outside the focused scan they add a little to domain fit)
        tags = set(j.get("tags", []))
        head = j.get("desc", "")[:2500]
        ai_title = re.search(r"\b(ai|artificial intelligence|llm|llms|genai|machine learning|ml|model|models|agentic|automation)\b", j["title"], re.I)
        ai_body = len(re.findall(r"\b(ai|artificial intelligence|llm|llms|genai|generative|machine learning|foundation model|ambient|agentic)\b", head, re.I))
        np_body = NP_RX.search(head)
        lens = []
        if "ai" in tags or ai_title or ai_body >= 3:
            lens.append("AI health tech")
        if "nonprofit" in tags or np_body or (focus.get("require_nonprofit") and is_nonprofit(j)):
            lens.append("nonprofit / mission-driven")
        if j.get("watched"):
            lens.append("Watched employer")
        if j.get("also_listed"):
            lens.append(also.get("label") or "Clinical leadership")
        if j.get("fundraising_heavy"):
            lens.append("Fundraising heavy")
        # chart-review flavored titles are capped on the list; a manager-or-above seat over those teams is not chart review
        if j["level"] <= 2 and re.search(r"\b(utilization|prior auth\w*|appeals?|medical review|chart|abstraction|abstractor|coding|coder|cdi|documentation integrity|case manager|care manager|claims? review|denials?|audit(or)?)\b", j["title"], re.I):
            j["chart_review"] = True
        j["lens"] = lens
        # ---- the four parts: skills overlap 40, seniority fit 25, domain fit 20, practical fit 15
        p_sk, n_sk = SC.skills(j, sim, hits, rterms[ridx] if ridx >= 0 else set())
        p_se, n_se = SC.seniority(j, pay)
        p_do, n_do = SC.domain(j, lens)
        p_pr, n_pr = SC.practical(j, pay)
        parts = {"skills": int(round(p_sk)), "seniority": int(round(p_se)), "domain": int(round(p_do)), "practical": int(round(p_pr))}
        if re.search(r"\b(senior|sr\.?|staff|principal) (software|data|ml|machine)", j["title"], re.I):
            parts = {k: 0 for k in parts}
        j["parts"] = parts
        j["part_notes"] = {"skills": n_sk, "seniority": n_se, "domain": n_do, "practical": n_pr}
        j["score"] = sum(parts.values())
        j["resume"] = rlabel
        j["resume_file"] = rfile
        j["resume_sim"] = round(sim, 3)
        j["why"] = [label for _, label in hits[:6]]
        j["pay"] = pay
        j["age_days"] = age
        j["status"] = status.get(j["id"], "")
        j["previously_listed"] = j["id"] in listed
        j["first_listed"] = listed.get(j["id"], {}).get("first_date")

    # ---- select today's list
    type_label = {"contract": "Contract / 1099", "part_time": "Part-time", "full_time": "Full-time", "unknown": "Not stated"}
    # a role that misses on seniority is one the candidate would be turned down for in one line, whatever else it scores
    min_sen = SC.cfg["min_seniority"]
    n_sen_out = sum(1 for j in cands if j["parts"]["seniority"] < min_sen and not j.get("watched"))
    # a watched employer's roles are listed whatever they score; the score still shows
    eligible = [j for j in cands if (j.get("watched") or (j["score"] >= a.min_score and j["parts"]["seniority"] >= min_sen)) and not j["status"]]
    new = [j for j in eligible if not j["previously_listed"]]
    old = [j for j in eligible if j["previously_listed"]]
    def order(js):
        # contract and part-time already carry a large bonus in the score; sort by score,
        # breaking ties in favor of contract / part-time
        return sorted(js, key=lambda j: (-j["score"], 0 if j["etype"] in ("contract", "part_time") else 1))
    chosen, per_co = [], {}
    chart_cap = max(4, a.limit // 3)          # at most a third of the list is chart-review flavored
    lens_reserve = max(3, a.limit // 4)       # reserve slots for AI health tech and for nonprofit roles when available
    fresh_reserve = max(3, a.limit // 4)      # reserve slots for decent roles posted within the last day (score floor keeps weak ones out)
    fresh_floor = max(a.min_score, SC.cfg["fresh_floor"])
    def take(j):
        c = norm(j["company"])
        if per_co.get(c, 0) >= 3 or j in chosen:
            return False
        if j.get("chart_review") and sum(1 for x in chosen if x.get("chart_review")) >= chart_cap:
            return False
        per_co[c] = per_co.get(c, 0) + 1
        chosen.append(j)
        return True
    ranked = order(new) + order(old)
    # first pass: the freshest postings (within the last day), best score first
    n = 0
    for j in ranked:
        if n >= fresh_reserve or len(chosen) >= a.limit:
            break
        if j.get("fresh") and j["score"] >= fresh_floor and take(j):
            n += 1
    # watched employers next, so their roles keep a slot on a crowded day (the per-company cap of 3 still applies)
    for j in ranked:
        if len(chosen) >= a.limit:
            break
        if j.get("watched"):
            take(j)
    for lens_name in ("AI health tech", "nonprofit / mission-driven", also.get("label") or "Clinical leadership"):
        n = 0
        for j in ranked:
            if n >= lens_reserve or len(chosen) >= a.limit:
                break
            if lens_name in j.get("lens", []) and take(j):
                n += 1
    # keep a couple of the strongest RN / chart-review roles so they never disappear entirely
    # (ones already taken by the fresh or lens passes count toward the two)
    n = sum(1 for x in chosen if x.get("chart_review"))
    for j in ranked:
        if n >= 2 or len(chosen) >= a.limit:
            break
        if j.get("chart_review") and take(j):
            n += 1
    for j in ranked:
        if len(chosen) >= a.limit:
            break
        take(j)
    # strong roles posted within the last day lead the list; everything else follows by score
    chosen = sorted(chosen, key=lambda j: (1 if j["previously_listed"] else 0, 0 if (j.get("fresh") and j["score"] >= SC.cfg["fresh_lead"]) else 1, -j["score"], 0 if j["etype"] in ("contract", "part_time") else 1))

    for j in cands:
        stats[j["source"] if j["source"] in stats else j["source"].split(":")[0]] = stats.get(j["source"], {"fetched": 0, "error": "", "matched": 0})
    for j in cands:
        key = j["source"] if j["source"] in stats else None
        if key:
            stats[key]["matched"] += 1

    def clean_loc(loc):
        parts = re.split(r"\s*[|;/·]\s*|\s{2,}|(?<=\S)\s+(?=remote\b)", loc or "", flags=re.I)
        keys, out = [], []
        for p in parts:
            p = p.strip(" ,-")
            k = re.sub(r"[^a-z]", "", p.lower())
            if not p or any(k == x or k in x for x in keys):
                continue
            keys.append(k); out.append(p)
        return " · ".join(out)[:120]

    def pack(j):
        return {
            "id": j["id"], "title": j["title"].strip(), "company": j["company"].strip(), "url": j["url"],
            "source": j["source"].split(":")[0], "location": clean_loc(j.get("location")),
            "type": j["etype"], "type_label": M.flex_label(j) if j["etype"] in ("contract", "part_time") else type_label[j["etype"]], "pay": j["pay"],
            "posted": j["posted"].isoformat() if j.get("posted") else "", "age_days": j["age_days"],
            "score": j["score"], "parts": j["parts"], "parts_max": SC.W, "part_notes": j["part_notes"], "family": j["family"], "resume": j["resume"], "resume_file": j["resume_file"],
            "why": j["why"], "repeat": bool(j["previously_listed"]), "first_listed": j.get("first_listed") or a.date,
            "snippet": re.sub(r"\s+", " ", j.get("desc", ""))[:280],
            "gaps": j.get("gaps", []), "years_required": j.get("years_required"),
            "lens": j.get("lens", []), "tags": j.get("tags", []), "chart_review": bool(j.get("chart_review")),
            "fresh": bool(j.get("fresh")), "watched": bool(j.get("watched")), "clinical_leadership": bool(j.get("also_listed")),
        }

    day = {
        "date": a.date,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(chosen),
        "new_count": sum(1 for j in chosen if not j["previously_listed"]),
        "repeat_count": sum(1 for j in chosen if j["previously_listed"]),
        "contract_count": sum(1 for j in chosen if j["etype"] in ("contract", "part_time")),
        "fresh_count": sum(1 for j in chosen if j.get("fresh")),
        "watched_count": sum(1 for j in chosen if j.get("watched")),
        "clinical_leadership_count": sum(1 for j in chosen if j.get("also_listed")),
        "fractional_count": sum(1 for j in chosen if re.search(r"\b(fractional|interim)\b", (j.get("etype_raw") or "") + " " + j["title"], re.I)),
        "focus": focus.get("label", ""),
        "jobs": [pack(j) for j in chosen],
        "stats": {
            "total_fetched": total_fetched, "remote_us_candidates": len(cands), "eligible": len(eligible),
            "seniority_mismatch": n_sen_out, "weights": SC.W,
            "sources_ok": sum(1 for s in stats.values() if not s["error"]), "sources_failed": sum(1 for s in stats.values() if s["error"]),
            "failed_sources": [k for k, v in stats.items() if v["error"]][:30],
            "rejects": reject, "resumes_on_file": len(resumes), "resume_source": resume_origin,
            "seconds": int(time.time() - t0),
        },
    }
    json.dump(day, open(os.path.join(a.out, "day.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"date": a.date, "last_run": day["generated_at"], "count": day["count"], "new_count": day["new_count"],
               "contract_count": day["contract_count"], "fresh_count": day["fresh_count"], "watched_count": day["watched_count"], "clinical_leadership_count": day["clinical_leadership_count"], "fractional_count": day["fractional_count"], "focus": day["focus"], "resumes_on_file": len(resumes), "resume_source": resume_origin,
               "total_fetched": total_fetched, "sources_ok": day["stats"]["sources_ok"], "sources_failed": day["stats"]["sources_failed"]},
              open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8"), indent=1)
    json.dump([pack(j) for j in sorted(cands, key=lambda j: -j["score"])], open(os.path.join(a.out, "candidates.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    json.dump(focus_out, open(os.path.join(a.out, "focus_rejects.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # resume docs that are new or changed vs the database cache
    rw = os.path.join(a.out, "resume_writes")
    os.makedirs(rw, exist_ok=True)
    n_rw = 0
    if resume_origin == "laptop":
        for r in resumes:
            if rcache.get(r["id"]) != r.get("mtimeMs"):
                json.dump(r, open(os.path.join(rw, r["id"] + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
                n_rw += 1

    # summary
    lines = [f"# Job scan {a.date}" + (f" ({focus['label']})" if focus.get("label") else ""), "",
             f"{total_fetched} postings fetched from {len(stats)} sources ({day['stats']['sources_failed']} failed); "
             f"{len(cands)} remote-US title matches; {len(eligible)} eligible (score >= {a.min_score}, or a watched employer); listing {len(chosen)} "
             f"({day['new_count']} new, {day['repeat_count']} repeats, {day['contract_count']} contract/part-time, {day['fresh_count']} posted within the last day, {day['watched_count']} from watched employers, {day['clinical_leadership_count']} clinical leadership). "
             f"Resumes: {len(resumes)} ({resume_origin}); {n_rw} resume docs to write.", ""]
    for j in chosen:
        lens = (" | " + ", ".join(j["lens"])) if j.get("lens") else ""
        if j.get("fresh"):
            lens = " | NEW <24h" + lens
        pp = j["parts"]
        lines.append(f"- [{j['score']} = skills {pp['skills']} + seniority {pp['seniority']} + domain {pp['domain']} + practical {pp['practical']}] {j['title']} | {j['company']} | {M.flex_label(j) if j['etype'] in ('contract', 'part_time') else type_label[j['etype']]} | {j['pay'] or 'pay n/a'} | {j['family']}{lens} | use: {j['resume']} | {j['url']}")
    open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines))
    print("\nrejects:", reject)
    print("held back for seniority mismatch:", n_sen_out)
    print("failed sources:", day["stats"]["failed_sources"])


if __name__ == "__main__":
    main()
