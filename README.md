# Tee-time email alerts (Denver metro) on GitHub Actions

Every 5 minutes GitHub runs `teealert.py`, which polls the tee sheet of each course in `courses.json`
(across MemberSports, TeeItUp, foreUP, Quick18, ClubCaddie and EZLinks), and emails you when a tee time matching your filters opens up that
it hasn't already told you about. You still book manually (the site has a captcha).

## One-time setup (~15 minutes)

### 1. Create the repo
- github.com → New repository → name it e.g. `teealert` → **Public** → Create.
  (Public is deliberate: scheduled Actions are free and unlimited on public repos, but a private
  repo would blow through the 2,000 free minutes/month at 5-minute polling. Nothing sensitive is
  in the repo — your email credentials live in encrypted Secrets, not in files.)
- Upload these files, preserving the folder structure:
  `teealert.py`, `courses.json`, `README.md`, and `.github/workflows/teealert.yml`.
  Easiest: "Add file → Upload files" for the three top-level files, then
  "Add file → Create new file", type `.github/workflows/teealert.yml` as the name and paste the YAML.

### 2. Gmail app password
Alerts are sent *from* a Gmail account via SMTP. Use your personal Gmail (or make a throwaway one).
- Google Account → Security → 2-Step Verification must be on.
- Then search "App passwords" (myaccount.google.com/apppasswords) → create one named `teealert`
  → copy the 16-character password.

### 3. Add secrets
Repo → Settings → Secrets and variables → Actions → **New repository secret**, three times:

| name | value |
|---|---|
| `SMTP_USER` | the Gmail address sending alerts |
| `SMTP_PASS` | the 16-char app password |
| `ALERT_TO`  | where alerts go (can be a different address, or several separated by commas) |

### 4. Turn it on and test
- Repo → Actions tab → if prompted, "I understand my workflows, go ahead and enable them."
- Click **Tee-time alerts** → **Run workflow**. The first run emails every currently-open matching
  slot (so you get a baseline), commits `state.json`, and from then on only emails *new* openings.
- No email? Open the run log; `email not configured` means a secret name is misspelled;
  an SMTP auth error means the app password is wrong.

## Adding courses
Each entry in `courses` has a `platform` plus that platform's identifiers, then the common filters.

| platform | identifiers | where they come from |
|---|---|---|
| `membersports` | `golfClubId`, `golfCourseId`, `configurationTypeId` | the three numbers in `app.membersports.com/tee-times/A/B/C` (C is usually 0; 1 for 27-hole rotations like Kennedy) |
| `teeitup` | `alias`, `facilityId` | `https://<alias>.book.teeitup.com/?course=<facilityId>` |
| `foreup` | `courseId`, `scheduleId`, `bookingClass` | `foreupsoftware.com/index.php/booking/<courseId>/<scheduleId>`; bookingClass is the public/non-resident class id in the page source |
| `quick18` | `host`, `courseIds` | `https://<host>/teetimes/searchmatrix`; course ids are in the "Courses" dropdown (use the 18-hole one) |
| `ezlinks` | `host`, `courseId` | `https://<host>/search`; course ids come from `https://<host>/api/search/init` (the `Courses` list). Uses the view-tee-sheet endpoint, so no prices in alerts. |
| `clubcaddie` | `host`, `apikey`, `courseId` | `https://<host>/webapi/view/<apikey>/slots`; courseId is the hidden `CourseId` field on that page |

Common filters (all optional, defaults shown in the file):

| field | meaning |
|---|---|
| `days` | list of `Mon`…`Sun` |
| `earliest` / `latest` | 24h `HH:MM` tee-time window (inclusive) |
| `min_open_spots` | 1–4 seats required (e.g. 2 if you always play with your wife) |
| `eighteen_only` | skip 9-hole and back-nine starts (set `false` for executive courses like Heather Gardens) |
| `exclude_name` | regex; skip tee-time names matching it (e.g. `West` to skip Kennedy rotations using the West 9) |
| `bookable_only` | `true` = ignore slots flagged outside your booking window (Loyalty-only etc.) |
| `days_ahead` | how far out to scan; match the course's public booking window to avoid a daily flood of "new" far-out slots |
| `tier` | `1` immediate alerts, `2` twice-daily digest (see Tiers) |
| `disabled` | `true` to pause a course without deleting it |

### Tiers
Each course has a `tier`:
- **`tier: 1`** — polled every run (~5 min); any new opening is emailed immediately.
- **`tier: 2`** — polled only at the times in the top-level `tier2_digest_times` (default `["07:00","17:00"]`, Mountain);
  one "Digest:" email lists what's newly open since the previous digest. Add or change times freely
  (e.g. `["06:30","12:00","18:00"]`); a digest fires on the first run after each listed time.

Edit `courses.json` directly on GitHub; the next run picks it up automatically.

### Not pollable
- **Club Prophet (`*.cps.golf`)** — Indian Tree, Eagle Trace, Legacy Ridge, Walnut Creek — sits behind a Cloudflare browser challenge that blocks scripts.
- **Broadlands** uses Noteefy, which *is* a tee-time alert service: sign up on its booking page and pick your window there.

## Things to know
- GitHub's cron is best-effort: "every 5 minutes" usually means 5–10, occasionally longer at busy hours.
  Denver Golf's Loyalty release is what matters most; if you want sharper timing later, the same
  script runs unchanged on a $5 VPS or a Raspberry Pi at 1-minute intervals.
- GitHub pauses scheduled workflows in repos with no commits for 60 days. The bot's own
  `state.json` commits generally keep it alive, but if alerts stop, check the Actions tab for a
  "re-enable" banner.
- To re-alert on everything (e.g. after changing filters), delete `state.json` from the repo. Moving a course between tiers re-alerts once for that course.
- Polling volume is one request per weekend day scanned per course per run (~70 light requests every 5 min across 18 courses).
- `python3 teealert.py --dry` polls and prints without emailing or touching `state.json` — handy after editing the config.
