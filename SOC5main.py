import os
import logging
import io
import json
import time
import base64
import threading
from datetime import datetime, timedelta, timezone, date
import requests
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials as GSuiteCredentials
from PIL import Image, ImageDraw, ImageFont
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

print("### BOT STARTING ###")
BOT_START_TIME = time.time()

# ==================== GOOGLE CREDENTIALS ====================
GOOGLE_CREDENTIALS_FILE = "service_account.json"
_google_creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if _google_creds_json:
    with open(GOOGLE_CREDENTIALS_FILE, "w") as f:
        f.write(_google_creds_json)

# ==================== CONFIG ====================
PH_TIMEZONE = timezone(timedelta(hours=8))

APP_ID            = os.getenv("APP_ID", "")
APP_SECRET        = os.getenv("APP_SECRET", "")
ADMIN_CODE        = os.getenv("ADMIN_CODE", "")
ADMIN_CODE_2      = os.getenv("ADMIN_CODE_2", "")   # second incharge for keeper alerts

# ── SESSION ALERT CADENCE ──
# A state change only escalates to an alert once it's held steady for
# SESSION_DEBOUNCE_SECONDS — this prevents a flaky session that blips bad
# on one request out of 22 (then recovers on the next) from producing a
# burst of rapid expired/restored alert pairs. Once genuinely escalated,
# the down alert repeats at most once every SESSION_ALERT_REPEAT_SECONDS.
_fms_session_raw_valid     = True   # last raw per-request result observed
_fms_session_state_since   = time.time()   # when that raw state began
_fms_session_alerted_valid = True   # the state we've actually sent an alert for
_last_session_alert_sent   = 0.0
SESSION_DEBOUNCE_SECONDS     = 300       # 5 min of sustained state before alerting
SESSION_ALERT_REPEAT_SECONDS = 30 * 60   # 30 min repeat while still down
SESSION_ALERT_COOLDOWN       = 30 * 60   # 30 min min gap between ANY two session alerts

# Toggle: also broadcast session-down / session-restored messages to the
# SeaTalk GROUP (not just the admin DMs), so any team member can see the
# keeper needs a refresh. Set GROUP_NOTIFY_ON_SESSION_ISSUE=true to enable —
# defaults to off so these alerts stay DM-only to ADMIN_CODE / ADMIN_CODE_2.
GROUP_NOTIFY_ON_SESSION_ISSUE = os.getenv("GROUP_NOTIFY_ON_SESSION_ISSUE", "false").strip().lower() == "true"

FMS_COOKIE        = os.getenv("FMS_COOKIE", "").replace("\n", "").replace("\r", "").strip()
FMS_CSRF_TOKEN    = os.getenv("FMS_CSRF_TOKEN", "").replace("\n", "").replace("\r", "").strip()
SPREADSHEET_ID    = os.getenv("SPREADSHEET_ID", "")
OT_SPREADSHEET_ID = os.getenv("OT_SPREADSHEET_ID", "")
RENDER_URL        = os.getenv("RENDER_URL", "")

FMS_ATTENDANCE_URL = "https://spx.shopee.ph/api/wfm/admin/attendance/clock/statistic_data_list"
FMS_STATE_FILE     = "fms_state.json"
CONFIG_SHEET       = "Config"

# ── OT SHEET CONFIG ──
OT_SHEET_ID    = "127t6dR6elGWd_JkzZpCfSezNyQhioTwp3P1i-cUmjfI"   # new OT responses sheet
SCHED_SHEET_ID = "1CnrimRbvg4aInBK8bM67J9Ulh4Bw0UIq_-6sPK_uPMA"
SCHED_GID      = "528022402"
SCHED_HEADER_ROW = 0
SCHED_DATE_START = date(2026, 5, 26)
SCHED_DATE_END   = date(2026, 6, 25)

MONTH_MAP_SCHED = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

# New shift type labels for the OT responses sheet
SCHED_CODE_MAP = {
    "SOC 5|DS":    "Regular Day",
    "SOC 5|NS":    "Regular Day",
    "OT|DS|SOC5":  "Rest Day OT",
    "OT|NS|SOC5":  "Rest Day OT",
    "RD":          "Rest Day (no work)",
}

# Leave suffix → display label for any "SOC 8|DS/NS <suffix>" code
LEAVE_SUFFIX_MAP = {
    "VL":  "Vacation Leave (VL)",
    "SL":  "Sick Leave (SL)",
    "FCL": "Family Care Leave (FCL)",
    "CL":  "Compassionate Leave (CL)",
    "NPL": "No Pay Leave (NPL)",
    "PL":  "Paternity Leave (PL)",
    "OL":  "Others Leave (OL)",
}

def is_leave_code(sched_code: str):
    """Return (True, leave_label) for any 'SOC 8|DS/NS <suffix>' leave code."""
    code = sched_code.strip()
    for prefix in ("SOC 8|DS ", "SOC 8|NS "):
        if code.startswith(prefix):
            suffix = code[len(prefix):].strip().upper()
            label  = LEAVE_SUFFIX_MAP.get(suffix, f"ON LEAVE ({suffix})")
            return True, label
    return False, ""

# ── ROSTER ROLES ──
# Keyed by Ops ID (lowercased) rather than name, since names can be typed
# or shortened multiple different ways — Ops ID is the one stable
# identifier already used everywhere else in this file.
ROSTER_ROLE_MAP = {
    "ops181178": "Planner",     # Patrick Arvin Habla
    "ops232397": "Planner",     # Teody Aldueza
    "ops181706": "Facilities",  # Erick T. Valdez
    "ops232398": "Facilities",  # Michael Ignacio
    "ops265884": "MFM",         # Carvin Cayetano
    "ops181199": "Lead",        # Elvin John Asiado
    "ops181188": "Lead",        # Ralph Victor Año
    "ops181207": "Lead",        # Jayson Biñas
    "ops181177": "Lead",        # Neil Algire
}
ROSTER_DEFAULT_ROLE = "FTE"
# How each non-FTE role's line is formatted in the roster message.
ROSTER_ROLE_LINE_FORMAT = {
    "Planner":    "Planner - {names}",
    "Facilities": "Facilities - {names}",
    "MFM":        "MFM- {names}",
    "Lead":       "Lead - {names}",
}
# Roster display uses first names only — most people's first word works
# fine, but a few go by a two-word first name. Add exceptions here as
# needed (match on the full registered name, lowercased).
ROSTER_FIRST_NAME_OVERRIDES = {
    "john michael aquino": "John Michael",
}

def casual_first_name(full_name: str) -> str:
    """First-name-only display for the roster (e.g. 'Ralph', 'John Michael')."""
    if not full_name:
        return full_name
    override = ROSTER_FIRST_NAME_OVERRIDES.get(full_name.strip().lower())
    if override:
        return override
    return full_name.strip().split()[0]

# New 7-column layout for OT responses sheet (row 3 headers):
# ACTUAL IN | Shopee Email Address | Date of Rendered Overtime |
# SHIFT TYPE | Shift Start | Shift End | ACTUAL OUT
HEADERS_OT = [
    "ACTUAL IN",                    # A
    "Shopee Email Address",         # B
    "Date of Rendered Overtime",    # C
    "SHIFT TYPE",                   # D
    "Shift Start",                  # E
    "Shift End",                    # F
    "ACTUAL OUT",                   # G
    "Remarks",                      # H — visible, manual correction note
    "Hidden",                       # I — hidden True/False flag
]

def round_to_half_hour(dt: datetime) -> datetime:
    """Round a datetime to the nearest :00 or :30 mark, matching the OT
    sheet's 30-minute interval. 08:03:34 -> 08:00:00, 07:38:05 -> 07:30:00."""
    discard = timedelta(minutes=dt.minute % 30, seconds=dt.second, microseconds=dt.microsecond)
    rounded = dt - discard
    if discard >= timedelta(minutes=15):
        rounded += timedelta(minutes=30)
    return rounded

# Dynamic cutoffs: 11-25 and 26-10 monthly, computed at runtime
def get_ot_cutoffs(ref_date=None):
    """Returns current and previous cutoff windows based on today."""
    from datetime import date as _date
    if ref_date is None:
        ref_date = datetime.now(PH_TIMEZONE).date()
    day = ref_date.day
    yr  = ref_date.year
    mo  = ref_date.month
    if day <= 10:
        # current: 26th prev month → 10th this month
        prev_mo = mo - 1 if mo > 1 else 12
        prev_yr = yr if mo > 1 else yr - 1
        cur  = {"label": f"26/{prev_mo:02d}–10/{mo:02d}", "start": _date(prev_yr, prev_mo, 26), "end": _date(yr, mo, 10)}
        # previous: 11th–25th of month before that
        pp_mo = prev_mo - 1 if prev_mo > 1 else 12
        pp_yr = prev_yr if prev_mo > 1 else prev_yr - 1
        prev = {"label": f"11/{pp_mo:02d}–25/{pp_mo:02d}", "start": _date(pp_yr, pp_mo, 11), "end": _date(prev_yr, prev_mo, 25)}
    elif day <= 25:
        # current: 11th–25th this month
        cur  = {"label": f"11/{mo:02d}–25/{mo:02d}", "start": _date(yr, mo, 11), "end": _date(yr, mo, 25)}
        # previous: 26th prev month → 10th this month
        prev_mo = mo - 1 if mo > 1 else 12
        prev_yr = yr if mo > 1 else yr - 1
        prev = {"label": f"26/{prev_mo:02d}–10/{mo:02d}", "start": _date(prev_yr, prev_mo, 26), "end": _date(yr, mo, 10)}
    else:
        # current: 26th this month → 10th next month
        next_mo = mo + 1 if mo < 12 else 1
        next_yr = yr if mo < 12 else yr + 1
        cur  = {"label": f"26/{mo:02d}–10/{next_mo:02d}", "start": _date(yr, mo, 26), "end": _date(next_yr, next_mo, 10)}
        # previous: 11th–25th this month
        prev = {"label": f"11/{mo:02d}–25/{mo:02d}", "start": _date(yr, mo, 11), "end": _date(yr, mo, 25)}
    return prev, cur

# ── LEAVE TYPES ──
LEAVE_TYPES = {
    "sl":  "Sick Leave (SL)",
    "vl":  "Vacation Leave (VL)",
    "fcl": "Family Care Leave (FCL)",
    "cl":  "Compassionate Leave (CL)",
    "npl": "No Pay Leave (NPL)",
    "pl":  "Paternity Leave (PL)",
    "ol":  "Others Leave (OL)",
}
LEAVE_ALIASES = {
    "sick leave": "sl",
    "vacation leave": "vl",
    "family care leave": "fcl",
    "compassionate leave": "cl",
    "no pay leave": "npl",
    "paternity leave": "pl",
    "others leave": "ol",
}

# ── INTERNAL FMS POLLER ──
# The bot polls FMS directly every N seconds so clock-in alerts fire in real time
# without depending on an external push-attendance call.
FMS_POLL_INTERVAL = int(os.getenv("FMS_POLL_INTERVAL", "20"))  # seconds

# ========== TRACKED USERS BY OPS ID ==========
OPS_IDS_ENV = os.getenv("OPS_IDS", "")
TRACKED_OPS_IDS = [oid.strip() for oid in OPS_IDS_ENV.split(",") if oid.strip()]
print(f"✅ Loaded {len(TRACKED_OPS_IDS)} tracked Ops IDs from env")

OPS_ID_FIELD = os.getenv("OPS_ID_FIELD", "staff_employee_id")

MANAGER_NAMES = [
    "Carmelo Amboy",
    "Rotsen Camanag",
    "Lau Binas",
]

# ==================== FILES ====================
LAST_TIMEIN_FILE  = "last_timein.json"
LAST_TIMEOUT_FILE = "last_timeout.json"

def _load(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def _save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

SHIFT_CACHE_FILE = "shift_cache.json"
shift_cache_map  = _load(SHIFT_CACHE_FILE)

# ==================== GOOGLE SHEETS ====================
USERS_SHEET_NAME = "Users"
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]

def _get_sheets_client():
    creds = GSuiteCredentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SHEETS_SCOPE)
    return gspread.authorize(creds)

def shorten_name(full_name):
    if not full_name or not isinstance(full_name, str):
        return full_name
    parts = full_name.strip().split()
    if len(parts) <= 2:
        return full_name.strip()
    return f"{parts[0]} {parts[-1]}"

# ==================== ATTENDANCE DATE HELPER ====================
def get_attendance_date(ti=None, to=None, is_timeout=False):
    ts = ti if ti else to
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts, PH_TIMEZONE)
    # Night-shift clock-outs (before 13:00) belong to the previous calendar day
    if is_timeout and dt.hour < 13:
        dt = dt - timedelta(days=1)
    elif not is_timeout and dt.hour < 5:
        # Only roll back genuine post-midnight clock-ins (00:00–04:59).
        # 05:xx is early day shift and stays on the current calendar day.
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")

def classify_shift(timestamp):
    dt = datetime.fromtimestamp(timestamp, PH_TIMEZONE)
    h = dt.hour
    if 6 <= h < 14:
        return "Day Shift"
    elif 14 <= h < 22:
        return "Mid Shift"
    else:
        return "Night Shift"

def get_user_sheet(email_or_name):
    """Look up the member's tracker tab.

    Tab naming has been inconsistent across members in two different ways:
    some are titled with the full name instead of the shortened form (e.g.
    the tab is literally "Elvin John Asiado", not "Elvin Asiado"), and some
    use different casing (ALL CAPS "ELVIN ASIADO" vs Title Case "Mark
    Millanar"). gspread's worksheet() requires an exact match, so this
    tries the full name and the shortened name, each first exact then
    case-insensitively, before giving up.
    """
    full_name  = (email_or_name or "").strip()
    short_name = shorten_name(full_name)
    candidates = [full_name] if full_name == short_name else [full_name, short_name]

    try:
        client = _get_sheets_client()
        sheet  = client.open_by_key(SPREADSHEET_ID)
    except Exception as e:
        print(f"[Sheet] Error getting user sheet: {e}")
        return None

    for tab_name in candidates:
        if not tab_name:
            continue
        try:
            return sheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            continue
        except Exception as e:
            print(f"[Sheet] Error getting user sheet: {e}")
            return None

    # Exact match failed for every candidate — try case-insensitively
    # before giving up entirely.
    try:
        for ws in sheet.worksheets():
            title_lower = ws.title.strip().lower()
            if any(title_lower == c.strip().lower() for c in candidates if c):
                return ws
    except Exception as e:
        print(f"[Sheet] Error getting user sheet: {e}")
        return None

    print(f"[Sheet] Tab not found for '{email_or_name}' (tried: {candidates}) – skipping write.")
    return None

def get_shift_label(ts):
    dt   = datetime.fromtimestamp(ts, PH_TIMEZONE)
    hour = dt.hour
    if dt.minute > 10:
        hour += 1
    if hour >= 24:
        hour -= 24
    if hour == 0:
        return "12AM"
    elif hour < 12:
        return f"{hour}AM"
    elif hour == 12:
        return "12PM"
    else:
        return f"{hour - 12}PM"

def write_clock_record(email_or_name, clock_in_time=None, clock_out_time=None):
    try:
        ws = get_user_sheet(email_or_name)
        if not ws:
            print(f"[Sheet] ⛔ No tracker tab for '{email_or_name}' — clock write skipped")
            return

        all_rows = ws.get_all_values()

        if clock_out_time and not clock_in_time:
            record_date = get_attendance_date(None, clock_out_time, is_timeout=True)
            target_row  = None
            for i, row in enumerate(all_rows[1:], start=2):
                if row and len(row) > 0 and row[0].strip() == record_date:
                    target_row = i
                    break
            if not target_row:
                print(f"[Sheet] ⛔ No row found for {record_date} ('{email_or_name}') "
                      f"to write clock-out into — skipping (row may not exist yet, "
                      f"or clock-in write hasn't landed)")
                return
            existing_row = all_rows[target_row - 1]
            existing_out = existing_row[4].strip() if len(existing_row) > 4 else ""
            if existing_out:
                print(f"[Sheet] ⛔ Row for {record_date} ('{email_or_name}') already has "
                      f"a clock-out ({existing_out}) — not overwriting")
                return
            clock_out_str = datetime.fromtimestamp(clock_out_time, PH_TIMEZONE).strftime("%H:%M:%S")
            ws.update_cell(target_row, 5, clock_out_str)
            print(f"[Sheet] ✅ Clock-out {clock_out_str} written to row {target_row} "
                  f"({record_date}, '{email_or_name}')")
            return

        record_date  = get_attendance_date(clock_in_time, clock_out_time, is_timeout=False)
        if not record_date:
            return
        clock_in_str  = datetime.fromtimestamp(clock_in_time, PH_TIMEZONE).strftime("%H:%M:%S") if clock_in_time else ""
        clock_out_str = datetime.fromtimestamp(clock_out_time, PH_TIMEZONE).strftime("%H:%M:%S") if clock_out_time else ""
        shift_source  = clock_in_time if clock_in_time else clock_out_time
        shift         = get_shift_label(shift_source)
        target_row    = None
        for i, row in enumerate(all_rows[1:], start=2):
            if row and len(row) > 0 and row[0] == record_date:
                target_row = i
                break
        if target_row:
            existing    = all_rows[target_row - 1]
            existing_in  = existing[3] if len(existing) > 3 else ""
            existing_out = existing[4] if len(existing) > 4 else ""
            if existing_in and existing_out:
                print(f"[Sheet] ⛔ Skip complete row {record_date}")
                return
            if clock_in_str and not existing_in:
                ws.update_cell(target_row, 4, clock_in_str)
                ws.update_cell(target_row, 2, shift)
            if clock_out_str and not existing_out:
                ws.update_cell(target_row, 5, clock_out_str)
            print(f"[Sheet] ✅ Updated row {record_date}")
        else:
            ws.append_row([record_date, shift, "SOC8", clock_in_str, clock_out_str, "", ""])
            print(f"[Sheet] ✅ New row {record_date}")
    except Exception as e:
        print(f"[Sheet] Error: {e}")

# ── Schedule loader ──

# ─────── SOC5 name normalization (added by patch) ───────
import re
import unicodedata as _ud

def _strip_diacritics(s):
    return "".join(c for c in _ud.normalize("NFD", s)
                   if _ud.category(c) != "Mn")

def _norm_sched_name(raw):
    """Normalize a schedule name for fuzzy matching against Users tab."""
    if not raw:
        return ""
    s = raw.strip()
    s = re.sub(r"\s*\([^)]*\)", "", s, flags=re.I)  # strip ANY trailing/embedded paren
    s = re.sub(r"\s*SOC\d+\s*$", "", s, flags=re.I)
    s = s.strip()
    s = _strip_diacritics(s).lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ─────── SOC5 manual name aliases (schedule → Users roster) ───────
# Keys must be the _norm_sched_name() output (lowercase, no punctuation,
# diacritics stripped). Values are the exact name as it appears in the
# Users tab.
NAME_ALIASES = {
    "aries escueta":      "Aris Escueta",
    "aris escueta":       "Aris Escueta",
    # add more here if the parser misses anyone else
}

def _match_sched_to_roster(sched_raw, roster_names):
    """Find the roster name that matches a schedule row name (fuzzy)."""
    if not sched_raw:
        return None
    sched_norm = _norm_sched_name(sched_raw)
    if not sched_norm:
        return None
    if sched_norm in NAME_ALIASES:
        return NAME_ALIASES[sched_norm]
    sched_tokens = sched_norm.split()
    sched_concat = sched_norm.replace(" ", "")
    # Pass 1: exact normalized match
    for n in roster_names:
        if _norm_sched_name(n) == sched_norm:
            return n
    # Pass 2: every roster token must appear in the schedule string;
    # require >= 2 tokens so a bare first name can't false-match.
    for n in roster_names:
        r_norm = _norm_sched_name(n)
        r_tokens = r_norm.split()
        if len(r_tokens) < 2:
            continue
        if all(any(tok in st or tok in sched_concat for st in sched_tokens)
               for tok in r_tokens):
            return n
    return None
# ─────── end SOC5 name normalization ───────

SCHED_NAMES = []  # populated after user load

def load_schedule_dynamic():
    try:
        client = _get_sheets_client()
        sh     = client.open_by_key(SCHED_SHEET_ID)
        target_ws = next(
            (ws for ws in sh.worksheets() if str(ws.id) == SCHED_GID),
            sh.get_worksheet(0),
        )
        all_rows = target_ws.get_all_values()

        # SOC5: member names are always in column A
        name_col = 0

        if SCHED_HEADER_ROW >= len(all_rows):
            raise RuntimeError("Header row out of range")
        date_header = all_rows[SCHED_HEADER_ROW]

        today_for_year = datetime.now(PH_TIMEZONE).date()

        def parse_date_cell(cell):
            cell = cell.strip()
            if not cell:
                return None
            parts = cell.split("-")
            if len(parts) != 2:
                return None
            try:
                day = int(parts[0])
                mon = MONTH_MAP_SCHED.get(parts[1].lower())
                if not mon:
                    return None
                # Cells only carry day/month, no year — pick whichever of
                # last/this/next year lands closest to today, so cells near
                # a Dec/Jan boundary resolve to the correct year.
                candidates = [date(today_for_year.year + off, mon, day) for off in (-1, 0, 1)]
                return min(candidates, key=lambda d: abs((d - today_for_year).days))
            except Exception:
                return None

        all_date_cols = []
        for ci, cell in enumerate(date_header):
            d = parse_date_cell(cell)
            if d:
                all_date_cols.append((ci, d))
        if not all_date_cols:
            raise RuntimeError("No date columns found")

        all_date_cols.sort(key=lambda x: x[0])
        blocks  = []
        current = [all_date_cols[0]]
        for prev, cur in zip(all_date_cols, all_date_cols[1:]):
            if cur[0] == prev[0] + 1 and (cur[1] - prev[1]).days == 1:
                current.append(cur)
            else:
                blocks.append(current)
                current = [cur]
        blocks.append(current)
        _today = datetime.now(PH_TIMEZONE).date()
        _active = next((b for b in blocks if b[0][1] <= _today <= b[-1][1]), None)
        _active = _active or max(blocks, key=lambda b: b[-1][1])
        date_col_map = {ci: d for ci, d in _active}

        schedule = {}
        _row_to_name = {}
        for ri, row in enumerate(all_rows):
            if ri < len(row) and name_col < len(row):
                canonical = _match_sched_to_roster(row[name_col], SCHED_NAMES)
                if canonical:
                    _row_to_name[ri] = canonical
        for name in SCHED_NAMES:
            candidate_rows = [ri for ri, nm in _row_to_name.items() if nm == name]
            if not candidate_rows:
                continue
            best_ri    = None
            best_count = -1
            for ri in candidate_rows:
                row   = all_rows[ri]
                count = sum(1 for ci in date_col_map if ci < len(row) and row[ci].strip() not in ("", "-"))
                if count > best_count:
                    best_count = count
                    best_ri    = ri
            row = all_rows[best_ri]
            for ci, d in date_col_map.items():
                if ci < len(row):
                    code = row[ci].strip()
                    if code and code != "-":
                        schedule[(name, d)] = code
        return schedule
    except Exception as e:
        print(f"⚠️ Schedule load failed: {e}")
        return {}

# ==================== LEAVE PARSER ====================
def parse_leave_command(text: str):
    """
    Parses a leave command in this format (case-insensitive):
        leave
        SL          ← leave type code or full name
        06/19/2026  ← date (MM/DD/YYYY)

    Returns dict with keys: leave_code, leave_label, date_obj
    or None if the text doesn't match the format.
    """
    lines = [l.strip() for l in text.strip().split('\n')]
    if len(lines) < 3:
        return None
    if lines[0].lower() != "leave":
        return None

    raw_type = lines[1].strip().lower()
    leave_code = raw_type if raw_type in LEAVE_TYPES else LEAVE_ALIASES.get(raw_type)
    if not leave_code:
        return None

    try:
        leave_date = datetime.strptime(lines[2].strip(), "%m/%d/%Y").date()
    except ValueError:
        return None

    return {
        "leave_code":  leave_code,
        "leave_label": LEAVE_TYPES[leave_code],
        "date_obj":    leave_date,
    }

# ==================== MANUAL CLOCK PARSER ====================
def parse_manual_clock(text):
    lines = [l.strip() for l in text.strip().split('\n')]
    if len(lines) < 3:
        return None
    first_line = lines[0].strip().lower()
    if first_line not in ('time in', 'timein', 'time out', 'timeout'):
        return None
    date_str = lines[1].strip()
    time_str = lines[2].strip()
    try:
        date_obj = datetime.strptime(date_str, '%m/%d/%Y').date()
    except ValueError:
        return None
    time_clean = time_str.replace('H', '').replace('h', '')
    try:
        time_obj = datetime.strptime(time_clean, '%H%M').time()
    except ValueError:
        return None
    return {
        'type':     first_line,
        'date_obj': date_obj,
        'time_obj': time_obj,
    }

# ==================== GOOGLE SHEETS USERS ====================
def gsheet_load_users():
    try:
        client = _get_sheets_client()
        sheet  = client.open_by_key(SPREADSHEET_ID)
        try:
            ws = sheet.worksheet(USERS_SHEET_NAME)
        except Exception:
            ws = sheet.add_worksheet(USERS_SHEET_NAME, 100, 4)
            ws.append_row(["employee_code", "ops_id", "name", "email"])
            print("✅ Users sheet created (was missing)")
            return {}, {}, {}
        all_values = ws.get_all_values()
        if not all_values:
            return {}, {}, {}
        headers = all_values[0]
        if len(headers) < 4 or "email" not in headers:
            try:
                ws.update_cell(1, 1, "employee_code")
                ws.update_cell(1, 2, "ops_id")
                ws.update_cell(1, 3, "name")
                ws.update_cell(1, 4, "email")
                headers = ["employee_code", "ops_id", "name", "email"]
            except Exception as e:
                print(f"⚠️ Could not fix headers: {e}")
                return {}, {}, {}
        try:
            emp_idx   = headers.index("employee_code")
            ops_idx   = headers.index("ops_id")
            name_idx  = headers.index("name")
            email_idx = headers.index("email")
        except ValueError:
            print("❌ Missing required columns in Users sheet")
            return {}, {}, {}
        ops_ids = {}
        names   = {}
        emails  = {}
        loaded  = 0
        skipped = 0
        for row in all_values[1:]:
            if not row:
                continue
            emp_code = row[emp_idx].strip()  if emp_idx  < len(row) else ""
            ops_id   = row[ops_idx].strip()  if ops_idx  < len(row) else ""
            name     = row[name_idx].strip() if name_idx < len(row) else ""
            email    = row[email_idx].strip() if email_idx < len(row) else ""
            key      = emp_code if emp_code else ops_id
            if not key or not name:
                skipped += 1
                continue
            ops_ids[key] = ops_id
            names[key]   = name
            emails[key]  = email
            loaded += 1
        print(f"✅ Loaded {loaded} users ({skipped} skipped)")
        return ops_ids, names, emails
    except Exception as e:
        print(f"❌ gsheet_load_users error: {e}")
        return {}, {}, {}

def gsheet_save_user(emp_code, ops_id=None, name=None, email=None):
    for attempt in range(2):
        try:
            client = _get_sheets_client()
            ws     = client.open_by_key(SPREADSHEET_ID).worksheet(USERS_SHEET_NAME)
            rows   = ws.get_all_values()
            for i, row in enumerate(rows[1:], start=2):
                if row and row[0] == emp_code:
                    if ops_id is not None:
                        ws.update_cell(i, 2, ops_id)
                    if name is not None:
                        ws.update_cell(i, 3, name)
                    if email is not None:
                        ws.update_cell(i, 4, email)
                    print(f"✅ Updated user {emp_code}")
                    return
            ws.append_row([emp_code, ops_id or "", name or "", email or ""])
            print(f"✅ Appended new user {emp_code}")
            return
        except Exception as e:
            print(f"❌ gsheet_save_user error (attempt {attempt + 1}): {e}")
            if attempt == 0:
                time.sleep(2)

# ==================== FMS STATE -> GOOGLE SHEETS ====================
def save_fms_state_to_sheets(state_dict):
    try:
        client = _get_sheets_client()
        sheet  = client.open_by_key(SPREADSHEET_ID)
        try:
            ws = sheet.worksheet(CONFIG_SHEET)
        except Exception:
            ws = sheet.add_worksheet(CONFIG_SHEET, 20, 2)
            ws.append_row(["key", "value"])
        cookies_only = json.dumps({"cookies": state_dict.get("cookies", [])})
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0] == "fms_state":
                ws.update_cell(i, 2, cookies_only)
                print("✅ FMS state saved to Sheets")
                return
        ws.append_row(["fms_state", cookies_only])
        print("✅ FMS state saved to Sheets (new row)")
    except Exception as e:
        print(f"❌ save_fms_state_to_sheets error: {e}")

def load_fms_state_from_sheets():
    try:
        client = _get_sheets_client()
        sheet  = client.open_by_key(SPREADSHEET_ID)
        try:
            ws = sheet.worksheet(CONFIG_SHEET)
        except Exception:
            return None
        rows = ws.get_all_values()
        for row in rows[1:]:
            if row and row[0] == "fms_state" and len(row) > 1 and row[1]:
                print("✅ FMS state loaded from Sheets")
                return row[1]
        return None
    except Exception as e:
        print(f"❌ load_fms_state_from_sheets error: {e}")
        return None

# ==================== POLLER STATE -> GOOGLE SHEETS ====================
POLLER_STATE_KEY     = "poller_state"
_last_sheets_save    = 0
SHEETS_SAVE_INTERVAL = 60

# Decoupled from the event-firing rate on purpose: with 20+ members, a
# shift-start rush can fire many clock events within the same minute, and
# calling save_poller_state_to_sheets() once per event (each a Sheets read
# + write) burns through the Sheets API read quota fast. Local files
# (LAST_TIMEIN_FILE / LAST_TIMEOUT_FILE) are still written immediately on
# every fire for same-instance crash safety — this flag only gates the
# Sheets mirror, which exists to survive a Render redeploy.
_poller_state_dirty      = False
_poller_state_dirty_lock = threading.Lock()

DEDUP_KEY_MAX_AGE_DAYS = 3   # keys older than this serve no purpose — roster/dedup only ever look at today/yesterday

def _prune_stale_dedup_keys(map_dict):
    """Remove keys whose embedded date is older than DEDUP_KEY_MAX_AGE_DAYS.
    Key format: '{prefix}:{YYYY-MM-DD}:in' or '...:out'. Without this, these
    maps grow forever and eventually exceed Google Sheets' 50,000-character
    per-cell limit, which silently breaks save_poller_state_to_sheets for
    good (not just once — every future save fails the same way)."""
    cutoff  = (datetime.now(PH_TIMEZONE) - timedelta(days=DEDUP_KEY_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for key in list(map_dict.keys()):
        parts = key.rsplit(":", 2)
        if len(parts) != 3:
            continue
        if parts[1] < cutoff:
            del map_dict[key]
            removed += 1
    return removed

def _poller_state_saver_loop():
    """Background thread: persists the dedup state to Sheets at most once
    every SHEETS_SAVE_INTERVAL seconds, no matter how many events fired in
    between — guarantees it's never more than ~60s stale without ever
    bursting the read quota. Also prunes stale keys every cycle so the
    payload never grows past Sheets' cell size limit."""
    global _poller_state_dirty
    while True:
        time.sleep(SHEETS_SAVE_INTERVAL)
        with _poller_state_dirty_lock:
            dirty = _poller_state_dirty
            _poller_state_dirty = False
        if dirty:
            with data_locks["last_timein"], data_locks["last_timeout"]:
                removed_in  = _prune_stale_dedup_keys(last_timein_map)
                removed_out = _prune_stale_dedup_keys(last_timeout_map)
                if removed_in or removed_out:
                    print(f"[Poller] 🧹 Pruned {removed_in} stale time-in key(s), "
                          f"{removed_out} stale time-out key(s) (older than "
                          f"{DEDUP_KEY_MAX_AGE_DAYS}d)")
                save_poller_state_to_sheets(dict(last_timein_map), dict(last_timeout_map))
            print("[Poller] 💾 Dedup state synced to Sheets")

def start_poller_state_saver():
    t = threading.Thread(target=_poller_state_saver_loop, daemon=True)
    t.start()

def save_poller_state_to_sheets(timein_map, timeout_map):
    try:
        client  = _get_sheets_client()
        ws      = client.open_by_key(SPREADSHEET_ID).worksheet(CONFIG_SHEET)
        payload = json.dumps({"last_timein": timein_map, "last_timeout": timeout_map})
        rows    = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0] == POLLER_STATE_KEY:
                ws.update_cell(i, 2, payload)
                return
        ws.append_row([POLLER_STATE_KEY, payload])
    except Exception as e:
        print(f"❌ save_poller_state_to_sheets error: {e}")

def load_poller_state_from_sheets():
    try:
        client = _get_sheets_client()
        sheet  = client.open_by_key(SPREADSHEET_ID)
        try:
            ws = sheet.worksheet(CONFIG_SHEET)
        except Exception:
            return {}, {}
        rows = ws.get_all_values()
        for row in rows[1:]:
            if row and row[0] == POLLER_STATE_KEY and len(row) > 1 and row[1]:
                data = json.loads(row[1])
                print(f"✅ Poller state loaded ({len(data.get('last_timein',{}))} time-ins, {len(data.get('last_timeout',{}))} time-outs)")
                return data.get("last_timein", {}), data.get("last_timeout", {})
        return {}, {}
    except Exception as e:
        print(f"❌ load_poller_state_from_sheets error: {e}")
        return {}, {}

# ==================== STARTUP ====================
def load_fms_state_on_startup():
    state_str = load_fms_state_from_sheets()
    if state_str:
        with open(FMS_STATE_FILE, "w") as f:
            f.write(state_str)
        print("✅ fms_state.json written from Google Sheets")
        return
    fms_state_json = os.environ.get("FMS_STATE_JSON", "")
    if fms_state_json:
        with open(FMS_STATE_FILE, "w") as f:
            f.write(fms_state_json)
        print("✅ fms_state.json written from env var")
        try:
            save_fms_state_to_sheets(json.loads(fms_state_json))
        except Exception:
            pass
        return
    print("⚠️ No FMS state found — falling back to FMS_COOKIE")

load_fms_state_on_startup()
user_ops_id_map, user_name_map, user_email_map = gsheet_load_users()

# Build SCHED_NAMES from Users sheet
SCHED_NAMES = list(user_name_map.values())

_sheets_timein, _sheets_timeout = load_poller_state_from_sheets()
last_timein_map  = _sheets_timein  if _sheets_timein  else _load(LAST_TIMEIN_FILE)
last_timeout_map = _sheets_timeout if _sheets_timeout else _load(LAST_TIMEOUT_FILE)

processed_msgs = {}

data_locks = {
    "user_ops_id":    threading.Lock(),
    "user_name":      threading.Lock(),
    "last_timein":    threading.Lock(),
    "last_timeout":   threading.Lock(),
    "processed_msgs": threading.Lock(),
    "fms_session":    threading.Lock(),
}

# ==================== FLASK ====================
app = Flask(__name__)

# Quiet werkzeug's default per-request access logging (every health-check
# HEAD/GET from Render's own pings otherwise prints its own line, drowning
# out the actual application logs). Real errors still surface normally.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

@app.route("/")
def home():
    return "FMS Bot Running ✅", 200

# ==================== ADMIN: UPDATE FMS STATE ====================
@app.route("/admin/update-fms-state", methods=["POST"])
def admin_update_fms_state():
    body = request.get_json(silent=True) or {}
    if body.get("admin_code") != ADMIN_CODE:
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    fms_state_str = body.get("fms_state", "")
    if not fms_state_str:
        return jsonify({"ok": False, "error": "Missing fms_state"}), 400
    try:
        state_dict = json.loads(fms_state_str)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON in fms_state"}), 400
    with open(FMS_STATE_FILE, "w") as f:
        json.dump(state_dict, f)
    save_fms_state_to_sheets(state_dict)
    global _fms_session, _fms_session_raw_valid, _fms_session_state_since, _fms_session_alerted_valid
    with data_locks["fms_session"]:
        _fms_session = None
    # Keeper just explicitly refreshed it — assume good and reset the
    # debounce clock so a stale pending state doesn't linger.
    _fms_session_raw_valid     = True
    _fms_session_state_since   = time.time()
    was_down = not _fms_session_alerted_valid
    _fms_session_alerted_valid = True
    n_cookies = len(state_dict.get("cookies", []))
    print(f"✅ FMS state updated via /admin/update-fms-state ({n_cookies} cookies)"
          + (" (rescued a down session)" if was_down else ""))
    if was_down and ADMIN_CODE:
        # Only DM when the keeper refresh actually rescued an alerted-down
        # session — otherwise every 6h sync (even on a healthy session)
        # was a pointless notification.
        _send_keeper_alert(f"✅ FMS session refreshed by keeper! ({n_cookies} cookies loaded)", is_recovery=True)
    return jsonify({"ok": True, "cookies_loaded": n_cookies})

# ==================== ADMIN: PUSH ATTENDANCE ====================
# These fire once per *distinct* record, not once per push cycle — the
# same stuck OUT-only or duplicate-OUT record otherwise reprints every
# ~25s for as long as it stays inside FMS's query window (hours of
# identical spam for one anomaly).
_skip_warned_out_only  = set()
_skip_warned_dup_out   = set()

@app.route("/admin/push-attendance", methods=["POST"])
def admin_push_attendance():
    body = request.get_json(silent=True) or {}
    if body.get("admin_code") != ADMIN_CODE:
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    records = body.get("records", [])
    warmup  = body.get("warmup", False)
    if warmup:
        seeded = 0
        for r in records:
            ops_id = _extract_ops_id(r)
            if ops_id not in TRACKED_OPS_IDS:
                continue
            info       = _ops_id_lookup.get(ops_id.strip().lower(), {})
            email      = info.get("email") or r.get("staff_email", "").strip()
            key_prefix = email if email else ops_id
            ti = r.get("clock_in_time", 0)
            if ti:
                record_date = get_attendance_date(ti, None, is_timeout=False)
                key = f"{key_prefix}:{record_date}:in"
                with data_locks["last_timein"]:
                    if last_timein_map.get(key, 0) < ti:
                        last_timein_map[key] = ti
                        seeded += 1
            to = r.get("clock_out_time", 0)
            if to:
                record_date = get_attendance_date(None, to, is_timeout=True)
                key = f"{key_prefix}:{record_date}:out"
                with data_locks["last_timeout"]:
                    if last_timeout_map.get(key, 0) < to:
                        last_timeout_map[key] = to
                        seeded += 1
        print(f"✅ Warm-up push: seeded {seeded} entries from {len(records)} records")
        return jsonify({"ok": True, "mode": "warmup", "seeded": seeded})
    clean_records = []
    seen_out      = set()
    for r in records:
        ti = r.get("clock_in_time", 0)
        to = r.get("clock_out_time", 0)
        if not ti and to:
            if to not in _skip_warned_out_only:
                _skip_warned_out_only.add(to)
                print(f"[FILTER] ⛔ OUT-only skipped: {to} (further repeats of this record suppressed)")
            continue
        if to:
            if to in seen_out:
                if to not in _skip_warned_dup_out:
                    _skip_warned_dup_out.add(to)
                    print(f"[FILTER] ⛔ Duplicate OUT skipped: {to} (further repeats of this record suppressed)")
                continue
            seen_out.add(to)
        clean_records.append(r)

    # The keeper's 24h-window push is the only source that sees clock-outs
    # (the internal poller's window is too short to reach back from a
    # clock-in to its clock-out hours later). This path and the internal
    # poller share process_attendance_records() and its dedup lock, so
    # each event fires exactly once regardless of which source sees it.
    fired_in, fired_out = process_attendance_records(clean_records)

    return jsonify({"ok": True, "mode": "live", "records": len(clean_records),
                     "fired_in": fired_in, "fired_out": fired_out})

# ==================== SEATALK TOKEN ====================
_access_token = None
_token_expiry  = 0
_token_lock    = threading.Lock()

def get_app_token():
    global _access_token, _token_expiry
    with _token_lock:
        if _access_token and time.time() < _token_expiry - 300:
            return _access_token
        try:
            res  = requests.post(
                "https://openapi.seatalk.io/auth/app_access_token",
                json={"app_id": APP_ID, "app_secret": APP_SECRET},
                timeout=10,
            )
            data = res.json()
            if data.get("code") == 0:
                _access_token = data["app_access_token"]
                _token_expiry = data.get("expire", 0)
                print("✅ SeaTalk token refreshed")
                return _access_token
        except Exception as e:
            print(f"Token error: {e}")
        return None

# ==================== SENDERS ====================
def _send_keeper_alert(text, is_recovery=False):
    """DM the admin(s) about a session state change, and — if
    GROUP_NOTIFY_ON_SESSION_ISSUE is enabled — also post it to the SeaTalk
    group so the whole team can see the keeper needs attention.

    Cadence is controlled by the CALLER (see fms_get()): fires immediately
    on the down-transition, then at most once every
    SESSION_ALERT_REPEAT_SECONDS while still down, then once on recovery —
    never a flood."""
    if ADMIN_CODE:
        send_dm(ADMIN_CODE, text)
    if ADMIN_CODE_2:
        send_dm(ADMIN_CODE_2, text)
    if GROUP_NOTIFY_ON_SESSION_ISSUE:
        send_group(text, mention_all=not is_recovery)


def send_dm(code, text):
    token = get_app_token()
    if not token:
        return
    try:
        requests.post(
            "https://openapi.seatalk.io/messaging/v2/single_chat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "employee_code": code,
                "message": {"tag": "text", "text": {"content": text}},
                "usable_platform": "all",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"send_dm error: {e}")

def send_dm_image(code, image_b64):
    token = get_app_token()
    if not token:
        return
    try:
        requests.post(
            "https://openapi.seatalk.io/messaging/v2/single_chat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "employee_code": code,
                "message": {"tag": "image", "image": {"content": image_b64}},
                "usable_platform": "all",
            },
            timeout=15,
        )
    except Exception as e:
        print(f"send_dm_image error: {e}")

# ==================== SEATALK OPEN API CONFIG ====================
APP_ID     = "NDAxNjE5NDc3Njk0"
APP_SECRET = "5S9svb39C4N7iL2o8XpOkiwu4piHrmTk"
SEATALK_GROUP_ID = os.getenv("SEATALK_GROUP_ID", "NDk2NzMxMTU5NjU1")
# One or more group IDs, comma-separated. Every group message (time in/out,
# duty roster, etc.) is posted to ALL of them. Example:
#   SEATALK_GROUP_ID=MjQ0OTE0ODU1MTkw,NDk2NzMxMTU5NjU1
SEATALK_GROUP_IDS = [g.strip() for g in SEATALK_GROUP_ID.split(",") if g.strip()]
SEATALK_TOKEN_URL  = "https://openapi.seatalk.io/auth/app_access_token"
SEATALK_MSG_URL    = "https://openapi.seatalk.io/messaging/v2/group_chat"

seatalk_access_token    = None
seatalk_token_expire_at = 0

def seatalk_get_token():
    global seatalk_access_token, seatalk_token_expire_at
    now = time.time()
    if seatalk_access_token and now < seatalk_token_expire_at - 60:
        return seatalk_access_token
    print("[Token] Refreshing SeaTalk access token...")
    try:
        resp = requests.post(
            SEATALK_TOKEN_URL,
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data  = resp.json()
        token = data.get("app_access_token")
        if token:
            seatalk_access_token = token
            raw_expire = data.get("expire", 0)
            seatalk_token_expire_at = raw_expire if raw_expire > now else now + raw_expire
            print(f"[Token] ✅ Expires at: {datetime.fromtimestamp(seatalk_token_expire_at).strftime('%Y-%m-%d %H:%M:%S')}")
            return token
        print(f"[Token] ❌ Token missing: {data}")
    except Exception as e:
        print(f"[Token] ❌ Failed: {e}")
    return None

def send_group(message, mention_all=True):
    """Post to every group listed in SEATALK_GROUP_ID (comma-separated).
    A failure in one group never blocks the others."""
    for gid in SEATALK_GROUP_IDS:
        try:
            _send_group_to(gid, message, mention_all=mention_all)
        except Exception as e:
            print(f"[Group] ❌ Unexpected error for {gid}: {e}")

def _send_group_to(group_id, message, mention_all=True, _retry=False):
    token = seatalk_get_token()
    if not token:
        print("[Group] ❌ No SeaTalk token — skipping.")
        return
    content = message
    if mention_all:
        content += '\n<mention-tag target="seatalk://user?id=0"/>'
    payload = {
        "group_id": group_id,
        "message":  {"tag": "text", "text": {"format": 1, "content": content}},
    }
    try:
        resp = requests.post(
            SEATALK_MSG_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        code = data.get("code", 0)
        if code == 0:
            print(f"[Group] ✅ Message delivered to {group_id}")
            return
        if code == 100 and not _retry:
            global seatalk_access_token, seatalk_token_expire_at
            seatalk_access_token    = None
            seatalk_token_expire_at = 0
            _send_group_to(group_id, message, mention_all=mention_all, _retry=True)
            return
        if mention_all and not _retry:
            _send_group_to(group_id, message, mention_all=False, _retry=True)
            return
        print(f"[Group] ❌ SeaTalk error for {group_id} (code={code}): {data}")
    except requests.HTTPError as e:
        print(f"[Group] ❌ HTTP error: {e}")
        if not _retry:
            seatalk_access_token    = None
            seatalk_token_expire_at = 0
            _send_group_to(group_id, message, mention_all=mention_all, _retry=True)
    except Exception as e:
        print(f"[Group] ❌ Send failed: {e}")

# ==================== FORMATTERS ====================
def fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=PH_TIMEZONE).strftime("%H%M")

def fmt_date(ts):
    return datetime.fromtimestamp(ts, tz=PH_TIMEZONE).strftime("%A, %B %d, %Y")

def infer_shift_from_time(timestamp, is_timeout=False):
    dt   = datetime.fromtimestamp(timestamp, PH_TIMEZONE)
    hour = dt.hour
    if is_timeout:
        return "Night Shift" if hour < 12 else "Day Shift"
    return "Night Shift" if hour >= 18 else "Day Shift"

def get_greeting(shift):
    return "Good evening!" if "Night" in shift else "Good morning!"

def format_timein(name, ts, shift=None, station="SOC5"):
    if not name or name == "Team Member" or "Employee" in name:
        return None
    return (
        f"🟢 TIME IN\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}\n"
        f"⏰ {fmt_time(ts)}H | {fmt_date(ts)}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Reporting for duty. Thank you!"
    )

def format_timeout(name, ts, shift=None, station="SOC5"):
    return (
        f"🔴 TIME OUT\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}\n"
        f"⏰ {fmt_time(ts)}H | {fmt_date(ts)}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"End of shift. Thank you!"
    )

def format_leave(name, leave_label, leave_date_str):
    return (
        f"🟡 ON LEAVE\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}\n"
        f"📋 {leave_label}\n"
        f"📅 {leave_date_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Noted. Rest well and take care!"
    )

# ==================== ATTENDANCE IMAGE ====================
COLUMNS    = ["Date", "Shift", "Station", "Clock In", "Clock Out", "Plan H", "Actual H"]
COL_WIDTHS = [95, 65, 72, 88, 88, 62, 68]

def _load_fonts():
    for bold_path, reg_path in [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ]:
        try:
            return (ImageFont.truetype(bold_path, 13),
                    ImageFont.truetype(reg_path, 12),
                    ImageFont.truetype(bold_path, 14))
        except Exception:
            continue
    f = ImageFont.load_default()
    return f, f, f

def render_attendance_image(records, display_name):
    PAD = 14; ROW_H = 30; HEADER_H = 36; TITLE_H = 48
    rows = []
    for r in records:
        ci_ts  = r.get("clock_in_time", 0)
        ci_str = r.get("clock_in_time_str") or (datetime.fromtimestamp(ci_ts, tz=PH_TIMEZONE).strftime("%H:%M:%S") if ci_ts else "-")
        co_ts  = r.get("clock_out_time", 0)
        co_str = r.get("clock_out_time_str") or (datetime.fromtimestamp(co_ts, tz=PH_TIMEZONE).strftime("%H:%M:%S") if co_ts else "-")
        station  = r.get("station_name") or r.get("event_station_name") or "-"
        shift    = r.get("slot_code") or r.get("shift_name") or "-"
        plan_h   = str(r.get("planned_hours") or "-")
        actual_h = str(r.get("actual_hours")  or "-")
        rows.append([r.get("date", "-"), shift, station, ci_str, co_str, plan_h, actual_h])
    if not rows:
        return None
    font_bold, font_reg, font_title = _load_fonts()
    total_w = sum(COL_WIDTHS) + PAD * 2
    total_h = TITLE_H + HEADER_H + ROW_H * len(rows) + PAD
    img  = Image.new("RGB", (total_w, total_h), "#1a1a2e")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, total_w, TITLE_H], fill="#0f3460")
    draw.text((PAD, 14), f"My FMS Attendance  |  {display_name}", fill="#e0e0e0", font=font_title)
    y = TITLE_H
    draw.rectangle([0, y, total_w, y + HEADER_H], fill="#16213e")
    x = PAD
    for i, col in enumerate(COLUMNS):
        draw.text((x + 5, y + 10), col, fill="#00d4ff", font=font_bold)
        x += COL_WIDTHS[i]
    for ri, row in enumerate(rows):
        y  = TITLE_H + HEADER_H + ri * ROW_H
        draw.rectangle([0, y, total_w, y + ROW_H], fill="#1a1a2e" if ri % 2 == 0 else "#16213e")
        x = PAD
        for ci, cell in enumerate(row):
            color = "#ffd700" if ci == 0 else ("#ff6b6b" if ci == 4 and cell == "-" else "#ffffff")
            draw.text((x + 5, y + 8), str(cell), fill=color, font=font_reg)
            x += COL_WIDTHS[ci]
    for ri in range(len(rows) + 1):
        y = TITLE_H + HEADER_H + ri * ROW_H
        draw.line([(0, y), (total_w, y)], fill="#2a2a4a", width=1)
    x = PAD
    for w in COL_WIDTHS[:-1]:
        x += w
        draw.line([(x, TITLE_H), (x, total_h)], fill="#2a2a4a", width=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

# ==================== FMS SESSION ====================
_fms_session = None

def build_fms_session():
    s = requests.Session()
    s.headers.update({
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "accept":     "application/json, text/plain, */*",
        "referer":    "https://spx.shopee.ph/",
    })
    if os.path.exists(FMS_STATE_FILE):
        with open(FMS_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        for c in state.get("cookies", []):
            s.cookies.set(
                c["name"].strip(), c["value"].strip(),
                domain=c.get("domain", ".shopee.ph"),
                path=c.get("path", "/"),
            )
        csrf = s.cookies.get("csrftoken", "").strip()
        if csrf:
            s.headers["x-csrftoken"] = csrf
        print(f"✅ FMS session built ({len(state.get('cookies', []))} cookies)")
        return s
    if FMS_COOKIE:
        for part in FMS_COOKIE.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain=".shopee.ph", path="/")
        csrf = FMS_CSRF_TOKEN or s.cookies.get("csrftoken", "").strip()
        if csrf:
            s.headers["x-csrftoken"] = csrf.strip()
        print("✅ FMS session built from FMS_COOKIE")
        return s
    print("⚠️ No FMS credentials found")
    return s

def get_fms_session():
    global _fms_session
    with data_locks["fms_session"]:
        if _fms_session is None:
            _fms_session = build_fms_session()
        return _fms_session

def fms_get(params):
    global _fms_session_raw_valid, _fms_session_state_since, _fms_session_alerted_valid, _last_session_alert_sent
    session = get_fms_session()
    try:
        res = session.get(FMS_ATTENDANCE_URL, params=params, timeout=20)
        expired = res.status_code in (401, 403)
        if not expired:
            try:
                if res.json().get("retcode") != 0:
                    expired = True
            except Exception:
                pass

        raw_valid_now = not expired
        now = time.time()

        if raw_valid_now != _fms_session_raw_valid:
            # The raw per-request result just changed — reset the "how long
            # has it been steady" clock. A single blip (one bad request out
            # of 22 in a poll cycle, or one good one right after) does NOT
            # get alerted on its own; it only counts once it HOLDS.
            _fms_session_raw_valid   = raw_valid_now
            _fms_session_state_since = now

        stable_duration = now - _fms_session_state_since
        stable_duration = now - _fms_session_state_since
        cooldown_ok     = (now - _last_session_alert_sent) >= SESSION_ALERT_COOLDOWN
        if cooldown_ok and stable_duration >= SESSION_DEBOUNCE_SECONDS and _fms_session_raw_valid != _fms_session_alerted_valid:
            # The state has held steady long enough to actually mean
            # something — escalate to an alert.
            if not _fms_session_raw_valid:
                print("⚠️ FMS session expired")
                _send_keeper_alert(
                    "⚠️ FMS session expired!\n"
                    "Attendance tracking is paused until someone runs the "
                    "keeper script and logs in again.\n"
                    "(This reminder repeats every 30 minutes until it's fixed.)"
                )
                _last_session_alert_sent = now
            else:
                print("✅ FMS session restored")
                _send_keeper_alert("✅ FMS session restored — attendance tracking is back online.", is_recovery=True)
            _fms_session_alerted_valid = _fms_session_raw_valid

        elif not _fms_session_alerted_valid and not _fms_session_raw_valid:
            # Already alerted "down" and still down — apply the 30-min
            # repeat cadence rather than staying silent forever.
            if now - _last_session_alert_sent >= SESSION_ALERT_REPEAT_SECONDS:
                _send_keeper_alert(
                    "⚠️ FMS session expired!\n"
                    "Attendance tracking is paused until someone runs the "
                    "keeper script and logs in again.\n"
                    "(This reminder repeats every 30 minutes until it's fixed.)"
                )
                _last_session_alert_sent = now

        return res
    except Exception as e:
        print(f"fms_get error: {e}")
        raise

# ==================== FMS FETCH ====================
def fetch_fms_for_ops_id(ops_id, days=15):
    now_ph   = datetime.now(PH_TIMEZONE)
    end_ts   = int(now_ph.timestamp())
    start_ts = int((now_ph - timedelta(days=days)).timestamp())
    all_recs = []
    page     = 1
    while True:
        try:
            res = fms_get({
                "biz_staff_id": ops_id,
                "pageno": page, "count": 24,
                "staff_type": 2,
                "start_time": start_ts, "end_time": end_ts,
            })
            if res.status_code != 200:
                break
            data = res.json()
            if data.get("retcode") != 0:
                break
            recs = data.get("data", {}).get("list", [])
            if not recs:
                break
            all_recs.extend(recs)
            if len(recs) < 24:
                break
            page += 1
        except Exception as e:
            print(f"fetch_fms_for_ops_id error: {e}")
            break
    return all_recs

def fetch_fms_for_ops_id_range(ops_id, start_dt, end_dt):
    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())
    all_recs = []
    page     = 1
    while True:
        try:
            res = fms_get({
                "biz_staff_id": ops_id,
                "pageno": page, "count": 24,
                "staff_type": 2,
                "start_time": start_ts, "end_time": end_ts,
            })
            if res.status_code != 200:
                break
            data = res.json()
            if data.get("retcode") != 0:
                break
            recs = data.get("data", {}).get("list", [])
            if not recs:
                break
            all_recs.extend(recs)
            if len(recs) < 24:
                break
            page += 1
        except Exception as e:
            print(f"fetch_fms_for_ops_id_range error: {e}")
            break
    return all_recs

# ==================== INTERNAL FMS POLLER ====================
# This loop runs inside the bot every FMS_POLL_INTERVAL seconds and fetches
# the latest attendance records for all tracked Ops IDs directly from FMS.
# This is what makes clock-in alerts fire in real time without an external caller.

_poller_running = False

# Build a fast reverse-lookup: ops_id.lower() → (emp_code, name, email)
# Rebuilt once on startup and whenever users are reloaded.
_ops_id_lookup: dict = {}

def _rebuild_ops_lookup():
    global _ops_id_lookup
    lk = {}
    for emp_code, oid in user_ops_id_map.items():
        if oid:
            lk[oid.strip().lower()] = {
                "emp_code": emp_code,
                "name":     user_name_map.get(emp_code, ""),
                "email":    user_email_map.get(emp_code, ""),
            }
    _ops_id_lookup = lk
    print(f"[Poller] 🗂️ ops_id lookup rebuilt ({len(lk)} entries)")

_rebuild_ops_lookup()


WARMUP_RECENCY_MARGIN_SECONDS = 10 * 60   # 10 minutes — events newer than this are left for the live poller

def _self_warmup():
    """Run once at startup, BEFORE the live poller starts. Silently seeds
    last_timein_map / last_timeout_map with each tracked member's recent
    FMS records — no alerts fired, no Sheets/tracker writes triggered.

    This is what actually makes a restart safe. Previously, dedup state
    only survived a restart if it had been persisted to Sheets in time —
    and Sheets saves have been hitting 429 quota errors, so on restart the
    maps often came back empty, and the next poll cycle treated the whole
    day's already-alerted events as brand new, flooding the group. This
    re-derives the same state directly from FMS (no Sheets quota involved)
    using the EXACT same key format process_attendance_records() uses
    (email if available, else ops_id), so it actually suppresses re-firing
    instead of silently mismatching keys.

    IMPORTANT: events newer than WARMUP_RECENCY_MARGIN_SECONDS are
    deliberately NOT seeded here, even though they're within the lookback
    window. If a clock-out happens right as the bot restarts (redeploy, or
    recovering from a session outage), it's entirely possible it was never
    actually alerted before the restart — silently seeding it here would
    mean it NEVER gets alerted at all (a permanently missing clock-out).
    Leaving very recent events unseeded lets the live poller pick them up
    and fire normally, at the small cost of an occasional duplicate for
    something that really was already alerted moments before restart.
    A missing clock-out is a worse failure than a rare duplicate.
    """
    if not TRACKED_OPS_IDS:
        return
    print(f"[Warmup] 🌡️  Self-warming dedup state for {len(TRACKED_OPS_IDS)} member(s)...")
    now_ph   = datetime.now(PH_TIMEZONE)
    now_ts   = now_ph.timestamp()
    end_ts   = int(now_ts)
    # 20h window: safely covers a full shift including one that started
    # yesterday evening and is still running (night shift).
    start_ts = int((now_ph - timedelta(hours=20)).timestamp())
    seeded_in = seeded_out = skipped_recent = 0

    for ops_id in TRACKED_OPS_IDS:
        info  = _ops_id_lookup.get(ops_id.strip().lower(), {})
        email = info.get("email", "")
        key_prefix = email if email else ops_id

        try:
            res = fms_get({
                "biz_staff_id": ops_id,
                "pageno": 1, "count": 5,
                "staff_type": 2,
                "start_time": start_ts, "end_time": end_ts,
            })
            if res.status_code != 200:
                continue
            data = res.json()
            if data.get("retcode") != 0:
                continue
            recs = data.get("data", {}).get("list", [])
        except Exception as e:
            print(f"[Warmup] ⚠️ Error fetching {ops_id}: {e}")
            continue

        for r in recs:
            ti = r.get("clock_in_time", 0)
            to = r.get("clock_out_time", 0)
            if ti:
                if now_ts - ti < WARMUP_RECENCY_MARGIN_SECONDS:
                    skipped_recent += 1
                else:
                    record_date = get_attendance_date(ti, None, is_timeout=False)
                    key = f"{key_prefix}:{record_date}:in"
                    with data_locks["last_timein"]:
                        if last_timein_map.get(key, 0) < ti:
                            last_timein_map[key] = ti
                            seeded_in += 1
            if to:
                if now_ts - to < WARMUP_RECENCY_MARGIN_SECONDS:
                    skipped_recent += 1
                else:
                    record_date = get_attendance_date(None, to, is_timeout=True)
                    key = f"{key_prefix}:{record_date}:out"
                    with data_locks["last_timeout"]:
                        if last_timeout_map.get(key, 0) < to:
                            last_timeout_map[key] = to
                            seeded_out += 1
        time.sleep(0.3)

    _save(LAST_TIMEIN_FILE, last_timein_map)
    _save(LAST_TIMEOUT_FILE, last_timeout_map)
    print(f"[Warmup] ✅ Seeded {seeded_in} time-in(s), {seeded_out} time-out(s) from FMS "
          f"— these will NOT re-fire alerts"
          + (f" ({skipped_recent} too-recent event(s) left for the live poller to handle normally)"
             if skipped_recent else ""))


_last_poller_fail_log     = 0.0
POLLER_FAIL_LOG_INTERVAL  = 60   # seconds — max once/min for the failed-fetch summary line

def _poll_fms_once():
    """Single poll pass — one FMS request per tracked Ops ID, fired in quick
    succession.  We keep individual requests (vs a batch call) because the
    FMS statistic_data_list endpoint does not accept multiple biz_staff_ids.
    The loop completes in ~10s for 21 members (0.4s sleep between calls).
    """
    if not TRACKED_OPS_IDS:
        print("[Poller] ⚠️ No TRACKED_OPS_IDS — nothing to poll.")
        return

    now_ph   = datetime.now(PH_TIMEZONE)
    end_ts   = int(now_ph.timestamp())
    # 20-hour window: matches _self_warmup()'s lookback so the poller can
    # catch a clock-out that happens hours after its clock-in (which a
    # 2-hour window can't reach). The dedup map — persisted to Sheets and
    # flushed on SIGTERM — guarantees already-alerted records don't replay
    # even with the wider window. This also makes the bot functional when
    # the keeper is offline.
    POLL_LOOKBACK_HOURS = int(os.getenv("POLL_LOOKBACK_HOURS", "20"))
    start_ts = int((now_ph - timedelta(hours=POLL_LOOKBACK_HOURS)).timestamp())

    records = []
    failed_count = 0
    for ops_id in TRACKED_OPS_IDS:
        info  = _ops_id_lookup.get(ops_id.strip().lower(), {})
        name  = info.get("name", "")
        email = info.get("email", "")

        try:
            res = fms_get({
                "biz_staff_id": ops_id,
                "pageno": 1, "count": 30,
                "staff_type": 2,
                "start_time": start_ts, "end_time": end_ts,
            })
            if res.status_code != 200:
                failed_count += 1
                continue
            data = res.json()
            if data.get("retcode") != 0:
                # retcode != 0 can mean no records — not always an error
                continue
            recs = data.get("data", {}).get("list", [])
            for r in recs:
                # Inject name/email if FMS omitted them so dedup key is stable
                if name and not r.get("staff_name"):
                    r["staff_name"] = name
                if email and not r.get("staff_email"):
                    r["staff_email"] = email
                records.append(r)
        except Exception as e:
            print(f"[Poller] ❌ Error fetching {ops_id}: {e}")
        time.sleep(0.4)   # ~8.4s total for 21 members — well within rate limits

    if failed_count:
        # One summary line instead of one per failed member — during a
        # session outage this used to print ~22 lines every single poll
        # cycle (every 20s), drowning out everything else in the Render
        # log. The actual session-down alert (DM, throttled to once/30min)
        # already covers this — this print is just for local visibility,
        # so it's throttled the same way.
        global _last_poller_fail_log
        now_t = time.time()
        if now_t - _last_poller_fail_log >= POLLER_FAIL_LOG_INTERVAL:
            print(f"[Poller] ⚠️ {failed_count}/{len(TRACKED_OPS_IDS)} member fetch(es) failed "
                  f"this cycle (session likely down — see session-expired alert)")
            _last_poller_fail_log = now_t

    if records:
        fired_in, fired_out = process_attendance_records(records)
        if fired_in:
            print(f"[Poller] 🟢 TIME-IN  fired: {fired_in}")
        if fired_out:
            print(f"[Poller] 🔴 TIME-OUT fired: {fired_out}")

def _poller_loop():
    global _poller_running
    _poller_running = True
    print(f"[Poller] 🚀 Internal FMS poller started (interval={FMS_POLL_INTERVAL}s)")
    while True:
        try:
            _poll_fms_once()
        except Exception as e:
            print(f"[Poller] ❌ Unhandled error: {e}")
        time.sleep(FMS_POLL_INTERVAL)

def start_internal_poller():
    t = threading.Thread(target=_poller_loop, daemon=True)
    t.start()

# ==================== DUTY ROSTER ====================
MAX_SHIFT_HOURS_BEFORE_STALE = 16   # no real shift (incl. OT) runs longer than this

def compute_on_duty(shift_type="day"):
    """Returns a list of {"ops_id", "full_name", "short_name"} dicts for
    everyone currently on duty for the given shift_type, using the same
    live in-memory poller state the scheduled roster uses (no API calls,
    instant). Excludes anyone who has already clocked out for that shift
    date, and anyone whose clock-in is stale (see MAX_SHIFT_HOURS_BEFORE_STALE)."""
    now   = datetime.now(PH_TIMEZONE)
    today = now.strftime("%Y-%m-%d")

    # prefix -> {"ops_id", "full_name", "short_name"}
    # Prefixes covered: email, ops_id, full name, shortened name — matching
    # every form process_attendance_records() might have used as a dedup
    # key. Without the name/short-name entries, a record that arrived with
    # both email and ops_id empty (so the key fell back to the full name)
    # would miss this lookup and print "No name mapping for prefix ...".
    prefix_to_info: dict = {}
    with data_locks["user_name"]:
        names_snapshot = dict(user_name_map)
    with data_locks["user_ops_id"]:
        ops_snapshot = dict(user_ops_id_map)

    for emp_code, name in names_snapshot.items():
        if not name or name == "Team Member":
            continue
        email  = user_email_map.get(emp_code, "")
        ops_id = ops_snapshot.get(emp_code, "")
        short  = shorten_name(name)
        info   = {"ops_id": ops_id, "full_name": name, "short_name": short}
        if email:
            prefix_to_info[email.lower()] = info
        if ops_id:
            prefix_to_info[ops_id.lower()] = info
        prefix_to_info[name.lower()] = info
        if short != name:
            prefix_to_info[short.lower()] = info

    # These two windows must cover all 24 hours between them with no gap.
    if shift_type == "day":
        valid_hours = set(range(5, 16))   # 05:00–15:59 day/mid-shift window
    else:
        valid_hours = set(range(16, 24)) | set(range(0, 6))  # 16:00–05:59 night shift window

    on_duty_by_ops_id: dict = {}   # ops_id -> info, dedups if both email+ops_id keys matched the same person

    with data_locks["last_timein"]:
        timein_snapshot = dict(last_timein_map)
    with data_locks["last_timeout"]:
        timeout_snapshot = dict(last_timeout_map)

    for key, ti_ts in timein_snapshot.items():
        parts = key.rsplit(":", 2)
        if len(parts) != 3 or parts[2] != "in":
            continue
        prefix   = parts[0].lower()
        key_date = parts[1]
        if key_date == today:
            pass
        elif shift_type == "night" and key_date == (now - timedelta(days=1)).strftime("%Y-%m-%d"):
            pass
        else:
            continue
        if not ti_ts:
            continue
        if f"{prefix}:{key_date}:out" in timeout_snapshot:
            continue
        # Safety cutoff: no real shift runs longer than this. Without it, a
        # clock-out that was never detected (e.g. it happened during an FMS
        # session outage) leaves someone showing as "on duty" forever, since
        # last_timein_map never expires and there's no timeout key to match
        # against. This is what caused Roger Marcial to still show up on a
        # day he wasn't even working.
        if (now.timestamp() - ti_ts) > MAX_SHIFT_HOURS_BEFORE_STALE * 3600:
            continue
        ci_hour = datetime.fromtimestamp(ti_ts, PH_TIMEZONE).hour
        if ci_hour not in valid_hours:
            continue
        info = prefix_to_info.get(prefix)
        if info:
            on_duty_by_ops_id[info["ops_id"] or info["short_name"]] = info
        else:
            print(f"[Roster] ⚠️ No name mapping for prefix '{prefix}'")

    return sorted(on_duty_by_ops_id.values(), key=lambda i: i["short_name"])


def current_shift_type():
    """Best-effort guess at which shift is 'current' right now, for the
    on-demand DM command. Day: 09:00-20:59. Night: 21:00-08:59."""
    hour = datetime.now(PH_TIMEZONE).hour
    return "day" if 9 <= hour < 21 else "night"


def get_leave_and_rdot_for_today(on_duty_ops_ids: set, shift_type: str):
    """Cross-references today's Schedule sheet against who's actually on
    duty. Returns (on_leave_names, not_rendered_rdot_names) — both lists of
    full names (not yet shortened to first-name display).

    on_leave: anyone whose schedule code for today is a leave code (checked
    regardless of shift_type — a leave day is a leave day either way).
    not_rendered_rdot: anyone scheduled for Rest Day OT *on this specific
    shift* today who is NOT in on_duty_ops_ids. This is shift-aware: a
    night-shift RDOT code (OT|NS|SOC8) is only checked against the night
    roster, and a day-shift one (OT|DS|SOC8) only against the day roster —
    otherwise a night-shift RDOT no-show would incorrectly also show up on
    the day roster's list, and vice versa.
    """
    on_leave = []
    not_rendered_rdot = []
    rdot_code_for_shift = "OT|DS|SOC8" if shift_type == "day" else "OT|NS|SOC8"
    try:
        schedule = load_schedule_dynamic()
        today    = datetime.now(PH_TIMEZONE).date()

        with data_locks["user_ops_id"]:
            ops_snapshot = dict(user_ops_id_map)
        with data_locks["user_name"]:
            names_snapshot = dict(user_name_map)
        name_to_ops_id = {
            name: ops_snapshot.get(emp_code, "")
            for emp_code, name in names_snapshot.items()
        }

        for (sched_name, sched_date), code in schedule.items():
            if sched_date != today:
                continue
            ops_id = name_to_ops_id.get(sched_name, "").strip().lower()

            is_leave, _ = is_leave_code(code)
            if is_leave:
                on_leave.append(sched_name)
                continue

            if code == rdot_code_for_shift:
                if ops_id not in on_duty_ops_ids:
                    not_rendered_rdot.append(sched_name)
    except Exception as e:
        print(f"[Roster] ⚠️ Could not read schedule for leave/RDOT sections: {e}")

    return on_leave, not_rendered_rdot


def generate_duty_roster(shift_type="day"):
    """
    Generates and sends a duty roster alert to the group.

    Reads clock-in data from last_timein_map (already in memory from the
    real-time poller) instead of re-reading Google Sheets attendance tabs.
    This avoids the tab-name mismatch problem and is instant (no API calls),
    except for the Schedule-sheet lookup used for the Leave/RDOT sections.
    """
    try:
        print(f"[Roster] 🕐 Generating {shift_type} shift roster...")
        today = datetime.now(PH_TIMEZONE)
        on_duty = compute_on_duty(shift_type)
        print(f"[Roster] Found {len(on_duty)} on duty: "
              f"{[p['short_name'] for p in on_duty]}")

        if not on_duty:
            print(f"[Roster] ⚠️ No one found for {shift_type} shift — not sending")
            return False

        # Group on-duty people by role
        role_buckets: dict = {}   # role -> [casual first name, ...]
        for person in on_duty:
            role = ROSTER_ROLE_MAP.get((person["ops_id"] or "").lower(), ROSTER_DEFAULT_ROLE)
            role_buckets.setdefault(role, []).append(casual_first_name(person["full_name"]))

        fte_names = sorted(role_buckets.get(ROSTER_DEFAULT_ROLE, []))

        on_duty_ops_ids = {(p["ops_id"] or "").lower() for p in on_duty}
        on_leave_full, rdot_full = get_leave_and_rdot_for_today(on_duty_ops_ids, shift_type)
        on_leave_names = sorted({casual_first_name(n) for n in on_leave_full})
        rdot_names     = sorted({casual_first_name(n) for n in rdot_full})

        shift_label = "Dayshift" if shift_type == "day" else "Nightshift"
        date_str    = today.strftime("%B %d, %Y %A")

        lines = [
            f"Good Day! {shift_label} Duty for {date_str}",
            "",
            f"FTE On-Duty - {len(on_duty)}",
            "",
            f"* {', '.join(fte_names)}" if fte_names else "* ",
            "* ",
        ]
        for role in ("Facilities", "MFM", "Planner", "Lead"):
            names = sorted(role_buckets.get(role, []))
            if names:
                lines.append(f"* {ROSTER_ROLE_LINE_FORMAT[role].format(names=', '.join(names))}")
        lines += [
            "",
            "Not Render RDOT: ",
            "",
            f"* {', '.join(rdot_names)}" if rdot_names else "* ",
            "",
            "On Leave: ",
            "",
            f"* {', '.join(on_leave_names)}" if on_leave_names else "* ",
        ]
        body = "\n".join(lines)

        # Mention tag goes at the TOP (renders as "@All" there), unlike
        # other group messages in this file which mention at the bottom —
        # this matches the requested roster format specifically.
        message = '<mention-tag target="seatalk://user?id=0"/>\n\n' + body
        send_group(message, mention_all=False)
        return True
    except Exception as e:
        print(f"[Roster] ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

# ==================== SCHEDULER ====================
scheduler = BackgroundScheduler(timezone=PH_TIMEZONE)

def schedule_duty_rosters():
    try:
        for hour, minute, shift_type, job_id in [
            (9,  0, "day",   "dayshift_roster_0900"),
            (21, 0, "night", "nightshift_roster_2100"),
        ]:
            scheduler.add_job(
                generate_duty_roster,
                CronTrigger(hour=hour, minute=minute, timezone=PH_TIMEZONE),
                args=[shift_type],
                id=job_id,
                replace_existing=True,
            )
        if not scheduler.running:
            scheduler.start()
            print("[Scheduler] ✅ Started")
    except Exception as e:
        print(f"[Scheduler] Error: {e}")

# ==================== OT RECORD WRITER ====================
def _classify_shift_type(sched_code, clock_in_ts, work_date, clock_out_ts=None):
    """Map schedule code or clock-in/out hour to new OT sheet shift type labels.

    A time-out-only manual correction (clock_in_ts is None) used to fall
    through to "Rest Day (no work)" even on a regular weekday, since only
    clock_in_ts was checked for "was there activity". Now clock_out_ts
    counts too, so either half of a manual correction is recognized.
    """
    # Any SOC 8|DS/NS <leave_type> — SL, VL, FCL, CL, NPL, PL, OL, etc.
    on_leave, leave_label = is_leave_code(sched_code)
    if on_leave:
        return leave_label
    has_activity = bool(clock_in_ts or clock_out_ts)
    # Rest day with no work
    if sched_code == "RD" or (not sched_code and not has_activity):
        return "Rest Day (no work)"
    # Schedule-driven codes (DS/NS regular, OT codes)
    if sched_code in SCHED_CODE_MAP:
        return SCHED_CODE_MAP[sched_code]
    # Infer from clock-in/out when no schedule code
    if has_activity:
        if work_date.weekday() >= 5:
            return "Rest Day OT"
        return "Regular Day"
    return "Rest Day (no work)"


def _ensure_ot_tab(ot_sh, name):
    """Get or create the member tab in the OT responses sheet."""
    try:
        return ot_sh.worksheet(name)
    except Exception:
        ws = ot_sh.add_worksheet(title=name, rows=500, cols=7)
        # Row 1: empty, Row 2: empty, Row 3: headers (matching existing sheet format)
        ws.update("A3:G3", [HEADERS_OT])
        ws.freeze(rows=3)
        import time as _time
        _time.sleep(1.0)
        return ws


def _append_with_cutoff_blank(ws, row, work_date):
    """Append row to OT tab, inserting a blank separator row when the
    cutoff period changes. Each cutoff boundary gets exactly one blank row."""
    try:
        prev_co, cur_co = get_ot_cutoffs(work_date)
        all_rows = ws.get_all_values()
        # Find the last non-empty row's date to detect a cutoff boundary
        last_date = None
        for r in reversed(all_rows[3:]):  # skip first 3 rows (title/header)
            if r and len(r) >= 3 and r[2].strip():
                try:
                    from datetime import datetime as _dt
                    last_date = _dt.strptime(r[2].strip(), "%m/%d/%Y").date()
                except ValueError:
                    pass
                break

        if last_date is not None:
            _, last_co = get_ot_cutoffs(last_date)
            _, this_co = get_ot_cutoffs(work_date)
            if last_co["label"] != this_co["label"]:
                # New cutoff period — insert one blank row as separator
                ws.append_row([""] * 7, value_input_option="USER_ENTERED")
                import time as _time
                _time.sleep(0.5)

        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[OT] ❌ append_with_cutoff_blank error: {e}")
        ws.append_row(row, value_input_option="USER_ENTERED")


def _find_last_date_row(cache: list, date_str: str):
    """Return the last 1-based row index in cache that matches date_str,
    skipping the first 3 rows (blank + header). Returns None if not found."""
    last_idx = None
    for idx, row in enumerate(cache[3:], start=3):
        if len(row) >= 3 and row[2].strip() == date_str:
            last_idx = idx
    return last_idx   # 0-based cache index; sheet row = last_idx + 1


def _insert_row_below(ws, cache_idx: int, row: list):
    """Insert `row` into the sheet at the row BELOW cache_idx (0-based).
    Uses gspread only — no direct googleapiclient import required."""
    sheet_row = cache_idx + 1    # 0-based cache index → 1-based sheet row
    insert_at = sheet_row + 1    # we want the row BELOW the matched row

    client      = _get_sheets_client()
    spreadsheet = client.open_by_key(OT_SHEET_ID)
    spreadsheet.batch_update({"requests": [{
        "insertRange": {
            "range": {
                "sheetId":       ws.id,
                "startRowIndex": insert_at - 1,   # 0-based for API
                "endRowIndex":   insert_at,
            },
            "shiftDimension": "ROWS",
        }
    }]})
    time.sleep(1.0)
    col_end = chr(ord("A") + len(row) - 1)
    ws.update(values=[row], range_name=f"A{insert_at}:{col_end}{insert_at}")
    print(f"[OT] ✅ Inserted correction row at sheet row {insert_at}")


def write_ot_record(name, employee_code, work_date, clock_in_ts=None, clock_out_ts=None):
    """Write a manual-correction OT row (8 columns incl. Remarks).

    Inserts a new row DIRECTLY BELOW the last existing row for work_date so
    corrections appear beside the original entry, not at the bottom of the sheet.
    Column H (Remarks) is set to "Manual correction via Bot".

    ACTUAL OUT is rounded to the nearest 30-minute mark, matching the OT
    sheet's interval and otsender70.py's auto-sync rounding — so a manual
    correction reads the same way a synced row would.
    """
    schedule   = load_schedule_dynamic()
    sched_code = schedule.get((name, work_date), "")
    shift_type = _classify_shift_type(sched_code, clock_in_ts, work_date, clock_out_ts)

    actual_in = datetime.fromtimestamp(clock_in_ts, PH_TIMEZONE).strftime("%I:%M:%S %p") if clock_in_ts else ""

    if clock_in_ts:
        dt    = datetime.fromtimestamp(clock_in_ts, PH_TIMEZONE)
        hour  = (dt.hour + (1 if dt.minute > 10 else 0)) % 24
        start = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
        end   = start + timedelta(hours=9)
        shift_start = start.strftime("%I:%M:%S %p")
        shift_end   = end.strftime("%I:%M:%S %p")
    else:
        shift_start = shift_end = ""

    if clock_out_ts:
        out_dt_rounded = round_to_half_hour(datetime.fromtimestamp(clock_out_ts, PH_TIMEZONE))
        actual_out = out_dt_rounded.strftime("%I:%M:%S %p")
    else:
        actual_out = ""

    real_email = user_email_map.get(employee_code, "").strip()
    if not real_email:
        ops_id = user_ops_id_map.get(employee_code, "")
        real_email = f"{ops_id.lower()}@spxexpress.com" if ops_id else "unknown@spxexpress.com"

    date_str = work_date.strftime("%m/%d/%Y")

    # 9 columns: A-G data | H Remarks (visible) | I Hidden flag (hidden col)
    row = [actual_in, real_email, date_str, shift_type,
           shift_start, shift_end, actual_out, "Manual correction via Bot", ""]

    try:
        client = _get_sheets_client()
        ot_sh  = client.open_by_key(OT_SHEET_ID)
        ws     = _ensure_ot_tab(ot_sh, name)

        # Read current sheet to find where to insert
        cache = ws.get_all_values()
        last_idx = _find_last_date_row(cache, date_str)

        if last_idx is not None:
            # Insert directly below the last row matching this date
            _insert_row_below(ws, last_idx, row)
        else:
            # Date not in sheet yet — plain append
            ws.append_row(row, value_input_option="USER_ENTERED")
            print(f"[OT] ✅ Appended correction for {date_str} (date not found in sheet)")
    except Exception as e:
        print(f"[OT] ❌ Write error: {e}")
        import traceback as _tb
        _tb.print_exc()


def write_leave_record(name, employee_code, work_date, leave_label):
    """Write a leave entry to the member tab (no clock-in/out times).

    Inserts directly below the last existing row for work_date, same as
    write_ot_record() does for manual clock corrections — previously this
    always appended to the very bottom of the sheet regardless of date,
    which put leave entries out of chronological order whenever a row for
    that date (or a later date) already existed.
    """
    real_email = user_email_map.get(employee_code, "").strip()
    if not real_email:
        ops_id = user_ops_id_map.get(employee_code, "")
        real_email = f"{ops_id.lower()}@spxexpress.com" if ops_id else "unknown@spxexpress.com"

    date_str = work_date.strftime("%m/%d/%Y")
    row = [
        "",          # A: ACTUAL IN – blank
        real_email,  # B
        date_str,    # C
        leave_label, # D: SHIFT TYPE — actual leave label (SL, VL, etc.)
        "",          # E: Shift Start
        "",          # F: Shift End
        "",          # G: ACTUAL OUT
        leave_label, # H: Remarks — also shows the leave label
        "",          # I: Hidden flag
    ]

    try:
        client = _get_sheets_client()
        ot_sh  = client.open_by_key(OT_SHEET_ID)
        ws     = _ensure_ot_tab(ot_sh, name)

        cache    = ws.get_all_values()
        last_idx = _find_last_date_row(cache, date_str)

        if last_idx is not None:
            # Insert directly below the last row matching this date, same
            # as manual clock corrections — keeps the sheet in date order.
            _insert_row_below(ws, last_idx, row)
        else:
            # Date not in sheet yet — safe to fall back to the
            # cutoff-aware append (handles the blank-separator-row logic
            # for a brand new cutoff period).
            _append_with_cutoff_blank(ws, row, work_date)
        print(f"[OT] ✅ Leave recorded {date_str} for {name}: {leave_label}")
    except Exception as e:
        print(f"[OT] ❌ Leave write error: {e}")


def cutoff_for(d):
    """Returns the cutoff dict that contains date d, or None."""
    prev_co, cur_co = get_ot_cutoffs(d)
    for co in (prev_co, cur_co):
        if co["start"] <= d <= co["end"]:
            return co
    return None

# ==================== ATTENDANCE PROCESSING ====================
def _extract_ops_id(r):
    """Extract the ops_id from a record, tolerating the different field
    names the two sources use. The keeper pushes raw FMS records whose
    ops_id lives in 'biz_staff_id'; the bot's own poller uses whatever
    OPS_ID_FIELD is set to ('staff_employee_id' by default). Reading both
    keeps key_prefix consistent regardless of which path the record came
    through — which is what stops the same clock-out from being deduped
    under two different keys."""
    return str(
        r.get(OPS_ID_FIELD)
        or r.get("biz_staff_id")
        or r.get("staff_employee_id")
        or ""
    ).strip()

def process_attendance_records(records):
    state_changed = False
    fired_in      = []
    fired_out     = []

    for r in records:
        ops_id  = _extract_ops_id(r)
        # Always prefer our OWN canonical name/email from the Users sheet
        # (via _ops_id_lookup) over whatever FMS reports in staff_name /
        # staff_email. FMS has started returning full legal names (with
        # middle names) for some members, which don't match the tracker
        # sheet's tab-naming convention and caused "No tracker tab" write
        # failures. This makes that immune to FMS's formatting regardless
        # of which path (internal poller or keeper push) the record came
        # through, since both call this same function.
        canon      = _ops_id_lookup.get(ops_id.strip().lower(), {})
        email      = (canon.get("email") or r.get("staff_email", "")).strip()
        full_name  = canon.get("name") or r.get("staff_name", f"Employee {ops_id or email}")
        short_name = shorten_name(full_name)
        station    = r.get("station_code", "").strip() or r.get("station", "").strip() or ""

        if not ops_id and not email and not full_name:
            # No identifier at all resolved — an empty-string key_prefix
            # would silently merge dedup state across every other
            # blank-identifier record. Log it with the raw record so it's
            # traceable, and skip rather than risk that collision.
            print(f"[Attendance] ⚠️ Skipping record with no ops_id, email, or name: {r}")
            continue
        # Prefer email/ops_id as the dedup key, but fall back to the
        # resolved name rather than dropping the record entirely — this
        # keeps per-person uniqueness (no cross-contamination between
        # different blank-identifier people) while still firing the alert.
        fallback_key_id = email or ops_id or full_name

        ti = r.get("clock_in_time", 0)
        to = r.get("clock_out_time", 0)

        if ti and ti > 0:
            record_date = get_attendance_date(ti, None, is_timeout=False)
            key_prefix  = fallback_key_id
            key         = f"{key_prefix}:{record_date}:in"
            msg         = None

            with data_locks["last_timein"]:
                prev = last_timein_map.get(key, 0)
                # Strict one-shot per key: fire only the first time this
                # date's clock-in is seen. Previously allowed a re-fire if
                # a later-reported timestamp was >300s newer, meant to
                # catch FMS revising a provisional timestamp — but in
                # practice this fired a second alert for the SAME physical
                # clock-in (e.g. 0820H then 0831H, an 11-min gap), which
                # just looks like a duplicate to everyone in the group.
                # The tracker sheet write below is unaffected by this —
                # write_clock_record() does its own independent date match.
                if prev == 0:
                    last_timein_map[key] = ti
                    shift_label = get_shift_label(ti)
                    shift_key   = f"{key_prefix}:{record_date}"
                    shift_cache_map[shift_key] = shift_label
                    state_changed = True
                    fired_in.append(key_prefix)
                    msg = format_timein(short_name, ti, shift=classify_shift(ti), station=station)

            if msg:
                print(f"[TIME-IN] ✅ {key_prefix} @ {record_date}")
                _save(LAST_TIMEIN_FILE, last_timein_map)
                _save(SHIFT_CACHE_FILE, shift_cache_map)
                threading.Thread(target=send_group, args=(msg, False), daemon=True).start()
                threading.Thread(target=write_clock_record, args=(full_name, ti, None), daemon=True).start()

        if to and to > 0:
            record_date = get_attendance_date(None, to, is_timeout=True)
            key_prefix  = fallback_key_id
            key         = f"{key_prefix}:{record_date}:out"
            msg         = None

            with data_locks["last_timeout"]:
                prev = last_timeout_map.get(key, 0)
                if prev == 0:
                    last_timeout_map[key] = to
                    _save(LAST_TIMEOUT_FILE, last_timeout_map)
                    state_changed = True
                    fired_out.append(key_prefix)
                    shift_key   = f"{key_prefix}:{record_date}"
                    shift_label = shift_cache_map.get(shift_key) or get_shift_label(to)
                    msg = format_timeout(short_name, to, shift=shift_label, station=station)

            if msg:
                print(f"[TIME-OUT] ✅ {key_prefix} @ {record_date}")
                threading.Thread(target=send_group, args=(msg, False), daemon=True).start()
                threading.Thread(target=write_clock_record, args=(full_name, None, to), daemon=True).start()

    if state_changed:
        # Local files (LAST_TIMEIN_FILE / LAST_TIMEOUT_FILE) were already
        # saved immediately above, per event. The Sheets mirror is handled
        # by the background _poller_state_saver_loop() thread — this just
        # marks it dirty so that loop picks it up on its next tick (within
        # SHEETS_SAVE_INTERVAL seconds), instead of hitting Sheets once per
        # event and risking a read-quota burst when many fire at once.
        with _poller_state_dirty_lock:
            global _poller_state_dirty
            _poller_state_dirty = True

    return fired_in, fired_out

def parse_month(month_input):
    month_input = month_input.strip().lower()
    try:
        num = int(month_input)
        if 1 <= num <= 12:
            return num
    except ValueError:
        pass
    month_names = {
        'january':1,'jan':1,'february':2,'feb':2,'march':3,'mar':3,
        'april':4,'apr':4,'may':5,'june':6,'jun':6,'july':7,'jul':7,
        'august':8,'aug':8,'september':9,'sep':9,'sept':9,
        'october':10,'oct':10,'november':11,'nov':11,'december':12,'dec':12,
    }
    return month_names.get(month_input)

# ==================== KEEP-ALIVE ====================
_last_keepalive_log = 0.0
KEEPALIVE_LOG_INTERVAL = 30 * 60   # 30 minutes

def keep_alive():
    global _last_keepalive_log
    time.sleep(60)
    while True:
        if RENDER_URL:
            try:
                requests.get(f"{RENDER_URL}/", timeout=10)
                now_t = time.time()
                if now_t - _last_keepalive_log >= KEEPALIVE_LOG_INTERVAL:
                    print("✅ Keep-alive ping sent")
                    _last_keepalive_log = now_t
            except Exception as e:
                print(f"Keep-alive error: {e}")
        time.sleep(600)

threading.Thread(target=keep_alive, daemon=True).start()

# ==================== WEBHOOK ====================
@app.route("/umay", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return "OK", 200

    if data.get("event_type") == "event_verification":
        return jsonify({"seatalk_challenge": data["event"]["seatalk_challenge"]})

    if data.get("event_type") != "message_from_bot_subscriber":
        return "OK", 200

    msg_id = data.get("event", {}).get("message", {}).get("message_id", "")
    with data_locks["processed_msgs"]:
        if msg_id and msg_id in processed_msgs:
            return "OK", 200
        if msg_id:
            processed_msgs[msg_id] = time.time()
            now_t = time.time()
            for mid in list(processed_msgs):
                if now_t - processed_msgs[mid] > 3600:
                    del processed_msgs[mid]

    event = data.get("event", {})
    code  = event.get("employee_code", "")
    raw   = event.get("message", {}).get("text", {})
    text  = (raw.get("content", "") if isinstance(raw, dict) else str(raw)).strip()
    text_lower = text.lower()

    if not code or not text:
        return "OK", 200

    print(f"[DM] {code}: {text[:60]}")

    # ── LEAVE COMMAND ─────────────────────────────────────────
    leave = parse_leave_command(text)
    if leave:
        with data_locks["user_name"]:
            name = user_name_map.get(code)
        if not name:
            send_dm(code, "⚠️ Please set your name first: setmyname <Full Name>")
            return "OK", 200

        leave_date_str = leave["date_obj"].strftime("%A, %B %d, %Y")
        date_fmt       = leave["date_obj"].strftime("%m/%d/%Y")

        # Write to attendance sheet (blank clock-in/out, leave label as shift)
        try:
            ws = get_user_sheet(name)
            if ws:
                all_rows = ws.get_all_values()
                record_date_key = leave["date_obj"].strftime("%Y-%m-%d")
                target_row = None
                for i, row in enumerate(all_rows[1:], start=2):
                    if row and len(row) > 0 and row[0] == record_date_key:
                        target_row = i
                        break
                if target_row:
                    ws.update_cell(target_row, 2, leave["leave_label"])
                else:
                    ws.append_row([record_date_key, leave["leave_label"], "SOC8", "", "", "", ""])
        except Exception as e:
            print(f"[Leave] Sheet write error: {e}")

        # Write to OT sheet
        try:
            write_leave_record(name, code, leave["date_obj"], leave["leave_label"])
        except Exception as e:
            print(f"[Leave] OT write error: {e}")

        # Notify member
        send_dm(code,
            f"✅ Leave recorded!\n"
            f"👤 {name}\n"
            f"📋 {leave['leave_label']}\n"
            f"📅 {date_fmt}"
        )

        # Post to group
        group_msg = format_leave(name, leave["leave_label"], leave_date_str)
        threading.Thread(target=send_group, args=(group_msg,), daemon=True).start()

        return "OK", 200

    # ── MANUAL TIME-IN / TIME-OUT ──────────────────────────────
    manual = parse_manual_clock(text)
    if manual:
        with data_locks["user_name"]:
            name = user_name_map.get(code)
        if not name:
            send_dm(code, "⚠️ Please set your name first: setmyname <Full Name>")
            return "OK", 200

        dt = datetime.combine(manual["date_obj"], manual["time_obj"])
        dt = dt.replace(tzinfo=PH_TIMEZONE)
        ts = int(dt.timestamp())

        if manual["type"] in ("time in", "timein"):
            clock_in_ts, clock_out_ts = ts, None
        else:
            clock_in_ts, clock_out_ts = None, ts

        try:
            write_clock_record(name, clock_in_time=clock_in_ts, clock_out_time=clock_out_ts)
        except Exception as e:
            send_dm(code, f"❌ Failed to record entry: {str(e)[:100]}")
            return "OK", 200

        time_formatted = dt.strftime("%H:%M:%S")
        date_formatted = dt.strftime("%Y-%m-%d")
        verb = "TIME IN" if manual["type"] in ("time in", "timein") else "TIME OUT"
        send_dm(code, f"✅ Manual {verb} recorded\n📅 {date_formatted}\n⏰ {time_formatted}")

        today_date = datetime.now(PH_TIMEZONE).date()
        if manual["date_obj"] == today_date:
            shift_label = classify_shift(ts)
            if manual["type"] in ("time in", "timein"):
                group_msg = format_timein(name, ts, shift=shift_label, station="SOC5")
            else:
                group_msg = format_timeout(name, ts, shift=shift_label, station="SOC5")
            if group_msg:
                send_group(group_msg)

        try:
            write_ot_record(name, code, manual["date_obj"], clock_in_ts, clock_out_ts)
        except Exception as e:
            print(f"[OT] Failed to write manual record: {e}")

        return "OK", 200

    # ── STANDARD COMMANDS ─────────────────────────────────────
    if text_lower in ("help", "commands"):
        leave_list = "\n".join(f"    {k.upper()} → {v}" for k, v in LEAVE_TYPES.items())
        msg = (
            "📋 Available commands:\n"
            "• time in / time out — post to group\n"
            "• Manual clock (3 lines):\n"
            "    time in\n"
            "    MM/DD/YYYY\n"
            "    HHMMH\n"
            "• Leave entry (3 lines):\n"
            "    leave\n"
            "    <leave code or full type>\n"
            "    MM/DD/YYYY\n"
            f"  Leave types:\n{leave_list}\n"
            "• my fms — last 15 days attendance\n"
            "• my fms <month> — e.g. my fms may\n"
            "• who is on duty — current shift roster"
        )
        if code in (ADMIN_CODE, ADMIN_CODE_2):
            msg += (
                "\n\n🔐 Admin commands:\n"
                "• fms <Full Name> — pull FMS records for any member\n"
                "• post duty — post on-duty roster to the group (or: post duty day / post duty night)"
            )
        send_dm(code, msg)
        return "OK", 200

    if text_lower == "mycode":
        send_dm(code, f"Your employee code is:\n{code}")
        return "OK", 200

    if text_lower in ("who is on duty", "who's on duty", "on duty", "duty"):
        shift_type = current_shift_type()
        on_duty    = compute_on_duty(shift_type)
        shift_label = "Dayshift" if shift_type == "day" else "Nightshift"
        if not on_duty:
            send_dm(code, f"📋 {shift_label} — no one currently showing as on duty.")
        else:
            roster_text = "\n".join(f"• {p['short_name']}" for p in on_duty)
            send_dm(code, f"📋 {shift_label} — {len(on_duty)} on duty:\n{roster_text}")
        return "OK", 200

    # Admin-only: post the on-duty roster to the SOC5 group chat on demand.
    #   post duty          -> current shift (same rule as "who is on duty")
    #   post duty day      -> force dayshift
    #   post duty night    -> force nightshift
    if text_lower in ("post duty", "post duty day", "post duty night",
                      "send duty", "send duty day", "send duty night"):
        if code not in (ADMIN_CODE, ADMIN_CODE_2):
            send_dm(code, "⛔ Only admins can post the duty roster to the group.")
            return "OK", 200
        if text_lower.endswith(" day"):
            shift_type = "day"
        elif text_lower.endswith(" night"):
            shift_type = "night"
        else:
            shift_type = current_shift_type()
        shift_label = "Dayshift" if shift_type == "day" else "Nightshift"
        if generate_duty_roster(shift_type):
            send_dm(code, f"✅ {shift_label} duty roster posted to the group.")
        else:
            send_dm(code, f"⚠️ Nothing posted — no one currently showing as on duty for {shift_label}.")
        return "OK", 200

    if text_lower == "register me":
        with data_locks["user_ops_id"]:
            my_ops_id = user_ops_id_map.get(code, "").strip()
        if not my_ops_id:
            send_dm(code, "⚠️ Set your Ops ID first:\nsetmyopsid <your Ops ID>")
            return "OK", 200
        with data_locks["user_name"]:
            name = user_name_map.get(code, f"Ops {my_ops_id}")
        send_dm(code,
            f"✅ You're registered!\n👤 {name}\n🆔 {my_ops_id}\n\n"
            f"Your time in/out will now be automatically posted to the group."
        )
        return "OK", 200

    if text_lower.startswith("setmyopsid"):
        parts = text.split()
        if len(parts) < 2:
            send_dm(code, "Usage: setmyopsid Ops287167")
            return "OK", 200
        ops_id = parts[1].strip()
        with data_locks["user_ops_id"]:
            user_ops_id_map[code] = ops_id
        threading.Thread(target=gsheet_save_user, args=(code,), kwargs={"ops_id": ops_id}, daemon=True).start()
        send_dm(code, f"✅ Ops ID saved: {ops_id}")
        return "OK", 200

    if text_lower.startswith("setmyname"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_dm(code, "Usage: setmyname Full Name")
            return "OK", 200
        name = parts[1].strip().title()
        with data_locks["user_name"]:
            user_name_map[code] = name
        threading.Thread(target=gsheet_save_user, args=(code,), kwargs={"name": name}, daemon=True).start()
        send_dm(code, f"✅ Name saved: {name}")
        return "OK", 200

    if text_lower in ("time in", "timein"):
        now_ts = int(time.time())
        with data_locks["user_name"]:
            name = user_name_map.get(code, "Team Member")
        send_group(format_timein(name, now_ts))
        send_dm(code, f"✅ Time in posted! ({fmt_time(now_ts)}H)")
        return "OK", 200

    if text_lower in ("time out", "timeout"):
        now_ts = int(time.time())
        with data_locks["user_name"]:
            name = user_name_map.get(code, "Team Member")
        send_group(format_timeout(name, now_ts))
        send_dm(code, f"✅ Time out posted! ({fmt_time(now_ts)}H)")
        return "OK", 200

    if text_lower == "my fms":
        with data_locks["user_ops_id"]:
            my_ops_id = user_ops_id_map.get(code, "").strip()
        if not my_ops_id:
            send_dm(code, "⚠️ Set your Ops ID first:\nsetmyopsid <your Ops ID>")
            return "OK", 200
        send_dm(code, "⏳ Fetching last 15 days... This may take 10–20 seconds.")
        def _fetch():
            try:
                records = fetch_fms_for_ops_id(my_ops_id, days=15)
                if not records:
                    send_dm(code, f"❌ No records found for {my_ops_id}")
                    return
                with data_locks["user_name"]:
                    display = user_name_map.get(code) or f"Ops {my_ops_id}"
                img_b64 = render_attendance_image(records, display)
                if img_b64:
                    send_dm_image(code, img_b64)
                else:
                    send_dm(code, "❌ Could not generate image.")
            except Exception as e:
                send_dm(code, f"❌ Error: {str(e)[:100]}")
        threading.Thread(target=_fetch, daemon=True).start()
        return "OK", 200

    if text_lower.startswith("my fms "):
        with data_locks["user_ops_id"]:
            my_ops_id = user_ops_id_map.get(code, "").strip()
        if not my_ops_id:
            send_dm(code, "⚠️ Set your Ops ID first:\nsetmyopsid <your Ops ID>")
            return "OK", 200
        month_input = text[7:].strip().lower()
        month_num   = parse_month(month_input)
        if month_num is None:
            send_dm(code, "❌ Invalid month. Use: my fms may  or  my fms 5")
            return "OK", 200
        now  = datetime.now(PH_TIMEZONE)
        year = now.year if month_num <= now.month else now.year - 1
        first_day = datetime(year, month_num, 1, tzinfo=PH_TIMEZONE)
        if month_num == 12:
            last_day = datetime(year + 1, 1, 1, tzinfo=PH_TIMEZONE) - timedelta(days=1)
        else:
            last_day = datetime(year, month_num + 1, 1, tzinfo=PH_TIMEZONE) - timedelta(days=1)
        last_day   = last_day.replace(hour=23, minute=59, second=59)
        month_name = first_day.strftime("%B %Y")
        send_dm(code, f"⏳ Fetching {month_name}...")
        def _fetch_month():
            try:
                records = fetch_fms_for_ops_id_range(my_ops_id, first_day, last_day)
                if not records:
                    send_dm(code, f"❌ No records for {month_name}")
                    return
                with data_locks["user_name"]:
                    display = user_name_map.get(code) or f"Ops {my_ops_id}"
                img_b64 = render_attendance_image(records, display)
                if img_b64:
                    send_dm_image(code, img_b64)
                else:
                    send_dm(code, "❌ Could not generate image.")
            except Exception as e:
                send_dm(code, f"❌ Error: {str(e)[:100]}")
        threading.Thread(target=_fetch_month, daemon=True).start()
        return "OK", 200

    # ── FMS LOOKUP: "fms <full name>" — admin only ─────────────
    if text_lower.startswith("fms "):
        # Only ADMIN_CODE and ADMIN_CODE_2 can use this command
        if code not in (ADMIN_CODE, ADMIN_CODE_2):
            send_dm(code, "⚠️ This command is for admins only.")
            return "OK", 200

        target_name = text[4:].strip()
        if not target_name:
            send_dm(code, "Usage: fms <Full Name>\nExample: fms Elvin John Asiado")
            return "OK", 200

        # Find ops_id by matching name (case-insensitive)
        found_ops_id = None
        found_name   = None
        target_lower = target_name.lower()
        with data_locks["user_name"]:
            names_snap = dict(user_name_map)
        with data_locks["user_ops_id"]:
            ops_snap = dict(user_ops_id_map)

        for emp_code, name in names_snap.items():
            if name.lower() == target_lower or shorten_name(name).lower() == target_lower:
                found_ops_id = ops_snap.get(emp_code, "").strip()
                found_name   = name
                break

        if not found_ops_id:
            send_dm(code, f"❌ Member not found: '{target_name}'\nCheck spelling or use the full name.")
            return "OK", 200

        send_dm(code, f"⏳ Fetching FMS records for {found_name}...")

        def _fetch_fms_admin():
            try:
                # Fetch for current cutoff window (prev + current = ~50 days max)
                from datetime import date as _date
                now_ph = datetime.now(PH_TIMEZONE)
                yr, mo, day = now_ph.year, now_ph.month, now_ph.day
                # Determine cutoff window start (same logic as ot_sender)
                if 11 <= day <= 25:
                    prev_mo = mo - 1 if mo > 1 else 12
                    prev_yr = yr if mo > 1 else yr - 1
                    window_start = _date(prev_yr, prev_mo, 26)
                elif day <= 10:
                    prev_mo = mo - 1 if mo > 1 else 12
                    prev_yr = yr if mo > 1 else yr - 1
                    window_start = _date(prev_yr, prev_mo, 11)
                else:
                    window_start = _date(yr, mo, 11)

                days_back = (now_ph.date() - window_start).days + 1
                records   = fetch_fms_for_ops_id(found_ops_id, days=days_back)

                if not records:
                    send_dm(code, f"❌ No FMS records found for {found_name} (Ops ID: {found_ops_id})")
                    return

                # Build text summary
                lines = [f"📋 FMS Attendance — {found_name}"]
                lines.append(f"🆔 Ops ID: {found_ops_id}")
                lines.append(f"📅 Window: {window_start} → {now_ph.date()}")
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

                for r in sorted(records, key=lambda x: x.get("date", ""), reverse=True):
                    d       = r.get("date", "-")
                    ci_ts   = r.get("clock_in_time", 0)
                    co_ts   = r.get("clock_out_time", 0)
                    ci_str  = datetime.fromtimestamp(ci_ts, PH_TIMEZONE).strftime("%H%MH") if ci_ts else "—"
                    co_str  = datetime.fromtimestamp(co_ts, PH_TIMEZONE).strftime("%H%MH") if co_ts else "—"
                    actual  = str(r.get("actual_hours") or "—")
                    lines.append(f"📆 {d}  IN: {ci_str}  OUT: {co_str}  Hrs: {actual}")

                send_dm(code, "\n".join(lines))

                # Also send the image card
                img_b64 = render_attendance_image(records, found_name)
                if img_b64:
                    send_dm_image(code, img_b64)

            except Exception as e:
                send_dm(code, f"❌ Error fetching FMS for {found_name}: {str(e)[:150]}")

        threading.Thread(target=_fetch_fms_admin, daemon=True).start()
        return "OK", 200

    send_dm(code, "Unknown command. Type help to see available commands.")
    return "OK", 200

# ==================== GRACEFUL SHUTDOWN ====================
# Dedup state syncs to Sheets at most once every 60s (see
# _poller_state_saver_loop). Render sends SIGTERM on every deploy/restart —
# if a real clock event fired in the up-to-60s gap between "it happened"
# and "it got saved to Sheets", a restart would reload the OLDER Sheets
# snapshot, that event would look new again, and the next poll would
# re-fire the alert. This flushes synchronously the instant the shutdown
# signal arrives, closing that window. (Self-warmup's recency margin
# protects the other direction — a genuinely-new event right at
# startup — this protects a just-fired event right at shutdown.)
import signal

def _flush_dedup_state_and_exit(signum, frame):
    print(f"[Shutdown] Caught signal {signum} — flushing dedup state to Sheets before exit...")
    try:
        with data_locks["last_timein"], data_locks["last_timeout"]:
            save_poller_state_to_sheets(dict(last_timein_map), dict(last_timeout_map))
        print("[Shutdown] ✅ Dedup state flushed.")
    except Exception as e:
        print(f"[Shutdown] ❌ Flush failed: {e}")
    import sys
    sys.exit(0)

signal.signal(signal.SIGTERM, _flush_dedup_state_and_exit)
signal.signal(signal.SIGINT, _flush_dedup_state_and_exit)

# ==================== RUN ====================
if __name__ == "__main__":
    print("✅ Bot starting...")
    schedule_duty_rosters()
    _self_warmup()             # ← silently re-derive dedup state from FMS before going live
    start_internal_poller()   # ← real-time FMS polling loop
    start_poller_state_saver()   # ← throttled Sheets mirror of dedup state (max once/60s)
    port = int(os.getenv("PORT", 8081))
    app.run(host="0.0.0.0", port=port)




