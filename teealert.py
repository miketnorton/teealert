#!/usr/bin/env python3
"""Poll golf tee sheets across several booking platforms and email when a matching tee time opens.

Platforms: membersports, teeitup, foreup, quick18, clubcaddie, ezlinks (see README for per-course fields).
Runs one poll cycle per invocation (designed for GitHub Actions cron).
Email via env vars / GitHub Secrets: SMTP_HOST (smtp.gmail.com), SMTP_PORT (587), SMTP_USER, SMTP_PASS, ALERT_TO,
  and optional SMTP_FROM (the From address, if it differs from SMTP_USER — needed for Brevo and similar relays)
Usage:  python3 teealert.py           # poll + email new openings
        python3 teealert.py --test    # send a test email
        python3 teealert.py --dry     # poll, print, no email, no state change
Tiers: courses with "tier": 1 alert immediately every run; "tier": 2 are polled and emailed as a
digest only at the times in "tier2_digest_times" (default 07:00 and 17:00 local).
A tier-2 course with "release_time": "HH:MM" is additionally polled on the first two runs after that time
each day and alerted immediately, to catch a booking window opening.
"""
import json, os, re, smtplib, sys, html, datetime as dt, pathlib
from email.message import EmailMessage
from urllib.parse import unquote
from zoneinfo import ZoneInfo
import requests

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "courses.json").read_text())
STATE_FILE = HERE / "state.json"
TZ = ZoneInfo(CONFIG.get("timezone", "America/Denver"))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
S = requests.Session()
S.headers["User-Agent"] = UA


def log(msg):
    print(f"{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def hhmm(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def to_minutes(s):
    h, m = map(int, s.split(":"))
    return h * 60 + m


# ---------------------------------------------------------------------------
# Adapters. Each takes (course_config, date) and yields dicts:
#   {minutes, name, open, price, holes, back_nine, bookable, id}
# ---------------------------------------------------------------------------

def membersports(c, day):
    hdr = {"Content-Type": "application/json", "Accept": "application/json",
           "x-api-key": "A9814038-9E19-4683-B171-5A06B39147FC",
           "Origin": "https://app.membersports.com", "Referer": "https://app.membersports.com/"}
    body = {"configurationTypeId": c.get("configurationTypeId", 0), "date": day.strftime("%Y-%m-%d"),
            "golfClubGroupId": 0, "golfClubId": c["golfClubId"], "golfCourseId": c["golfCourseId"],
            "groupSheetTypeId": 0, "memberProfileId": 0}
    r = S.post("https://api.membersports.com/api/v1/golfclubs/onlineBookingTeeTimes", headers=hdr, json=body, timeout=25)
    r.raise_for_status()
    for slot in r.json():
        for it in slot["items"]:
            name = it.get("name", "")
            nine = bool(it.get("isBackNine")) or bool(re.search(r"9\s*only|back nine|par[ -]?3|executive|footgolf", name, re.I))
            yield dict(minutes=slot["teeTime"], name=name, open=4 - int(it.get("playerCount", 0)),
                       price=it.get("price", 0), holes=9 if nine else 18, back_nine=nine,
                       bookable=not it.get("bookingNotAllowed"), id=it.get("teeTimeId"),
                       premium=float(it.get("premiumCharge") or 0))


def teeitup(c, day):
    r = S.get("https://phx-api-be-east-1b.kenna.io/v2/tee-times",
              params={"date": day.strftime("%Y-%m-%d"), "facilityIds": c["facilityId"]},
              headers={"x-be-alias": c["alias"], "Accept": "application/json"}, timeout=25)
    r.raise_for_status()
    data = r.json()
    for fac in data:
        for t in fac.get("teetimes", []):
            local = dt.datetime.fromisoformat(t["teetime"].replace("Z", "+00:00")).astimezone(TZ)
            if local.date() != day:
                continue
            rates = t.get("rates", [])
            holes = 18 if any(x.get("holes") == 18 for x in rates) else (rates[0].get("holes", 18) if rates else 18)
            fee = next((x for x in rates if x.get("holes") == holes), rates[0] if rates else {})
            cents = fee.get("greenFeeWalking") or fee.get("greenFeeCart") or 0
            yield dict(minutes=local.hour * 60 + local.minute, name=c["label"], open=int(t.get("maxPlayers", 0)),
                       price=cents / 100, holes=holes, back_nine=bool(t.get("backNine")),
                       bookable=True, id=t["teetime"])


def foreup(c, day):
    r = S.get("https://foreupsoftware.com/index.php/api/booking/times",
              params={"time": "all", "date": day.strftime("%m-%d-%Y"), "holes": "all", "players": 0,
                      "booking_class": c["bookingClass"], "schedule_id": c["scheduleId"],
                      "schedule_ids[]": c["scheduleId"], "specials_only": 0, "api_key": "no_limits"},
              headers={"Accept": "application/json", "Api-Key": "no_limits", "X-Requested-With": "XMLHttpRequest"},
              timeout=25)
    r.raise_for_status()
    for t in r.json():
        hh, mm = map(int, t["time"].split(" ")[1].split(":"))
        holes = 18 if t.get("available_spots_18", 0) else t.get("holes", 9)
        yield dict(minutes=hh * 60 + mm, name=t.get("schedule_name") or c["label"], open=int(t.get("available_spots", 0)),
                   price=t.get("green_fee_18") or t.get("green_fee") or 0, holes=holes,
                   back_nine=bool(t.get("teesheet_side_order", 1) > 1), bookable=True, id=t.get("start_front"))


def quick18(c, day):
    r = S.get(f"https://{c['host']}/teetimes/searchmatrix", params={"teedate": day.strftime("%Y%m%d")}, timeout=25)
    r.raise_for_status()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
        m = re.search(r'mtrxTeeTimes">\s*(\d{1,2}):(\d{2})\s*<div class="be_tee_time_ampm">(AM|PM)', row)
        if not m:
            continue
        hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        hh = hh % 12 + (12 if ap == "PM" else 0)
        cid = re.search(r"/teetimes/course/(\d+)/", row)
        if c.get("courseIds") and (not cid or int(cid.group(1)) not in c["courseIds"]):
            continue
        name = re.search(r'mtrxCourse">([^<]*)<', row)
        players = re.search(r'matrixPlayers">([^<]*)<', row)
        nums = [int(x) for x in re.findall(r"\d+", players.group(1))] if players else [1]
        prices = [float(p) for p in re.findall(r"mtrxPrice\">\$([\d.]+)", row)]
        nm = html.unescape(name.group(1).strip()) if name else c["label"]
        nine = bool(re.search(r"\b9\b|nine", nm, re.I))
        yield dict(minutes=hh * 60 + mm, name=nm, open=max(nums), price=min(prices) if prices else 0,
                   holes=9 if nine else 18, back_nine="back" in nm.lower(), bookable=bool(prices),
                   id=f"{day}-{hh:02d}{mm:02d}-{cid.group(1) if cid else ''}")


_cc_sessions = {}
def clubcaddie(c, day):
    key = (c["host"], c["apikey"])
    if key not in _cc_sessions:
        boot = S.get(f"https://{c['host']}/webapi/view/{c['apikey']}/slots",
                     params={"date": day.strftime("%m/%d/%Y"), "player": 1, "ratetype": "any",
                             "SetSessionIdInLocalStorage": "true"}, timeout=25)
        _cc_sessions[key] = boot.headers.get("Session-Id")
    sid = _cc_sessions[key]
    r = S.post(f"https://{c['host']}/webapi/TeeTimes", cookies={"PHPSESSID": sid},
               headers={"X-Requested-With": "XMLHttpRequest",
                        "Referer": f"https://{c['host']}/webapi/view/{c['apikey']}/slots"},
               data={"date": day.strftime("%m/%d/%Y"), "player": 1, "holes": "any", "fromtime": 4, "totime": 23,
                     "minprice": 0, "maxprice": 9999, "ratetype": "any", "HoleGroup": "front",
                     "CourseId": c["courseId"], "apikey": c["apikey"]}, timeout=30)
    r.raise_for_status()
    seen = set()
    for enc in re.findall(r'name="slot" value="([^"]+)"', r.text):
        try:
            s = json.loads(unquote(enc))
        except Exception:
            continue
        st = s.get("StartTime", "00:00:00")
        if st in seen:
            continue
        seen.add(st)
        hh, mm = int(st[:2]), int(st[3:5])
        plans = s.get("PricingPlan") or []
        r18 = [p.get("HoleRate_18") for p in plans if p.get("HoleRate_18")]
        r9 = [p.get("HoleRate_9") for p in plans if p.get("HoleRate_9")]
        yield dict(minutes=hh * 60 + mm, name=c["label"], open=int(s.get("PlayersAvailable", 0)),
                   price=min(r18) if r18 else (min(r9) if r9 else 0), holes=18 if r18 else 9,
                   back_nine=False, bookable=True, id=f"{day}-{st}")



_ez_sessions = {}
def ezlinks(c, day):
    """EZLinks/GolfNow: the public search API is WAF-blocked, but the 'view tee sheet' endpoint is not.
    Open seats show as empty Slot1..Slot4 strings. No pricing is available from this view."""
    host = c["host"]
    if host not in _ez_sessions:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
                             "Accept": "application/json, text/plain, */*", "Referer": f"https://{host}/search", "Origin": f"https://{host}"})
        sess.get(f"https://{host}/search", timeout=25)
        init = sess.get(f"https://{host}/api/search/init", timeout=25).json()
        _ez_sessions[host] = (sess, init["MasterSponsorID"], init["CsrfToken"])
    sess, sponsor, csrf = _ez_sessions[host]
    r = sess.get(f"https://{host}/api/teesheet/getteesheet", headers={"X-CSRF-Token": csrf},
                 params={"SponsorID": sponsor, "CourseID": c["courseId"], "TeeTimeDate": day.strftime("%m/%d/%Y")}, timeout=25)
    r.raise_for_status()
    for t in r.json().get("TeeTimes", []):
        m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", t["TeeTime"].strip(), re.I)
        if not m:
            continue
        hh = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        seats = [t.get(f"Slot{i}", "") or "" for i in range(1, 5)]
        if any("BLOCKED" in x for x in seats):
            continue
        open_ = sum(1 for x in seats if x.strip() == "")
        yield dict(minutes=hh * 60 + int(m.group(2)), name=c["label"], open=open_, price=None,
                   holes=c.get("holes", 18), back_nine=False, bookable=True, id=f"{day}-{hh:02d}{m.group(2)}")

ADAPTERS = {"membersports": membersports, "teeitup": teeitup, "foreup": foreup,
            "quick18": quick18, "clubcaddie": clubcaddie, "ezlinks": ezlinks}

# ---------------------------------------------------------------------------

def matching_openings(course):
    found = {}
    today = dt.datetime.now(TZ).date()
    lo, hi = to_minutes(course.get("earliest", "06:00")), to_minutes(course.get("latest", "11:59"))
    fetch = ADAPTERS[course.get("platform", "membersports")]
    for i in range(course.get("days_ahead", 14) + 1):
        day = today + dt.timedelta(days=i)
        if DAY_ABBR[day.weekday()] not in course.get("days", ["Sat", "Sun"]):
            continue
        try:
            slots = list(fetch(course, day))
        except Exception as e:
            log(f"[{course['label']}] fetch error {day}: {e}")
            continue
        for s in slots:
            if not (lo <= s["minutes"] <= hi):
                continue
            if course.get("eighteen_only", True) and (s["holes"] != 18 or s["back_nine"]):
                continue
            if course.get("exclude_name") and re.search(course["exclude_name"], s["name"], re.I):
                continue
            if s["open"] < course.get("min_open_spots", 1):
                continue
            if course.get("bookable_only") and not s["bookable"]:
                continue
            if course.get("no_premium") and s.get("premium"):
                continue
            key = f"{course['label']}|{day}|{s['minutes']}|{s['id']}"
            flag = "" if s["bookable"] else " (Loyalty window)"
            if s.get("premium"):
                flag += f" (+${s['premium']:.0f}/player advance fee)"
            price = f" @ ${float(s['price']):.0f}" if s.get("price") else ""
            found[key] = f"{day:%a %b %-d}  {hhmm(s['minutes']):>8}  {s['name']}  {s['open']} open{price}{flag}"
    return found


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 587)
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("ALERT_TO")
    if not (user and pw and to):
        log("email not configured (SMTP_USER/SMTP_PASS/ALERT_TO missing); would have sent:\n" + body)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("SMTP_FROM") or user, to
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(); s.login(user, pw); s.send_message(msg)
    log(f"emailed: {subject}")


def digest_due(state, now):
    """True if a tier-2 digest time has passed today that hasn't been sent yet."""
    last = state.get("tier2_last_digest")
    last = dt.datetime.fromisoformat(last).replace(tzinfo=TZ) if last else None
    for t in CONFIG.get("tier2_digest_times", ["07:00", "17:00"]):
        hh, mm = map(int, t.split(":"))
        sched = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= sched and (last is None or last < sched):
            return True
    return False


def poll_courses(courses, prev):
    """Poll a list of courses; return (all_found, sections, labels) with sections for the new ones."""
    found_all, sections, labels = {}, [], []
    for course in courses:
        found = matching_openings(course)
        found_all.update(found)
        fresh = {k: v for k, v in found.items() if k not in prev}
        log(f"[{course['label']}] {len(found)} matching open slot(s), {len(fresh)} new")
        if fresh:
            labels.append(course["label"])
            lines = [fresh[k] for k in sorted(fresh)]
            sections.append(f"{course['label']}\n{course.get('url','')}\n\n" + "\n".join(lines))
    return found_all, sections, labels


def email_sections(prefix, sections, labels, dry):
    if not sections:
        return
    n = sum(len(s.split("\n")) - 3 for s in sections)
    subject = f"⛳ {prefix}{n} new tee time(s): {', '.join(labels)}"
    body = "\n\n----------------\n\n".join(sections) + "\n\nBook fast — weekend mornings go quickly."
    if dry:
        print(f"\n=== {subject} ===\n{body}\n")
    else:
        send_email(subject, body)


def release_due(course, state, now):
    """Tier-2 course with a release_time: poll on the first two runs after that time each day."""
    rt = course.get("release_time")
    if not rt:
        return False
    hh, mm = map(int, rt.split(":"))
    sched = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < sched:
        return False
    done = state.setdefault("release_runs", {}).get(course["label"], {})
    return done.get("date") != str(now.date()) or done.get("count", 0) < 2


def run_once(dry=False):
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if "tier1" not in state:  # migrate old flat state
        state = {"tier1": {k: v for k, v in state.items() if "|" in k}, "tier2": {}, "tier2_last_digest": None}
    active = [c for c in CONFIG["courses"] if not c.get("disabled")]
    now = dt.datetime.now(TZ)

    tier1 = [c for c in active if c.get("tier", 1) == 1]
    found, sections, labels = poll_courses(tier1, state["tier1"])
    email_sections("", sections, labels, dry)
    state["tier1"] = found

    tier2 = [c for c in active if c.get("tier", 1) == 2]
    if tier2 and digest_due(state, now):
        found, sections, labels = poll_courses(tier2, state["tier2"])
        email_sections("Digest: ", sections, labels, dry)
        if not sections:
            log("tier-2 digest due: nothing new")
        state["tier2"] = found
        state["tier2_last_digest"] = now.replace(tzinfo=None).isoformat(timespec="minutes")
    else:
        # Release-time checks: tier-2 courses whose booking window just opened get an immediate alert.
        rel = [c for c in tier2 if release_due(c, state, now)]
        if rel:
            found, sections, labels = poll_courses(rel, state["tier2"])
            email_sections("Release: ", sections, labels, dry)
            for c in rel:  # refresh just these courses' entries so the next digest doesn't repeat them
                state["tier2"] = {k: v for k, v in state["tier2"].items() if not k.startswith(c["label"] + "|")}
                state["tier2"].update({k: v for k, v in found.items() if k.startswith(c["label"] + "|")})
                d = state["release_runs"].get(c["label"], {})
                cnt = d.get("count", 0) + 1 if d.get("date") == str(now.date()) else 1
                state["release_runs"][c["label"]] = {"date": str(now.date()), "count": cnt}
        skipped = len(tier2) - len(rel)
        if skipped:
            log(f"tier-2: {skipped} course(s) skipped until next digest")

    if not dry:
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_email("⛳ Tee-time alert test", "If you're reading this, email alerts are working."); sys.exit()
    run_once(dry="--dry" in sys.argv)
