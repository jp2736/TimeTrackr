#!/usr/bin/env python3
"""TimeTrackr - Personal time tracker with Windows system tray"""

import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
import sys
import math
import platform_support as plat

from PIL import Image, ImageDraw

try:
    import reportlab  # noqa: F401
    _REPORTLAB = True
except ImportError:
    _REPORTLAB = False

# ── Config ───────────────────────────────────────────────────────────────────

APP_NAME = "TimeTrackr"
DATA_DIR = Path.home() / ".timetrackr"
DB_PATH = DATA_DIR / "data.db"

JOB_COLORS = [
    "#2196F3", "#4CAF50", "#F44336", "#FF9800",
    "#9C27B0", "#00BCD4", "#795548", "#607D8B",
]

# ── Country / dialling-code reference ─────────────────────────────────────────
# (name, dial_code). Curated common set; the country combobox is editable so
# anything not listed can still be typed. Plain text only — flag emoji do not
# render in Tkinter comboboxes on Windows.
COUNTRIES = [
    ("United Kingdom", "+44"), ("United States", "+1"), ("Ireland", "+353"),
    ("Canada", "+1"), ("Australia", "+61"), ("New Zealand", "+64"),
    ("Germany", "+49"), ("France", "+33"), ("Spain", "+34"), ("Italy", "+39"),
    ("Netherlands", "+31"), ("Belgium", "+32"), ("Switzerland", "+41"),
    ("Austria", "+43"), ("Sweden", "+46"), ("Norway", "+47"), ("Denmark", "+45"),
    ("Finland", "+358"), ("Portugal", "+351"), ("Poland", "+48"),
    ("Czech Republic", "+420"), ("India", "+91"), ("Singapore", "+65"),
    ("Hong Kong", "+852"), ("Japan", "+81"), ("South Africa", "+27"),
    ("United Arab Emirates", "+971"), ("Brazil", "+55"), ("Mexico", "+52"),
]


def country_names():
    return [name for name, _ in COUNTRIES]


def dial_labels():
    return [f"{name} ({code})" for name, code in COUNTRIES]


def label_for_code(code):
    for name, c in COUNTRIES:
        if c == code:
            return f"{name} ({c})"
    return code


def code_from_label(label):
    label = (label or "").strip()
    if label.endswith(")") and "(" in label:
        return label[label.rindex("(") + 1:-1].strip()
    return label


def compose_address(parts, sep=", "):
    """Join non-empty address parts (line1, line2, city, county, postcode, country)."""
    return sep.join(p.strip() for p in parts if p and p.strip())


def compose_phone(code, number):
    number = (number or "").strip()
    if not number:
        return ""
    return f"{(code or '').strip()} {number}".strip()


def compose_business_address(line_parts, country, legacy):
    """Business address for the PDF.

    Use the structured lines (plus country) only when at least one address
    line/locality field is set; otherwise fall back to the legacy single-blob
    address. A defaulted country alone must NOT mask an empty address.
    line_parts: [line1, line2, city, county, postcode] (country excluded).
    """
    if compose_address(line_parts):
        return compose_address(list(line_parts) + [country])
    return (legacy or "").strip()


def resolve_project_job(selected_job_id, job_ids):
    """Decide which job a new project attaches to when + Project is pressed.

    Returns ("use", job_id) to proceed, ("choose", None) to prompt the user,
    or ("empty", None) when no jobs exist.
    """
    if selected_job_id is not None:
        return ("use", selected_job_id)
    if not job_ids:
        return ("empty", None)
    if len(job_ids) == 1:
        return ("use", job_ids[0])
    return ("choose", None)


TAX_FREE_ALLOWANCE = 12_570
BASIC_RATE_LIMIT   = 50_270
HIGHER_RATE_LIMIT  = 125_140

# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT    NOT NULL UNIQUE,
                color   TEXT    NOT NULL DEFAULT '#2196F3',
                active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS projects (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id  INTEGER NOT NULL REFERENCES jobs(id),
                name    TEXT    NOT NULL,
                active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS entries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id     INTEGER NOT NULL REFERENCES jobs(id),
                project_id INTEGER REFERENCES projects(id),
                start_time TEXT    NOT NULL,
                end_time   TEXT,
                notes      TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS invoice_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS invoices (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                number       TEXT NOT NULL,
                client_name  TEXT,
                period_start TEXT,
                period_end   TEXT,
                total        REAL,
                pdf_path     TEXT,
                created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS invoice_jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
                job_name    TEXT    NOT NULL,
                hours       REAL    NOT NULL DEFAULT 0,
                rate        REAL    NOT NULL DEFAULT 0,
                amount      REAL    NOT NULL DEFAULT 0
            );
        """)
        self.conn.commit()
        # Migrate: add columns introduced after initial release
        for stmt in (
            "ALTER TABLE jobs    ADD COLUMN hourly_rate REAL",
            "ALTER TABLE entries ADD COLUMN invoiced INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self.conn.execute(stmt)
                self.conn.commit()
            except sqlite3.OperationalError:
                pass
        # Backfill: ensure any rows that stored NULL get set to 0
        self.conn.execute("UPDATE entries SET invoiced = 0 WHERE invoiced IS NULL")
        self.conn.commit()

    def _q(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params)

    def _w(self, sql, params=()):
        with self._lock:
            with self.conn:
                cur = self.conn.execute(sql, params)
                return cur.lastrowid

    # Jobs
    def jobs(self):
        return self._q("SELECT * FROM jobs WHERE active=1 ORDER BY name").fetchall()

    def add_job(self, name, color):
        return self._w("INSERT INTO jobs (name,color) VALUES (?,?)", (name, color))

    def update_job(self, jid, name, color):
        self._w("UPDATE jobs SET name=?,color=? WHERE id=?", (name, color, jid))

    def archive_job(self, jid):
        self._w("UPDATE jobs SET active=0 WHERE id=?", (jid,))

    # Projects
    def projects(self, job_id):
        return self._q(
            "SELECT * FROM projects WHERE job_id=? AND active=1 ORDER BY name",
            (job_id,)
        ).fetchall()

    def add_project(self, job_id, name):
        return self._w("INSERT INTO projects (job_id,name) VALUES (?,?)", (job_id, name))

    def update_project(self, pid, name):
        self._w("UPDATE projects SET name=? WHERE id=?", (name, pid))

    def archive_project(self, pid):
        self._w("UPDATE projects SET active=0 WHERE id=?", (pid,))

    # Entries
    def start_entry(self, job_id, project_id, notes):
        now = datetime.now().isoformat(timespec="seconds")
        return self._w(
            "INSERT INTO entries (job_id,project_id,start_time,notes) VALUES (?,?,?,?)",
            (job_id, project_id or None, now, notes or None),
        )

    def stop_entry(self, eid):
        now = datetime.now().isoformat(timespec="seconds")
        self._w("UPDATE entries SET end_time=? WHERE id=?", (now, eid))

    def log_entry(self, job_id, project_id, start_dt, end_dt, notes):
        self._w(
            "INSERT INTO entries (job_id,project_id,start_time,end_time,notes) VALUES (?,?,?,?,?)",
            (job_id, project_id or None,
             start_dt.isoformat(timespec="seconds"),
             end_dt.isoformat(timespec="seconds"),
             notes or None),
        )

    def open_entry(self):
        return self._q("""
            SELECT e.*, j.name AS job_name, j.color, p.name AS project_name
            FROM entries e
            JOIN jobs j ON e.job_id = j.id
            LEFT JOIN projects p ON e.project_id = p.id
            WHERE e.end_time IS NULL
            LIMIT 1
        """).fetchone()

    def recent_entries(self, limit=30):
        return self._q("""
            SELECT e.*,
                   j.name AS job_name, j.color,
                   p.name AS project_name,
                   ROUND(
                       (julianday(COALESCE(e.end_time, datetime('now','localtime')))
                        - julianday(e.start_time)) * 86400
                   ) AS duration_sec
            FROM entries e
            JOIN jobs j ON e.job_id = j.id
            LEFT JOIN projects p ON e.project_id = p.id
            ORDER BY e.start_time DESC
            LIMIT ?
        """, (limit,)).fetchall()

    def summary(self, start, end):
        return self._q("""
            SELECT j.name AS job_name, j.color,
                   p.name AS project_name,
                   SUM(ROUND(
                       (julianday(e.end_time) - julianday(e.start_time)) * 86400
                   )) AS seconds
            FROM entries e
            JOIN jobs j ON e.job_id = j.id
            LEFT JOIN projects p ON e.project_id = p.id
            WHERE e.end_time IS NOT NULL
              AND e.start_time >= ? AND e.start_time < ?
            GROUP BY e.job_id, e.project_id
            ORDER BY seconds DESC
        """, (start, end)).fetchall()

    def week_entries(self, start, end):
        return self._q("""
            SELECT e.*,
                   j.name AS job_name, j.color,
                   p.name AS project_name
            FROM entries e
            JOIN jobs j ON e.job_id = j.id
            LEFT JOIN projects p ON e.project_id = p.id
            WHERE e.start_time >= ? AND e.start_time < ?
            ORDER BY e.start_time
        """, (start, end)).fetchall()

    def entry_by_id(self, eid):
        return self._q("""
            SELECT e.*, j.name AS job_name, p.name AS project_name
            FROM entries e
            JOIN jobs j ON e.job_id = j.id
            LEFT JOIN projects p ON e.project_id = p.id
            WHERE e.id = ?
        """, (eid,)).fetchone()

    def update_entry(self, eid, job_id, project_id, start_dt, end_dt, notes):
        self._w(
            "UPDATE entries SET job_id=?, project_id=?, start_time=?, end_time=?, notes=? WHERE id=?",
            (job_id, project_id or None,
             start_dt.isoformat(timespec="seconds"),
             end_dt.isoformat(timespec="seconds") if end_dt else None,
             notes or None, eid),
        )

    def delete_entry(self, eid):
        self._w("DELETE FROM entries WHERE id=?", (eid,))

    # Invoice settings
    def get_setting(self, key, default=""):
        row = self._q("SELECT value FROM invoice_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        self._w("INSERT OR REPLACE INTO invoice_settings (key,value) VALUES (?,?)", (key, value))

    def invoice_line_items(self, start, end):
        return self._q("""
            SELECT j.id AS job_id, j.name AS job_name, j.hourly_rate,
                   p.id AS project_id, p.name AS project_name,
                   ROUND(SUM(
                       (julianday(e.end_time) - julianday(e.start_time)) * 24
                   ), 2) AS hours
            FROM entries e
            JOIN jobs j ON e.job_id = j.id
            LEFT JOIN projects p ON e.project_id = p.id
            WHERE e.end_time IS NOT NULL
              AND COALESCE(e.invoiced, 0) = 0
              AND e.start_time >= ? AND e.start_time < ?
            GROUP BY j.id, p.id
            ORDER BY j.name, p.name
        """, (start, end)).fetchall()

    def next_invoice_number(self, prefix="INV-"):
        row = self._q("SELECT COUNT(*) AS n FROM invoices").fetchone()
        return f"{prefix}{row['n'] + 1:03d}"

    def save_invoice(self, number, client_name, period_start, period_end, total, pdf_path,
                     line_items=None):
        with self._lock:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO invoices"
                    " (number,client_name,period_start,period_end,total,pdf_path)"
                    " VALUES (?,?,?,?,?,?)",
                    (number, client_name, period_start, period_end, total, str(pdf_path)),
                )
                invoice_id = cur.lastrowid
                if line_items:
                    for item in line_items:
                        self.conn.execute(
                            "INSERT INTO invoice_jobs (invoice_id,job_name,hours,rate,amount)"
                            " VALUES (?,?,?,?,?)",
                            (invoice_id, item["job"], item["hours"],
                             item.get("rate", 0), item["amount"]),
                        )
                self.conn.execute(
                    "UPDATE entries SET invoiced=1"
                    " WHERE end_time IS NOT NULL AND start_time >= ? AND start_time < ?",
                    (period_start, period_end),
                )

    def tax_overview(self, period_start=None, period_end=None):
        """Return (total_invoiced, [(job_name, amount), ...]) for the given date window.

        period_start/period_end filter by invoice.period_start (ISO date strings).
        Pass None to query all time.
        """
        where = ""
        p = []
        if period_start:
            where += " AND i.period_start >= ?"
            p.append(period_start)
        if period_end:
            where += " AND i.period_start < ?"
            p.append(period_end)
        total = self._q(
            f"SELECT COALESCE(SUM(total),0) AS t FROM invoices i WHERE 1=1{where}", p
        ).fetchone()["t"]
        by_job = self._q(
            f"""SELECT ij.job_name, SUM(ij.amount) AS amount
                FROM invoice_jobs ij
                JOIN invoices i ON ij.invoice_id = i.id
                WHERE 1=1{where}
                GROUP BY ij.job_name
                ORDER BY amount DESC""",
            p,
        ).fetchall()
        return total, by_job


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_hm(seconds):
    if not seconds:
        return "0h 0m"
    return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60)}m"


def fmt_hms(seconds):
    if not seconds:
        return "0:00:00"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def week_bounds(d=None):
    d = d or date.today()
    start = d - timedelta(days=d.weekday())
    return start.isoformat(), (start + timedelta(days=7)).isoformat()


def month_bounds(d=None):
    d = d or date.today()
    start = date(d.year, d.month, 1)
    end = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


# A known past invoicing Thursday. Only its phase (mod 14 days) matters — it fixes
# which Thursdays are biweekly anchors, since the cycle is out of phase with ISO weeks.
BIWEEKLY_ANCHOR = date(2026, 6, 18)


def biweekly_bounds(offset=0):
    """Return (start_iso, end_iso) for a biweekly billing period. End is EXCLUSIVE
    (SQL uses start_time < end). Periods are anchored to invoicing Thursdays every
    14 days from BIWEEKLY_ANCHOR; each completed period covers the 14 days ending on
    (and including) its anchor Thursday.

    offset=0  → current in-progress period: the day AFTER the most recent anchor,
                up to and including today. (On an anchor Thursday this is empty —
                that day's work belongs to the period that just closed.)
    offset=-1 → most recently completed period (closes on the most recent anchor on
                or before today, and includes that Thursday's work).
    offset=-2 → the period before that, and so on.

    Consecutive periods abut exactly: an anchor Thursday is the last billed day of
    its period and is never shared with the next.
    """
    today = date.today()
    # Most recent anchor Thursday on or before today (phase-aware, 14-day steps).
    days_since_anchor = (today - BIWEEKLY_ANCHOR).days % 14
    last_anchor = today - timedelta(days=days_since_anchor)

    if offset == 0:
        # In-progress: day after the last anchor → today (inclusive via exclusive +1).
        start = last_anchor + timedelta(days=1)
        return start.isoformat(), (today + timedelta(days=1)).isoformat()

    # Completed period(s): the anchor that closes the period, stepping back 14 days.
    anchor = last_anchor + timedelta(weeks=2 * (offset + 1))  # last_anchor when offset=-1
    start = anchor - timedelta(days=13)      # 14-day window ending on the anchor
    end   = anchor + timedelta(days=1)       # exclusive → includes the anchor Thursday
    return start.isoformat(), end.isoformat()


def uk_tax_year_bounds(offset=0):
    """Return (start_iso, end_iso) for a UK tax year (Apr 6 → Apr 5).

    offset=0 → current tax year; offset=-1 → previous.
    """
    today = date.today()
    start_year = today.year if today >= date(today.year, 4, 6) else today.year - 1
    start_year += offset
    return date(start_year, 4, 6).isoformat(), date(start_year + 1, 4, 6).isoformat()


def calc_uk_tax(income):
    """Break down income across UK tax bands and return estimated liability dict."""
    allowance_used  = min(income, TAX_FREE_ALLOWANCE)
    basic_taxable   = max(0.0, min(income, BASIC_RATE_LIMIT)  - TAX_FREE_ALLOWANCE)
    higher_taxable  = max(0.0, min(income, HIGHER_RATE_LIMIT) - BASIC_RATE_LIMIT)
    basic_tax       = basic_taxable  * 0.20
    higher_tax      = higher_taxable * 0.40
    return {
        "allowance_used":  allowance_used,
        "basic_taxable":   basic_taxable,
        "higher_taxable":  higher_taxable,
        "basic_tax":       basic_tax,
        "higher_tax":      higher_tax,
        "total_tax":       basic_tax + higher_tax,
    }


def make_tray_icon(tracking=False):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill   = "#43A047" if tracking else "#9E9E9E"
    border = "#1B5E20" if tracking else "#424242"
    d.ellipse([2, 2, 62, 62],  fill=fill,    outline=border, width=3)
    d.ellipse([12, 12, 52, 52], fill="white", outline=border, width=2)
    # clock hands
    d.line([32, 32, 32, 18], fill=border, width=3)
    d.line([32, 32, 43, 38], fill=border, width=2)
    d.ellipse([29, 29, 35, 35], fill=border)
    return img


class PlaceholderEntry(tk.Entry):
    """Entry that shows greyed example text when empty and unfocused.

    get_value() returns "" while the placeholder is displayed so placeholder
    text never leaks into saved settings or a generated PDF.
    """

    def __init__(self, master, placeholder="", color="#9AA0A6", **kw):
        super().__init__(master, **kw)
        self._placeholder = placeholder
        self._ph_color = color
        self._default_fg = self.cget("fg")
        self._is_placeholder = False
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self._show_placeholder()

    def _show_placeholder(self):
        self.delete(0, tk.END)
        self.insert(0, self._placeholder)
        self.config(fg=self._ph_color)
        self._is_placeholder = True

    def _on_focus_in(self, _=None):
        if self._is_placeholder:
            self.delete(0, tk.END)
            self.config(fg=self._default_fg)
            self._is_placeholder = False

    def _on_focus_out(self, _=None):
        if not self.get():
            self._show_placeholder()

    def get_value(self):
        return "" if self._is_placeholder else self.get()

    def set_value(self, text):
        if text:
            self.config(fg=self._default_fg)
            self._is_placeholder = False
            self.delete(0, tk.END)
            self.insert(0, text)
        else:
            self._show_placeholder()


def ask_string(parent, title, prompt, initial=""):
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.resizable(False, False)
    dlg.grab_set()
    result = [None]

    tk.Label(dlg, text=prompt, padx=12, pady=8).pack()
    var = tk.StringVar(value=initial)
    ent = tk.Entry(dlg, textvariable=var, width=32)
    ent.pack(padx=12, pady=(0, 8))
    ent.focus_set()
    ent.select_range(0, tk.END)

    def ok(_=None):
        result[0] = var.get().strip()
        dlg.destroy()

    ent.bind("<Return>", ok)
    ent.bind("<Escape>", lambda _: dlg.destroy())

    bf = tk.Frame(dlg)
    bf.pack(pady=(0, 8))
    ttk.Button(bf, text="OK",     command=ok).pack(side="left", padx=4)
    ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

    dlg.geometry("+%d+%d" % (parent.winfo_rootx() + 60, parent.winfo_rooty() + 60))
    parent.wait_window(dlg)
    return result[0]


def ask_choice(parent, title, prompt, options):
    """Modal single-choice picker. Returns the selected index, or None if cancelled."""
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.resizable(False, False)
    dlg.grab_set()
    result = {"idx": None}

    f = tk.Frame(dlg, padx=16, pady=12)
    f.pack()
    tk.Label(f, text=prompt, anchor="w").pack(fill="x", pady=(0, 6))
    var = tk.StringVar(value=options[0] if options else "")
    cb = ttk.Combobox(f, textvariable=var, values=list(options),
                      state="readonly", width=28)
    cb.pack()
    if options:
        cb.current(0)

    def ok(_=None):
        result["idx"] = cb.current()
        dlg.destroy()

    bf = tk.Frame(f)
    bf.pack(pady=(10, 0))
    ttk.Button(bf, text="OK", command=ok).pack(side="left", padx=5)
    ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side="left", padx=5)

    cx = parent.winfo_screenwidth() // 2 - 160
    cy = parent.winfo_screenheight() // 2 - 80
    dlg.geometry(f"+{cx}+{cy}")
    dlg.focus_force()
    parent.wait_window(dlg)
    return result["idx"]


# ── Invoice PDF generator ─────────────────────────────────────────────────────

def generate_invoice_pdf(data, out_path):
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                     Paragraph, Spacer, HRFlowable)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT

    doc  = SimpleDocTemplate(str(out_path), pagesize=A4,
                              leftMargin=2*cm, rightMargin=2*cm,
                              topMargin=2*cm, bottomMargin=2*cm)
    ss   = getSampleStyleSheet()
    blue = colors.HexColor("#1565C0")
    cur  = data.get("currency", "£")
    pw   = A4[0] - 4*cm

    def ps(name, **kw):
        return ParagraphStyle(name, parent=ss["Normal"], **kw)

    normal = ss["Normal"]
    s_r    = ps("_r",  alignment=TA_RIGHT)
    s_h2   = ps("_h2", fontSize=11, fontName="Helvetica-Bold", spaceAfter=4)
    s_sm   = ps("_sm", fontSize=9, textColor=colors.grey)

    story = []

    # Header: business name (left) | INVOICE (right)
    hdr = Table(
        [[Paragraph(f"<font size=20><b>{data.get('biz_name','')}</b></font>", normal),
          Paragraph(f'<font size=26 color="#1565C0"><b>INVOICE</b></font>', s_r)]],
        colWidths=[pw*0.6, pw*0.4],
    )
    hdr.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")]))
    story.append(hdr)
    story.append(Spacer(1, 0.6*cm))

    # Biz contact line + invoice meta
    biz_parts = [data.get(k,"").strip()
                 for k in ("biz_address","biz_email","biz_phone")
                 if data.get(k,"").strip()]
    biz_text  = "   ·   ".join(biz_parts) or " "
    def fmt_date(iso, inclusive_end=False):
        try:
            d = datetime.strptime(iso, "%Y-%m-%d")
            # Stored period_end is exclusive (SQL uses start_time < end); show the
            # actual last billed day (the invoicing Thursday) by stepping back one day.
            if inclusive_end:
                d = d - timedelta(days=1)
            return f"{d.day} {d.strftime('%B %Y')}"
        except Exception:
            return iso

    period_start = fmt_date(data.get("period_start", ""))
    period_end   = fmt_date(data.get("period_end",   ""), inclusive_end=True)
    period_str   = f"{period_start} – {period_end}" if period_start and period_end else ""

    meta_html = (f"<b>Invoice #:</b>  {data.get('invoice_number','')}<br/>"
                 f"<b>Date:</b>  {data.get('issue_date','')}<br/>"
                 f"<b>Due:</b>  {data.get('due_date','')}<br/>"
                 f"<b>Period:</b>  {period_str}")
    meta_tbl  = Table([[Paragraph(biz_text, normal), Paragraph(meta_html, s_r)]],
                       colWidths=[pw*0.6, pw*0.4])
    story += [meta_tbl, Spacer(1,0.3*cm),
              HRFlowable(width="100%", thickness=2, color=blue),
              Spacer(1,0.4*cm)]

    # Bill To
    story.append(Paragraph("Bill To", s_h2))
    story.append(Paragraph(f"<b>{data.get('client_name','')}</b>", normal))
    for line in data.get("client_address","").splitlines():
        if line.strip():
            story.append(Paragraph(line.strip(), normal))
    story.append(Spacer(1, 0.5*cm))

    # Line items table
    rows = [["Job", "Project", "Hours", f"Rate ({cur}/hr)", "Amount"]]
    for it in data.get("line_items", []):
        rows.append([it["job"], it["project"] or "—",
                     f"{it['hours']:.2f}",
                     f"{cur}{it['rate']:.2f}",
                     f"{cur}{it['amount']:.2f}"])
    cw     = [pw*p for p in (0.28, 0.24, 0.12, 0.18, 0.18)]
    it_tbl = Table(rows, colWidths=cw, repeatRows=1)
    it_ts  = TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), blue),
        ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("ALIGN",         (2,0), (-1,-1), "RIGHT"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("GRID",          (0,0), (-1,-1), 0.5, colors.HexColor("#DDDDDD")),
    ])
    for i in range(1, len(rows)):
        if i % 2 == 0:
            it_ts.add("BACKGROUND", (0,i), (-1,i), colors.HexColor("#F0F4FF"))
    it_tbl.setStyle(it_ts)
    story += [it_tbl, Spacer(1, 0.4*cm)]

    # Totals
    subtotal   = data.get("subtotal",   0.0)
    tax_rate   = data.get("tax_rate",   0.0)
    tax_amount = data.get("tax_amount", 0.0)
    total      = data.get("total",      0.0)
    tot_data   = [["", "Subtotal", f"{cur}{subtotal:.2f}"]]
    if tax_rate:
        tot_data.append(["", f"Tax ({tax_rate:.1f}%)", f"{cur}{tax_amount:.2f}"])
    tot_data.append(["",
                     Paragraph("<b>Total Due</b>", normal),
                     Paragraph(f"<b>{cur}{total:.2f}</b>", s_r)])
    tot_tbl = Table(tot_data, colWidths=[pw*0.55, pw*0.25, pw*0.20])
    tot_tbl.setStyle(TableStyle([
        ("ALIGN",         (1,0), (-1,-1), "RIGHT"),
        ("FONTSIZE",      (0,0), (-1,-1), 10),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LINEABOVE",     (0,-1), (-1,-1), 1, colors.black),
    ]))
    story += [tot_tbl, Spacer(1,0.8*cm),
              HRFlowable(width="100%", thickness=0.5, color=colors.grey),
              Spacer(1,0.3*cm)]

    # Payment details
    bank_keys = [("Account Name","bank_account_name"),("Bank","bank_name"),
                 ("Account No.","bank_account_number"),("Sort Code","bank_sort_code"),
                 ("IBAN","bank_iban"),("BIC/SWIFT","bank_bic")]
    bank_parts = [f"<b>{lbl}:</b> {data.get(k,'')}"
                  for lbl, k in bank_keys if data.get(k,"").strip()]
    if bank_parts:
        story.append(Paragraph("Payment Details", s_h2))
        story.append(Paragraph("Please make payment by bank transfer to:", normal))
        story.append(Spacer(1, 0.15*cm))
        story.append(Paragraph("   ·   ".join(bank_parts), normal))
        story.append(Spacer(1, 0.3*cm))

    if data.get("notes","").strip():
        story.append(Paragraph("Notes", s_h2))
        story.append(Paragraph(data["notes"].strip(), s_sm))

    doc.build(story)


# ── Start Tracking Dialog ─────────────────────────────────────────────────────

class StartDialog(tk.Toplevel):
    def __init__(self, parent, db, on_start):
        super().__init__(parent)
        self.db = db
        self.on_start = on_start
        self.title("Start Tracking")
        self.resizable(False, False)
        self.grab_set()
        self._jobs = db.jobs()
        self._projs_data = []
        self._build()
        cx = parent.winfo_screenwidth()  // 2 - 165
        cy = parent.winfo_screenheight() // 2 - 100
        self.geometry(f"+{cx}+{cy}")
        self.focus_force()

    def _build(self):
        f = tk.Frame(self, padx=16, pady=12)
        f.pack()

        tk.Label(f, text="Job", anchor="w", width=8).grid(row=0, column=0, sticky="w", pady=4)
        self._job_var = tk.StringVar()
        self._job_cb  = ttk.Combobox(f, textvariable=self._job_var,
                                      values=[j["name"] for j in self._jobs],
                                      state="readonly", width=26)
        self._job_cb.grid(row=0, column=1, pady=4)
        if self._jobs:
            self._job_cb.current(0)
        self._job_cb.bind("<<ComboboxSelected>>", lambda _: self._load_projects())

        tk.Label(f, text="Project", anchor="w", width=8).grid(row=1, column=0, sticky="w", pady=4)
        self._proj_var = tk.StringVar()
        self._proj_cb  = ttk.Combobox(f, textvariable=self._proj_var,
                                       state="readonly", width=26)
        self._proj_cb.grid(row=1, column=1, pady=4)
        self._load_projects()

        tk.Label(f, text="Notes", anchor="w", width=8).grid(row=2, column=0, sticky="w", pady=4)
        self._notes_var = tk.StringVar()
        tk.Entry(f, textvariable=self._notes_var, width=28).grid(row=2, column=1, pady=4)

        bf = tk.Frame(f)
        bf.grid(row=3, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(bf, text="Start",  command=self._start).pack(side="left", padx=5)
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=5)

    def _load_projects(self):
        job = self._sel_job()
        if job:
            projs = self.db.projects(job["id"])
            self._projs_data = [None] + list(projs)
            self._proj_cb["values"] = ["(none)"] + [p["name"] for p in projs]
        else:
            self._projs_data = [None]
            self._proj_cb["values"] = ["(none)"]
        self._proj_cb.current(0)

    def _sel_job(self):
        n = self._job_var.get()
        return next((j for j in self._jobs if j["name"] == n), None)

    def _start(self):
        job = self._sel_job()
        if not job:
            messagebox.showwarning("No Job", "Please select a job first.", parent=self)
            return
        idx     = self._proj_cb.current()
        proj    = self._projs_data[idx] if idx >= 0 else None
        proj_id = proj["id"] if proj else None
        notes   = self._notes_var.get().strip() or None
        eid     = self.db.start_entry(job["id"], proj_id, notes)
        self.on_start(eid)
        self.destroy()


# ── Log Past Time Dialog ──────────────────────────────────────────────────────

class LogTimeDialog(tk.Toplevel):
    """Log a completed block of time that has already happened."""

    def __init__(self, parent, db, on_logged):
        super().__init__(parent)
        self.db        = db
        self.on_logged = on_logged
        self.title("Log Past Time")
        self.resizable(False, False)
        self.grab_set()
        self._jobs       = db.jobs()
        self._projs_data = []
        self._build()
        cx = parent.winfo_screenwidth()  // 2 - 170
        cy = parent.winfo_screenheight() // 2 - 140
        self.geometry(f"+{cx}+{cy}")
        self.focus_force()

    def _build(self):
        f = tk.Frame(self, padx=16, pady=12)
        f.pack()

        now = datetime.now()

        def lbl_row(r, text, widget):
            tk.Label(f, text=text, anchor="w", width=9).grid(row=r, column=0, sticky="w", pady=4)
            widget.grid(row=r, column=1, pady=4, sticky="w")

        # Job
        self._job_var = tk.StringVar()
        self._job_cb  = ttk.Combobox(f, textvariable=self._job_var,
                                      values=[j["name"] for j in self._jobs],
                                      state="readonly", width=24)
        lbl_row(0, "Job", self._job_cb)
        if self._jobs:
            self._job_cb.current(0)
        self._job_cb.bind("<<ComboboxSelected>>", lambda _: self._load_projects())

        # Project
        self._proj_var = tk.StringVar()
        self._proj_cb  = ttk.Combobox(f, textvariable=self._proj_var, state="readonly", width=24)
        lbl_row(1, "Project", self._proj_cb)
        self._load_projects()

        # Date
        self._date_var = tk.StringVar(value=now.strftime("%Y-%m-%d"))
        lbl_row(2, "Date", tk.Entry(f, textvariable=self._date_var, width=14))
        tk.Label(f, text="YYYY-MM-DD", fg="#888", font=("Segoe UI", 8)).grid(
            row=2, column=2, padx=(4, 0), sticky="w")

        # Start / End times
        self._start_var = tk.StringVar(value=(now - timedelta(hours=1)).strftime("%H:%M"))
        self._end_var   = tk.StringVar(value=now.strftime("%H:%M"))
        lbl_row(3, "Start", tk.Entry(f, textvariable=self._start_var, width=8))
        lbl_row(4, "End",   tk.Entry(f, textvariable=self._end_var,   width=8))
        tk.Label(f, text="HH:MM", fg="#888", font=("Segoe UI", 8)).grid(
            row=3, column=2, padx=(4, 0), sticky="w")

        # Live duration display
        tk.Label(f, text="Duration", anchor="w", width=9).grid(row=5, column=0, sticky="w", pady=4)
        self._dur_lbl = tk.Label(f, text="", font=("Segoe UI", 9, "bold"), fg="#1565C0")
        self._dur_lbl.grid(row=5, column=1, sticky="w", pady=4)

        # Notes
        self._notes_var = tk.StringVar()
        lbl_row(6, "Notes", tk.Entry(f, textvariable=self._notes_var, width=26))

        # Buttons
        bf = tk.Frame(f)
        bf.grid(row=7, column=0, columnspan=3, pady=(12, 0))
        ttk.Button(bf, text="Log Time", command=self._log).pack(side="left", padx=5)
        ttk.Button(bf, text="Cancel",   command=self.destroy).pack(side="left", padx=5)

        # Update duration whenever times change
        for var in (self._date_var, self._start_var, self._end_var):
            var.trace_add("write", lambda *_: self._update_dur())
        self._update_dur()

    def _load_projects(self):
        job = self._sel_job()
        if job:
            projs = self.db.projects(job["id"])
            self._projs_data = [None] + list(projs)
            self._proj_cb["values"] = ["(none)"] + [p["name"] for p in projs]
        else:
            self._projs_data = [None]
            self._proj_cb["values"] = ["(none)"]
        self._proj_cb.current(0)

    def _sel_job(self):
        n = self._job_var.get()
        return next((j for j in self._jobs if j["name"] == n), None)

    def _parse_times(self):
        try:
            d = self._date_var.get().strip()
            s = self._start_var.get().strip()
            e = self._end_var.get().strip()
            start = datetime.strptime(f"{d} {s}", "%Y-%m-%d %H:%M")
            end   = datetime.strptime(f"{d} {e}", "%Y-%m-%d %H:%M")
            return start, end
        except ValueError:
            return None, None

    def _update_dur(self):
        start, end = self._parse_times()
        if start is None:
            self._dur_lbl.config(text="—", fg="#888")
        elif end <= start:
            self._dur_lbl.config(text="end must be after start", fg="#c62828")
        else:
            secs = (end - start).total_seconds()
            self._dur_lbl.config(text=fmt_hm(secs), fg="#1565C0")

    def _log(self):
        job = self._sel_job()
        if not job:
            messagebox.showwarning("No Job", "Please select a job.", parent=self)
            return
        start, end = self._parse_times()
        if start is None:
            messagebox.showwarning("Invalid time",
                "Use YYYY-MM-DD for the date and HH:MM for start/end.", parent=self)
            return
        if end <= start:
            messagebox.showwarning("Invalid time", "End must be after start.", parent=self)
            return
        if end > datetime.now() + timedelta(minutes=5):
            messagebox.showwarning("Invalid time", "End time can't be in the future.", parent=self)
            return
        idx     = self._proj_cb.current()
        proj    = self._projs_data[idx] if idx >= 0 else None
        proj_id = proj["id"] if proj else None
        notes   = self._notes_var.get().strip() or None
        self.db.log_entry(job["id"], proj_id, start, end, notes)
        self.on_logged()
        self.destroy()


# ── Edit Entry Dialog ─────────────────────────────────────────────────────────

class EditEntryDialog(tk.Toplevel):
    """Edit or delete an existing time entry."""

    def __init__(self, parent, db, entry_id, on_change):
        super().__init__(parent)
        self.db        = db
        self.entry_id  = entry_id
        self.on_change = on_change
        self.title("Edit Entry")
        self.resizable(False, False)
        self.grab_set()
        self._jobs       = db.jobs()
        self._projs_data = []
        entry = db.entry_by_id(entry_id)
        if not entry:
            self.destroy()
            return
        self._entry = entry
        self._build(entry)
        cx = parent.winfo_screenwidth()  // 2 - 170
        cy = parent.winfo_screenheight() // 2 - 150
        self.geometry(f"+{cx}+{cy}")
        self.focus_force()

    def _build(self, entry):
        f = tk.Frame(self, padx=16, pady=12)
        f.pack()

        start_dt = datetime.fromisoformat(entry["start_time"])
        end_dt   = datetime.fromisoformat(entry["end_time"]) if entry["end_time"] else None

        def lbl_row(r, text, widget):
            tk.Label(f, text=text, anchor="w", width=9).grid(row=r, column=0, sticky="w", pady=4)
            widget.grid(row=r, column=1, pady=4, sticky="w")

        # Job
        self._job_var = tk.StringVar()
        self._job_cb  = ttk.Combobox(f, textvariable=self._job_var,
                                      values=[j["name"] for j in self._jobs],
                                      state="readonly", width=24)
        lbl_row(0, "Job", self._job_cb)
        job_names = [j["name"] for j in self._jobs]
        if entry["job_name"] in job_names:
            self._job_cb.current(job_names.index(entry["job_name"]))
        self._job_cb.bind("<<ComboboxSelected>>", lambda _: self._load_projects())

        # Project
        self._proj_var = tk.StringVar()
        self._proj_cb  = ttk.Combobox(f, textvariable=self._proj_var,
                                       state="readonly", width=24)
        lbl_row(1, "Project", self._proj_cb)
        self._load_projects(preselect=entry["project_name"])

        # Date
        self._date_var = tk.StringVar(value=start_dt.strftime("%Y-%m-%d"))
        lbl_row(2, "Date", tk.Entry(f, textvariable=self._date_var, width=14))
        tk.Label(f, text="YYYY-MM-DD", fg="#888", font=("Segoe UI", 8)).grid(
            row=2, column=2, padx=(4, 0), sticky="w")

        # Start / End
        self._start_var = tk.StringVar(value=start_dt.strftime("%H:%M"))
        self._end_var   = tk.StringVar(value=end_dt.strftime("%H:%M") if end_dt else "")
        lbl_row(3, "Start", tk.Entry(f, textvariable=self._start_var, width=8))
        lbl_row(4, "End",   tk.Entry(f, textvariable=self._end_var,   width=8))
        tk.Label(f, text="HH:MM", fg="#888", font=("Segoe UI", 8)).grid(
            row=3, column=2, padx=(4, 0), sticky="w")
        tk.Label(f, text="leave blank if still running", fg="#888",
                 font=("Segoe UI", 8)).grid(row=4, column=2, padx=(4, 0), sticky="w")

        # Duration
        tk.Label(f, text="Duration", anchor="w", width=9).grid(row=5, column=0, sticky="w", pady=4)
        self._dur_lbl = tk.Label(f, text="", font=("Segoe UI", 9, "bold"), fg="#1565C0")
        self._dur_lbl.grid(row=5, column=1, sticky="w", pady=4)

        # Notes
        self._notes_var = tk.StringVar(value=entry["notes"] or "")
        lbl_row(6, "Notes", tk.Entry(f, textvariable=self._notes_var, width=26))

        for var in (self._date_var, self._start_var, self._end_var):
            var.trace_add("write", lambda *_: self._update_dur())
        self._update_dur()

        # Buttons
        bf = tk.Frame(f)
        bf.grid(row=7, column=0, columnspan=3, pady=(14, 0))
        ttk.Button(bf, text="Save Changes", command=self._save).pack(side="left", padx=4)
        ttk.Button(bf, text="Cancel",       command=self.destroy).pack(side="left", padx=4)
        tk.Button(bf, text="Delete Entry", command=self._delete,
                  fg="white", bg="#c62828", relief="flat",
                  padx=6, pady=2).pack(side="right", padx=4)

    def _load_projects(self, preselect=None):
        job = self._sel_job()
        if job:
            projs = self.db.projects(job["id"])
            self._projs_data = [None] + list(projs)
            self._proj_cb["values"] = ["(none)"] + [p["name"] for p in projs]
        else:
            self._projs_data = [None]
            self._proj_cb["values"] = ["(none)"]
        names = [p["name"] for p in self._projs_data if p]
        if preselect and preselect in names:
            self._proj_cb.current(names.index(preselect) + 1)
        else:
            self._proj_cb.current(0)

    def _sel_job(self):
        n = self._job_var.get()
        return next((j for j in self._jobs if j["name"] == n), None)

    def _parse_times(self):
        try:
            d = self._date_var.get().strip()
            s = self._start_var.get().strip()
            start = datetime.strptime(f"{d} {s}", "%Y-%m-%d %H:%M")
            e_str = self._end_var.get().strip()
            end   = datetime.strptime(f"{d} {e_str}", "%Y-%m-%d %H:%M") if e_str else None
            return start, end
        except ValueError:
            return None, None

    def _update_dur(self):
        start, end = self._parse_times()
        if start is None:
            self._dur_lbl.config(text="—", fg="#888")
        elif end is None:
            self._dur_lbl.config(text="still running", fg="#43A047")
        elif end <= start:
            self._dur_lbl.config(text="end must be after start", fg="#c62828")
        else:
            self._dur_lbl.config(text=fmt_hm((end - start).total_seconds()), fg="#1565C0")

    def _save(self):
        job = self._sel_job()
        if not job:
            messagebox.showwarning("No Job", "Please select a job.", parent=self)
            return
        start, end = self._parse_times()
        if start is None:
            messagebox.showwarning("Invalid time",
                "Use YYYY-MM-DD for the date and HH:MM for times.", parent=self)
            return
        if end is not None and end <= start:
            messagebox.showwarning("Invalid time", "End must be after start.", parent=self)
            return
        idx     = self._proj_cb.current()
        proj    = self._projs_data[idx] if idx >= 0 else None
        proj_id = proj["id"] if proj else None
        notes   = self._notes_var.get().strip() or None
        self.db.update_entry(self.entry_id, job["id"], proj_id, start, end, notes)
        self.on_change()
        self.destroy()

    def _delete(self):
        if not messagebox.askyesno("Delete Entry",
                "Permanently delete this time entry?", parent=self):
            return
        self.db.delete_entry(self.entry_id)
        self.on_change()
        self.destroy()


# ── Invoice Settings Dialog ───────────────────────────────────────────────────

class InvoiceSettingsDialog(tk.Toplevel):
    _GENERIC_TABS = [
        (" Bank Details ", [
            ("Account Name",   "bank_account_name"),
            ("Bank Name",      "bank_name"),
            ("Account Number", "bank_account_number"),
            ("Sort Code",      "bank_sort_code"),
            ("IBAN",           "bank_iban"),
            ("BIC / SWIFT",    "bank_bic"),
        ]),
        (" Defaults ", [
            ("Currency Symbol", "currency"),
            ("Default Rate/hr", "default_rate"),
            ("Invoice Prefix",  "invoice_prefix"),
            ("Payment Terms",   "payment_terms"),
        ]),
    ]
    _DEFAULTS = {
        "currency": "£", "default_rate": "0.00",
        "invoice_prefix": "INV-",
        "payment_terms": "Payment due within 30 days",
    }
    # Business text fields: (label, settings key, placeholder example)
    _BIZ_FIELDS = [
        ("Business Name", "biz_name",       "Acme Consulting Ltd"),
        ("Contact Name",  "biz_contact",    "Jane Smith"),
        ("Address Line 1", "biz_addr_line1", "123 Example St"),
        ("Address Line 2", "biz_addr_line2", "Suite 4"),
        ("City",          "biz_city",       "London"),
        ("State/County",  "biz_county",     "Greater London"),
        ("Postcode/ZIP",  "biz_postcode",   "SW1A 1AA"),
        ("Email",         "biz_email",      "you@example.com"),
    ]
    _ADDR_KEYS = ["biz_addr_line1", "biz_addr_line2", "biz_city",
                  "biz_county", "biz_postcode"]

    def __init__(self, parent, db):
        super().__init__(parent)
        self.db = db
        self.title("Invoice Settings")
        self.resizable(False, False)
        self.grab_set()
        self._vars = {}          # generic StringVars (bank/defaults)
        self._biz_entries = {}   # key -> PlaceholderEntry
        self._build()
        cx = parent.winfo_screenwidth()  // 2 - 230
        cy = parent.winfo_screenheight() // 2 - 200
        self.geometry(f"+{cx}+{cy}")
        self.focus_force()

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=8)

        # ── Business tab (custom) ──
        bf = tk.Frame(nb, padx=16, pady=10)
        nb.add(bf, text=" Business ")
        for row, (label, key, ph) in enumerate(self._BIZ_FIELDS):
            tk.Label(bf, text=label + ":", anchor="w", width=16).grid(
                row=row, column=0, sticky="w", pady=4)
            entry = PlaceholderEntry(bf, placeholder=ph, width=30)
            entry.grid(row=row, column=1, sticky="w", pady=4, padx=(4, 0), columnspan=2)
            existing = self.db.get_setting(key)
            if existing:
                entry.set_value(existing)
            self._biz_entries[key] = entry

        # Country combobox (editable, defaults to United Kingdom)
        crow = len(self._BIZ_FIELDS)
        tk.Label(bf, text="Country:", anchor="w", width=16).grid(
            row=crow, column=0, sticky="w", pady=4)
        self._country_var = tk.StringVar(
            value=self.db.get_setting("biz_country") or "United Kingdom")
        ttk.Combobox(bf, textvariable=self._country_var, values=country_names(),
                     width=28).grid(row=crow, column=1, sticky="w", pady=4,
                                    padx=(4, 0), columnspan=2)

        # Phone: dial-code combobox + number
        prow = crow + 1
        tk.Label(bf, text="Phone:", anchor="w", width=16).grid(
            row=prow, column=0, sticky="w", pady=4)
        self._phone_code_var = tk.StringVar(
            value=label_for_code(self.db.get_setting("biz_phone_code") or "+44"))
        ttk.Combobox(bf, textvariable=self._phone_code_var, values=dial_labels(),
                     state="readonly", width=18).grid(row=prow, column=1,
                                                      sticky="w", pady=4, padx=(4, 0))
        self._phone_entry = PlaceholderEntry(bf, placeholder="7700 900123", width=14)
        self._phone_entry.grid(row=prow, column=2, sticky="w", pady=4, padx=(4, 0))
        existing_num = self.db.get_setting("biz_phone_number")
        if existing_num:
            self._phone_entry.set_value(existing_num)

        self._migrate_legacy()

        # ── Generic tabs ──
        for tab_label, fields in self._GENERIC_TABS:
            f = tk.Frame(nb, padx=16, pady=10)
            nb.add(f, text=tab_label)
            for row, (label, key) in enumerate(fields):
                tk.Label(f, text=label + ":", anchor="w", width=16).grid(
                    row=row, column=0, sticky="w", pady=4)
                default = self.db.get_setting(key) or self._DEFAULTS.get(key, "")
                var = tk.StringVar(value=default)
                self._vars[key] = var
                tk.Entry(f, textvariable=var, width=30).grid(
                    row=row, column=1, sticky="w", pady=4, padx=(4, 0))

        bar = tk.Frame(self)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(bar, text="Save",   command=self._save).pack(side="right", padx=4)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=4)

    def _migrate_legacy(self):
        """One-time convenience: seed new fields from legacy single-blob settings."""
        structured = any(self.db.get_setting(k) for k in self._ADDR_KEYS)
        legacy_addr = self.db.get_setting("biz_address")
        if not structured and legacy_addr:
            self._biz_entries["biz_addr_line1"].set_value(
                ", ".join(part.strip() for part in legacy_addr.splitlines() if part.strip()))
        if not self.db.get_setting("biz_phone_number"):
            legacy_phone = self.db.get_setting("biz_phone")
            if legacy_phone:
                self._phone_entry.set_value(legacy_phone)

    def _save(self):
        for key, entry in self._biz_entries.items():
            self.db.set_setting(key, entry.get_value().strip())
        self.db.set_setting("biz_country", self._country_var.get().strip())
        self.db.set_setting("biz_phone_code", code_from_label(self._phone_code_var.get()))
        self.db.set_setting("biz_phone_number", self._phone_entry.get_value().strip())
        for key, var in self._vars.items():
            self.db.set_setting(key, var.get().strip())
        self.destroy()


# ── Generate Invoice Dialog ───────────────────────────────────────────────────

class GenerateInvoiceDialog(tk.Toplevel):
    _PERIODS = [
        "Last Pay Period",    # biweekly, most recent completed Thu→Thu window
        "This Pay Period",    # biweekly, current in-progress Thu→today window
        "This Week", "Last Week",
        "This Month", "Last Month",
        "Custom",
    ]

    def __init__(self, parent, db):
        super().__init__(parent)
        self.db    = db
        self._rows = []
        self.title("Generate Invoice")
        self.geometry("760x650")
        self.minsize(680, 560)
        self.resizable(True, True)
        self.grab_set()
        self._build()
        self._load_items()
        cx = parent.winfo_screenwidth()  // 2 - 380
        cy = parent.winfo_screenheight() // 2 - 325
        self.geometry(f"+{cx}+{cy}")
        self.focus_force()

    def _build(self):
        # Period selection
        pf = tk.LabelFrame(self, text=" Period ", padx=8, pady=6)
        pf.pack(fill="x", padx=12, pady=(10,4))

        self._period_var = tk.StringVar(value="Last Pay Period")
        pcb = ttk.Combobox(pf, textvariable=self._period_var,
                            values=self._PERIODS, state="readonly", width=14)
        pcb.pack(side="left")
        pcb.bind("<<ComboboxSelected>>", self._on_period_change)

        self._range_lbl = tk.Label(pf, text="", fg="#555", font=("Segoe UI", 9))
        self._range_lbl.pack(side="left", padx=(12,0))

        self._cf = tk.Frame(pf)
        tk.Label(self._cf, text="From:").pack(side="left", padx=(12,2))
        self._from_var = tk.StringVar(value=date.today().replace(day=1).isoformat())
        tk.Entry(self._cf, textvariable=self._from_var, width=11).pack(side="left")
        tk.Label(self._cf, text="To:").pack(side="left", padx=(6,2))
        self._to_var = tk.StringVar(value=date.today().isoformat())
        tk.Entry(self._cf, textvariable=self._to_var, width=11).pack(side="left")
        ttk.Button(self._cf, text="Load", command=self._load_items).pack(side="left", padx=(6,0))

        # Line items
        items_lf = tk.LabelFrame(self, text=" Line Items  (edit Rate to adjust pricing) ",
                                  padx=4, pady=4)
        items_lf.pack(fill="both", expand=True, padx=12, pady=4)

        self._items_canvas = tk.Canvas(items_lf, bg="white", height=190, highlightthickness=0)
        vsb = ttk.Scrollbar(items_lf, orient="vertical", command=self._items_canvas.yview)
        self._items_canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._items_canvas.pack(fill="both", expand=True)
        self._items_frame = tk.Frame(self._items_canvas, bg="white")
        self._canvas_win  = self._items_canvas.create_window((0,0), window=self._items_frame,
                                                              anchor="nw")
        self._items_frame.bind("<Configure>",
            lambda e: self._items_canvas.configure(
                scrollregion=self._items_canvas.bbox("all")))
        self._items_canvas.bind("<Configure>",
            lambda e: self._items_canvas.itemconfig(self._canvas_win, width=e.width))
        self._items_canvas.bind("<MouseWheel>",
            lambda e: self._items_canvas.yview_scroll(-1*(e.delta//120), "units"))

        # Client + invoice details side by side
        mid = tk.Frame(self)
        mid.pack(fill="x", padx=12, pady=4)

        client_lf = tk.LabelFrame(mid, text=" Bill To ", padx=8, pady=6)
        client_lf.pack(side="left", fill="both", expand=True, padx=(0, 4))

        self._client_name = PlaceholderEntry(client_lf, placeholder="Client Ltd", width=26)
        self._client_fields = {}
        client_rows = [
            ("Name",         self._client_name),
            ("Address 1",    PlaceholderEntry(client_lf, placeholder="123 Example St", width=26)),
            ("Address 2",    PlaceholderEntry(client_lf, placeholder="Suite 4", width=26)),
            ("City",         PlaceholderEntry(client_lf, placeholder="London", width=26)),
            ("State/County", PlaceholderEntry(client_lf, placeholder="Greater London", width=26)),
            ("Postcode/ZIP", PlaceholderEntry(client_lf, placeholder="SW1A 1AA", width=26)),
        ]
        for row, (label, widget) in enumerate(client_rows):
            tk.Label(client_lf, text=label + ":", anchor="w").grid(
                row=row, column=0, sticky="w", pady=2)
            widget.grid(row=row, column=1, sticky="w", pady=2, padx=(4, 0))
            if label != "Name":
                self._client_fields[label] = widget

        tk.Label(client_lf, text="Country:", anchor="w").grid(
            row=len(client_rows), column=0, sticky="w", pady=2)
        self._client_country = tk.StringVar(value="United Kingdom")
        ttk.Combobox(client_lf, textvariable=self._client_country,
                     values=country_names(), width=24).grid(
            row=len(client_rows), column=1, sticky="w", pady=2, padx=(4, 0))

        inv_lf = tk.LabelFrame(mid, text=" Invoice Details ", padx=8, pady=6)
        inv_lf.pack(side="left", fill="both", expand=True)

        prefix  = self.db.get_setting("invoice_prefix", "INV-")
        inv_num = self.db.next_invoice_number(prefix)
        inv_fields = [
            ("Invoice #",  inv_num),
            ("Issue Date", date.today().isoformat()),
            ("Due Date",   (date.today() + timedelta(days=30)).isoformat()),
            ("Tax %",      "0"),
        ]
        self._inv_vars = {}
        for row, (lbl, default) in enumerate(inv_fields):
            tk.Label(inv_lf, text=lbl+":", anchor="w", width=10).grid(
                row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=default)
            self._inv_vars[lbl] = var
            tk.Entry(inv_lf, textvariable=var, width=18).grid(
                row=row, column=1, sticky="w", pady=3, padx=(4,0))

        self._sub_lbl   = tk.Label(inv_lf, text="Subtotal:  —", anchor="e",
                                    fg="#333", font=("Segoe UI", 9))
        self._tax_lbl   = tk.Label(inv_lf, text="Tax:  —",      anchor="e",
                                    fg="#333", font=("Segoe UI", 9))
        self._total_lbl = tk.Label(inv_lf, text="Total:  —",    anchor="e",
                                    font=("Segoe UI", 10, "bold"), fg="#1565C0")
        for r, lbl in enumerate((self._sub_lbl, self._tax_lbl, self._total_lbl), start=4):
            lbl.grid(row=r, column=0, columnspan=2, sticky="e", pady=1)

        self._inv_vars["Tax %"].trace_add("write", lambda *_: self._recalc())

        # Notes + buttons
        bot = tk.Frame(self)
        bot.pack(fill="x", padx=12, pady=(4,10))
        tk.Label(bot, text="Notes:").pack(side="left", padx=(0,4))
        self._notes_var = tk.StringVar(value=self.db.get_setting("payment_terms",""))
        tk.Entry(bot, textvariable=self._notes_var, width=46).pack(side="left")
        ttk.Button(bot, text="Cancel",       command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bot, text="Generate PDF", command=self._generate).pack(side="right", padx=4)

    def _on_period_change(self, _=None):
        if self._period_var.get() == "Custom":
            self._cf.pack(side="left", padx=(8,0))
        else:
            self._cf.pack_forget()
            self._load_items()

    def _get_date_range(self):
        p     = self._period_var.get()
        today = date.today()
        if p == "This Pay Period":
            return biweekly_bounds(0)
        elif p == "Last Pay Period":
            return biweekly_bounds(-1)
        elif p == "This Week":
            start = today - timedelta(days=today.weekday())
            return start.isoformat(), (today + timedelta(days=1)).isoformat()
        elif p == "Last Week":
            mon = today - timedelta(days=today.weekday() + 7)
            return mon.isoformat(), (mon + timedelta(days=7)).isoformat()
        elif p == "This Month":
            return date(today.year, today.month, 1).isoformat(), \
                   (today + timedelta(days=1)).isoformat()
        elif p == "Last Month":
            first_this  = date(today.year, today.month, 1)
            last_mo_end = first_this - timedelta(days=1)
            return date(last_mo_end.year, last_mo_end.month, 1).isoformat(), \
                   first_this.isoformat()
        else:
            return self._from_var.get().strip(), self._to_var.get().strip()

    def _load_items(self):
        for w in self._items_frame.winfo_children():
            w.destroy()
        self._rows.clear()

        start, end = self._get_date_range()
        try:
            s_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%d %b %Y")
            e_fmt = datetime.strptime(end,   "%Y-%m-%d").strftime("%d %b %Y")
            self._range_lbl.config(text=f"{s_fmt} – {e_fmt}")
        except ValueError:
            self._range_lbl.config(text="")

        cur = self.db.get_setting("currency", "£")
        try:
            default_rate = float(self.db.get_setting("default_rate","0") or "0")
        except ValueError:
            default_rate = 0.0

        entries = self.db.invoice_line_items(start, end)

        hf = tk.Frame(self._items_frame, bg="#1565C0")
        hf.pack(fill="x")
        for txt, w in [("Job",18),("Project",15),("Hours",7),("Rate / hr",12),("Amount",10)]:
            tk.Label(hf, text=txt, bg="#1565C0", fg="white",
                     font=("Segoe UI", 9, "bold"), width=w,
                     anchor="w", padx=6).pack(side="left")

        if not entries:
            tk.Label(self._items_frame,
                     text="No un-invoiced entries found for this period.\n"
                          "Entries already included in a previous invoice are excluded.",
                     fg="#888", pady=14, justify="center").pack()
            self._recalc()
            return

        for i, e in enumerate(entries):
            bg       = "#F0F4FF" if i % 2 == 0 else "white"
            rf       = tk.Frame(self._items_frame, bg=bg)
            rf.pack(fill="x")
            hours    = math.ceil(float(e["hours"] or 0.0) * 4) / 4  # round up to nearest 15 min
            rate     = float(e["hourly_rate"]) if e["hourly_rate"] is not None else default_rate
            rate_var = tk.StringVar(value=f"{rate:.2f}")
            amount   = hours * rate

            tk.Label(rf, text=str(e["job_name"])[:22], bg=bg, width=18,
                     anchor="w", padx=6, font=("Segoe UI",9)).pack(side="left")
            tk.Label(rf, text=str(e["project_name"] or "—")[:18], bg=bg, width=15,
                     anchor="w", padx=6, font=("Segoe UI",9)).pack(side="left")
            tk.Label(rf, text=f"{hours:.2f}h", bg=bg, width=7,
                     anchor="e", padx=4, font=("Segoe UI",9)).pack(side="left")
            tk.Entry(rf, textvariable=rate_var, width=10,
                     justify="right", font=("Segoe UI",9)).pack(side="left", padx=4, pady=2)
            amount_lbl = tk.Label(rf, text=f"{cur}{amount:.2f}", bg=bg, width=10,
                                   anchor="e", padx=6, font=("Segoe UI",9,"bold"))
            amount_lbl.pack(side="left")

            row_data = {"job": e["job_name"], "project": e["project_name"],
                        "hours": hours, "rate_var": rate_var, "amount_lbl": amount_lbl}
            rate_var.trace_add("write", lambda *_, rd=row_data: self._update_row(rd))
            self._rows.append(row_data)

        self._recalc()

    def _update_row(self, rd):
        cur = self.db.get_setting("currency", "£")
        try:
            rd["amount_lbl"].config(
                text=f"{cur}{rd['hours'] * float(rd['rate_var'].get()):.2f}")
        except ValueError:
            rd["amount_lbl"].config(text="—")
        self._recalc()

    def _recalc(self):
        cur = self.db.get_setting("currency", "£")
        subtotal = 0.0
        for rd in self._rows:
            try:
                subtotal += rd["hours"] * float(rd["rate_var"].get())
            except ValueError:
                pass
        try:
            tax_rate = float(self._inv_vars["Tax %"].get() or "0")
        except ValueError:
            tax_rate = 0.0
        tax_amount = subtotal * tax_rate / 100
        total      = subtotal + tax_amount
        self._sub_lbl  .config(text=f"Subtotal:  {cur}{subtotal:.2f}")
        self._tax_lbl  .config(text=f"Tax ({tax_rate:.1f}%):  {cur}{tax_amount:.2f}")
        self._total_lbl.config(text=f"Total:  {cur}{total:.2f}")

    def _generate(self):
        if not _REPORTLAB:
            messagebox.showerror("Missing dependency",
                "reportlab is not installed.\n\n"
                "Run in the project folder:\n"
                "  .venv\\Scripts\\pip install reportlab",
                parent=self)
            return
        client = self._client_name.get_value().strip()
        if not client:
            messagebox.showwarning("Missing", "Please enter a client name.", parent=self)
            return
        if not self._rows:
            messagebox.showwarning("No data", "No line items to invoice.", parent=self)
            return

        cur = self.db.get_setting("currency", "£")
        try:
            tax_rate = float(self._inv_vars["Tax %"].get() or "0")
        except ValueError:
            tax_rate = 0.0

        line_items = []
        subtotal   = 0.0
        for rd in self._rows:
            try:
                rate = float(rd["rate_var"].get())
            except ValueError:
                rate = 0.0
            amount = rd["hours"] * rate
            subtotal += amount
            line_items.append({"job": rd["job"], "project": rd["project"],
                                "hours": rd["hours"], "rate": rate, "amount": amount})

        tax_amount = subtotal * tax_rate / 100
        total      = subtotal + tax_amount
        start, end = self._get_date_range()

        biz_addr = compose_business_address(
            [self.db.get_setting(k, "") for k in
             ("biz_addr_line1", "biz_addr_line2", "biz_city",
              "biz_county", "biz_postcode")],
            self.db.get_setting("biz_country", ""),
            self.db.get_setting("biz_address", ""),
        )
        biz_phone = compose_phone(
            self.db.get_setting("biz_phone_code", ""),
            self.db.get_setting("biz_phone_number", ""),
        ) or self.db.get_setting("biz_phone", "")
        client_addr = compose_address(
            [self._client_fields["Address 1"].get_value(),
             self._client_fields["Address 2"].get_value(),
             self._client_fields["City"].get_value(),
             self._client_fields["State/County"].get_value(),
             self._client_fields["Postcode/ZIP"].get_value(),
             self._client_country.get()],
            sep="\n",
        )

        pdf_data = {
            "biz_name":            self.db.get_setting("biz_name",""),
            "biz_address":         biz_addr,
            "biz_email":           self.db.get_setting("biz_email",""),
            "biz_phone":           biz_phone,
            "bank_account_name":   self.db.get_setting("bank_account_name",""),
            "bank_name":           self.db.get_setting("bank_name",""),
            "bank_account_number": self.db.get_setting("bank_account_number",""),
            "bank_sort_code":      self.db.get_setting("bank_sort_code",""),
            "bank_iban":           self.db.get_setting("bank_iban",""),
            "bank_bic":            self.db.get_setting("bank_bic",""),
            "currency":            cur,
            "client_name":         client,
            "client_address":      client_addr,
            "invoice_number":      self._inv_vars["Invoice #"].get().strip(),
            "issue_date":          self._inv_vars["Issue Date"].get().strip(),
            "due_date":            self._inv_vars["Due Date"].get().strip(),
            "line_items":          line_items,
            "subtotal":            subtotal,
            "tax_rate":            tax_rate,
            "tax_amount":          tax_amount,
            "total":               total,
            "notes":               self._notes_var.get().strip(),
            "period_start":        start,
            "period_end":          end,
        }

        inv_dir  = DATA_DIR / "invoices"
        inv_dir.mkdir(exist_ok=True)
        safe_num = pdf_data["invoice_number"].replace("/","_").replace("\\","_")
        out_path = inv_dir / f"{safe_num}.pdf"

        try:
            generate_invoice_pdf(pdf_data, out_path)
        except Exception as exc:
            messagebox.showerror("PDF Error", f"Failed to generate PDF:\n{exc}", parent=self)
            return

        self.db.save_invoice(pdf_data["invoice_number"], client,
                             start, end, total, out_path, line_items)
        try:
            plat.open_file(out_path)
        except Exception:
            pass

        messagebox.showinfo("Invoice Generated", f"Saved to:\n{out_path}", parent=self)
        self.destroy()


# ── Manage Jobs Window ────────────────────────────────────────────────────────

class ManageJobsWindow(tk.Toplevel):
    """Tree view of jobs and their projects with context-aware add/rename/delete."""

    def __init__(self, parent, db, on_change):
        super().__init__(parent)
        self.db        = db
        self.on_change = on_change
        self.title("Jobs & Projects")
        self.geometry("380x420")
        self.minsize(320, 300)
        self.grab_set()
        self._build()
        self._load()

    def _build(self):
        # Tree fills most of the window
        tree_frame = tk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self._tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(tree_frame, command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._tree.tag_configure("job",  font=("Segoe UI", 9, "bold"))
        self._tree.tag_configure("proj", foreground="#444444")

        self._tree.bind("<Double-1>", lambda _: self._rename())

        # Button row along the bottom
        btn = tk.Frame(self)
        btn.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Button(btn, text="+ Job",        command=self._add_job).pack(side="left",  padx=2)
        ttk.Button(btn, text="+ Project",    command=self._add_proj).pack(side="left", padx=2)
        ttk.Button(btn, text="Rename",       command=self._rename).pack(side="left",   padx=2)
        ttk.Button(btn, text="Delete",       command=self._delete).pack(side="right",  padx=2)

    # ── Data ─────────────────────────────────────────────────────────────────

    def _load(self, reopen=None):
        # Remember which job nodes were expanded
        expanded = {iid for iid in self._tree.get_children()
                    if self._tree.item(iid, "open")}
        selected = self._tree.selection()

        self._tree.delete(*self._tree.get_children())
        for job in self.db.jobs():
            jid = f"j{job['id']}"
            open_ = jid in expanded or jid == reopen
            self._tree.insert("", "end", iid=jid, text=f"  {job['name']}",
                              open=open_, tags=("job",))
            for proj in self.db.projects(job["id"]):
                self._tree.insert(jid, "end", iid=f"p{proj['id']}",
                                  text=f"    {proj['name']}", tags=("proj",))

        # Restore selection if it still exists
        if selected and self._tree.exists(selected[0]):
            self._tree.selection_set(selected[0])

    # ── Selection helpers ─────────────────────────────────────────────────────

    def _sel(self):
        s = self._tree.selection()
        return s[0] if s else None

    def _sel_kind(self):
        iid = self._sel()
        if not iid:
            return None, None
        return ("job", int(iid[1:])) if iid.startswith("j") else ("proj", int(iid[1:]))

    def _job_id_for_sel(self):
        """Return the job id regardless of whether a job or project is selected."""
        iid = self._sel()
        if not iid:
            return None
        if iid.startswith("j"):
            return int(iid[1:])
        parent = self._tree.parent(iid)
        return int(parent[1:]) if parent else None

    # ── Actions ───────────────────────────────────────────────────────────────

    def _add_job(self):
        name = ask_string(self, "Add Job", "Job name:")
        if not name:
            return
        all_jobs = list(self.db.jobs())
        try:
            self.db.add_job(name, JOB_COLORS[len(all_jobs) % len(JOB_COLORS)])
        except sqlite3.IntegrityError:
            messagebox.showerror("Duplicate", "A job with that name already exists.", parent=self)
            return
        self._load()
        self.on_change()

    def _add_proj(self):
        jobs = list(self.db.jobs())
        job_ids = [j["id"] for j in jobs]
        action, job_id = resolve_project_job(self._job_id_for_sel(), job_ids)

        if action == "empty":
            messagebox.showinfo(
                "No jobs yet",
                "Add a job first, then you can add projects under it.",
                parent=self,
            )
            return
        if action == "choose":
            names = [j["name"] for j in jobs]
            idx = ask_choice(self, "Add Project", "Which job is this project under?", names)
            if idx is None:
                return
            job_id = jobs[idx]["id"]

        job_name = next(j["name"] for j in jobs if j["id"] == job_id)
        name = ask_string(self, "Add Project", f"Project name  (under '{job_name}'):")
        if not name:
            return
        self.db.add_project(job_id, name)
        self._load(reopen=f"j{job_id}")
        self.on_change()

    def _rename(self):
        kind, eid = self._sel_kind()
        if not kind:
            return
        current = self._tree.item(self._sel(), "text").strip()
        label   = "Job" if kind == "job" else "Project"
        new_name = ask_string(self, f"Rename {label}", f"New name for '{current}':", initial=current)
        if not new_name or new_name == current:
            return
        if kind == "job":
            job = next((j for j in self.db.jobs() if j["id"] == eid), None)
            if job:
                self.db.update_job(eid, new_name, job["color"])
        else:
            self.db.update_project(eid, new_name)
        self._load()
        self.on_change()

    def _delete(self):
        kind, eid = self._sel_kind()
        if not kind:
            return
        name  = self._tree.item(self._sel(), "text").strip()
        label = "Job" if kind == "job" else "Project"
        extra = "\n\nAll time entries for this job will also be hidden." if kind == "job" else ""
        if not messagebox.askyesno("Delete", f"Delete {label.lower()} '{name}'?{extra}", parent=self):
            return
        if kind == "job":
            self.db.archive_job(eid)
        else:
            self.db.archive_project(eid)
        self._load()
        self.on_change()


# ── Week Calendar Tab ─────────────────────────────────────────────────────────

class WeekCalendarTab:
    TIME_W    = 50
    COL_W     = 90
    HOUR_H    = 56
    HDR_H     = 36
    DAY_START = 6
    DAY_END   = 22

    def __init__(self, parent, db):
        self.db = db
        today = date.today()
        self._week_start = today - timedelta(days=today.weekday())
        self._build(parent)
        self.draw(reset_scroll=True)

    def _build(self, parent):
        nav = tk.Frame(parent, pady=4)
        nav.pack(fill="x", padx=8)
        ttk.Button(nav, text="◀", width=3, command=self._prev).pack(side="left")
        self._nav_lbl = tk.Label(nav, text="", font=("Segoe UI", 10, "bold"))
        self._nav_lbl.pack(side="left", padx=12)
        ttk.Button(nav, text="▶", width=3, command=self._next).pack(side="left")
        ttk.Button(nav, text="Today", command=self._go_today).pack(side="left", padx=(12, 0))

        outer = tk.Frame(parent)
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._hdr = tk.Canvas(outer, height=self.HDR_H, bg="#EEEEEE",
                               highlightthickness=0)
        self._hdr.pack(fill="x")

        inner = tk.Frame(outer)
        inner.pack(fill="both", expand=True)

        total_w = self.TIME_W + 7 * self.COL_W
        total_h = (self.DAY_END - self.DAY_START) * self.HOUR_H
        self._canvas = tk.Canvas(inner, bg="white",
                                  scrollregion=(0, 0, total_w, total_h),
                                  highlightthickness=0)
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.bind(
            "<MouseWheel>",
            lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"),
        )

    def _prev(self):
        self._week_start -= timedelta(weeks=1)
        self.draw(reset_scroll=True)

    def _next(self):
        self._week_start += timedelta(weeks=1)
        self.draw(reset_scroll=True)

    def _go_today(self):
        today = date.today()
        self._week_start = today - timedelta(days=today.weekday())
        self.draw(reset_scroll=True)

    def draw(self, reset_scroll=False):
        ws = self._week_start
        we = ws + timedelta(days=7)
        self._nav_lbl.config(text=f"Week of {ws.strftime('%B %d, %Y')}")

        TW = self.TIME_W
        W  = self.COL_W
        HH = self.HDR_H
        H  = self.HOUR_H
        DS = self.DAY_START
        DE = self.DAY_END

        saved = self._canvas.yview()[0] if not reset_scroll else None

        # Header
        hdr = self._hdr
        hdr.delete("all")
        today_d = date.today()
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        hdr.create_rectangle(0, 0, TW, HH, fill="#EEEEEE", outline="#cccccc")
        for i, name in enumerate(days):
            d  = ws + timedelta(days=i)
            x  = TW + i * W
            bg = "#1565C0" if d == today_d else "#EEEEEE"
            fg = "white"   if d == today_d else "#333333"
            hdr.create_rectangle(x, 0, x + W, HH, fill=bg, outline="#cccccc")
            hdr.create_text(x + W // 2, HH // 2,
                            text=f"{name}  {d.strftime('%b %d')}",
                            font=("Segoe UI", 9, "bold"), fill=fg)

        # Time grid
        c = self._canvas
        c.delete("all")
        for row in range(DE - DS):
            hour = DS + row
            y    = row * H
            c.create_rectangle(0, y, TW, y + H, fill="#F7F7F7", outline="#dddddd")
            c.create_text(TW - 5, y + 4, text=f"{hour:02d}:00",
                          anchor="ne", font=("Segoe UI", 8), fill="#888888")
            for col in range(7):
                x    = TW + col * W
                fill = "#F9F9FB" if col >= 5 else "white"
                c.create_rectangle(x, y, x + W, y + H, fill=fill, outline="#EBEBEB")
                c.create_line(x + 2, y + H // 2, x + W - 2, y + H // 2, fill="#F0F0F0")

        # Current-time indicator
        now = datetime.now()
        if ws <= now.date() < we:
            idx  = (now.date() - ws).days
            frac = now.hour + now.minute / 60
            if DS <= frac < DE:
                ty = (frac - DS) * H
                x1 = TW + idx * W
                c.create_oval(x1 - 4, ty - 4, x1 + 4, ty + 4, fill="#E53935", outline="")
                c.create_line(x1, ty, x1 + W, ty, fill="#E53935", width=2)

        # Entries
        for e in self.db.week_entries(ws.isoformat(), we.isoformat()):
            start_dt = datetime.fromisoformat(e["start_time"])
            end_dt   = (datetime.fromisoformat(e["end_time"])
                        if e["end_time"] else datetime.now())
            idx = (start_dt.date() - ws).days
            if not (0 <= idx < 7):
                continue
            sf = max(start_dt.hour + start_dt.minute / 60, float(DS))
            ef = min(end_dt.hour   + end_dt.minute   / 60, float(DE))
            if ef <= sf:
                continue
            x  = TW + idx * W + 2
            y1 = (sf - DS) * H
            y2 = (ef - DS) * H
            bw = W - 4
            c.create_rectangle(x, y1, x + bw, y2, fill=e["color"], outline="white", width=1)
            bh = y2 - y1
            if bh >= 16:
                if bh < 32 or not e["project_name"]:
                    label = e["job_name"]
                else:
                    label = f"{e['job_name']}\n{e['project_name']}"
                c.create_text(x + 4, (y1 + y2) / 2, text=label,
                              anchor="w", font=("Segoe UI", 8),
                              fill="white", width=bw - 8)

        # Scroll position
        if saved is not None:
            c.yview_moveto(saved)
        else:
            c.yview_moveto(max(0.0, (8 - DS) / (DE - DS) - 0.02))


# ── Tax Overview Tab ──────────────────────────────────────────────────────────

class TaxOverviewTab:
    _BAND_BG  = ["#C8E6C9", "#FFE0B2", "#FFCDD2"]
    _BAND_FG  = ["#388E3C", "#E65100", "#B71C1C"]
    _BAND_LBL = ["Tax-free (0%)", "Basic (20%)", "Higher (40%)"]

    def __init__(self, parent, db):
        self.db      = db
        self._income = 0.0
        self._build(parent)
        self.refresh()

    def _build(self, parent):
        # ── Tax year selector (top) ───────────────────────────────────────────
        ctrl = tk.Frame(parent, pady=6)
        ctrl.pack(side="top", fill="x", padx=12)
        tk.Label(ctrl, text="Tax Year:", font=("Segoe UI", 9)).pack(side="left")
        self._year_var = tk.StringVar()
        s0, _ = uk_tax_year_bounds(0)
        s1, _ = uk_tax_year_bounds(-1)
        y0, y1 = int(s0[:4]), int(s1[:4])
        options = [f"{y0}/{str(y0+1)[2:]}", f"{y1}/{str(y1+1)[2:]}", "All Time"]
        self._year_var.set(options[0])
        cb = ttk.Combobox(ctrl, textvariable=self._year_var, values=options,
                          state="readonly", width=10)
        cb.pack(side="left", padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        ttk.Button(ctrl, text="Refresh", command=self.refresh).pack(side="left", padx=(10, 0))

        # ── Estimated tax liability (bottom) ─────────────────────────────────
        tax_lf = tk.LabelFrame(parent, text=" Estimated Tax Liability ", padx=10, pady=8)
        tax_lf.pack(side="bottom", fill="x", padx=12, pady=(0, 8))

        rows = [
            ("allowance",       "Tax-free allowance (0%):"),
            ("basic_taxable",   "Basic rate income (20%):"),
            ("higher_taxable",  "Higher rate income (40%):"),
            (None,              None),   # separator
            ("basic_tax",       "Basic rate tax:"),
            ("higher_tax",      "Higher rate tax:"),
            (None,              None),   # separator
            ("total_tax",       "Estimated total tax:"),
        ]
        self._tax_lbls = {}
        grid_row = 0
        for key, label in rows:
            if key is None:
                ttk.Separator(tax_lf, orient="horizontal").grid(
                    row=grid_row, column=0, columnspan=2, sticky="ew", pady=3, padx=2)
            else:
                bold = key == "total_tax"
                fg   = "#c62828" if key == "total_tax" else "#333"
                tk.Label(tax_lf, text=label, anchor="w", width=28,
                         font=("Segoe UI", 9, "bold") if bold else ("Segoe UI", 9),
                         fg=fg).grid(row=grid_row, column=0, sticky="w", pady=1)
                val_lbl = tk.Label(tax_lf, text="—", anchor="e", width=18,
                                   font=("Segoe UI", 9, "bold") if bold else ("Segoe UI", 9),
                                   fg=fg)
                val_lbl.grid(row=grid_row, column=1, sticky="e", pady=1, padx=(4, 0))
                self._tax_lbls[key] = val_lbl
            grid_row += 1

        # ── Income vs tax-band canvas (above tax summary) ─────────────────────
        band_lf = tk.LabelFrame(parent, text=" Income vs Tax Bands ", padx=8, pady=6)
        band_lf.pack(side="bottom", fill="x", padx=12, pady=(0, 4))
        self._band_canvas = tk.Canvas(band_lf, height=82, bg="white", highlightthickness=0)
        self._band_canvas.pack(fill="x")
        self._band_canvas.bind("<Configure>", lambda _: self._draw_bands())

        # ── Total invoiced label (above canvas) ───────────────────────────────
        self._total_lbl = tk.Label(parent, text="Total Invoiced:  —",
                                    font=("Segoe UI", 10, "bold"), anchor="e")
        self._total_lbl.pack(side="bottom", fill="x", padx=16, pady=(0, 2))

        # ── Per-job tree (fills remaining vertical space) ─────────────────────
        jobs_lf = tk.LabelFrame(parent, text=" Invoiced Per Job ", padx=6, pady=4)
        jobs_lf.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        cols = ("Job", "Amount Invoiced", "Share")
        self._jobs_tree = ttk.Treeview(jobs_lf, columns=cols, show="headings")
        self._jobs_tree.heading("Job",             text="Job")
        self._jobs_tree.heading("Amount Invoiced", text="Amount Invoiced")
        self._jobs_tree.heading("Share",           text="Share")
        self._jobs_tree.column("Job",             width=220, anchor="w", minwidth=120)
        self._jobs_tree.column("Amount Invoiced", width=140, anchor="e", minwidth=100)
        self._jobs_tree.column("Share",           width=70,  anchor="center", minwidth=50)
        jsb = ttk.Scrollbar(jobs_lf, command=self._jobs_tree.yview)
        self._jobs_tree.configure(yscrollcommand=jsb.set)
        self._jobs_tree.pack(side="left", fill="both", expand=True)
        jsb.pack(side="right", fill="y")

        note = tk.Label(jobs_lf,
                        text="Per-job amounts are tracked from invoices generated with this version onwards.",
                        fg="#888", font=("Segoe UI", 7), anchor="w")
        note.pack(side="bottom", fill="x", pady=(2, 0))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_period(self):
        val = self._year_var.get()
        if val == "All Time":
            return None, None
        year = int(val[:4])
        return date(year, 4, 6).isoformat(), date(year + 1, 4, 6).isoformat()

    def refresh(self):
        start, end = self._get_period()
        cur = self.db.get_setting("currency", "£")
        total, by_job = self.db.tax_overview(start, end)
        self._income = float(total)

        for r in self._jobs_tree.get_children():
            self._jobs_tree.delete(r)
        if by_job:
            for row in by_job:
                amt   = float(row["amount"])
                share = f"{amt/total*100:.0f}%" if total > 0 else "—"
                self._jobs_tree.insert("", "end", values=(
                    row["job_name"], f"{cur}{amt:,.2f}", share))
        else:
            self._jobs_tree.insert("", "end",
                values=("No per-job data yet — generate an invoice to populate this", "—", "—"))

        self._total_lbl.config(text=f"Total Invoiced:  {cur}{total:,.2f}")

        t = calc_uk_tax(float(total))
        remaining_allowance = max(0.0, TAX_FREE_ALLOWANCE - t["allowance_used"])
        remaining_basic     = max(0.0, (BASIC_RATE_LIMIT - TAX_FREE_ALLOWANCE) - t["basic_taxable"])
        remaining_higher    = max(0.0, (HIGHER_RATE_LIMIT - BASIC_RATE_LIMIT) - t["higher_taxable"])

        self._tax_lbls["allowance"].config(
            text=f"{cur}{t['allowance_used']:,.2f} used  "
                 f"({cur}{remaining_allowance:,.0f} remaining)")
        self._tax_lbls["basic_taxable"].config(
            text=f"{cur}{t['basic_taxable']:,.2f}  "
                 f"({cur}{remaining_basic:,.0f} of band remaining)")
        self._tax_lbls["higher_taxable"].config(
            text=f"{cur}{t['higher_taxable']:,.2f}  "
                 f"({cur}{remaining_higher:,.0f} of band remaining)")
        self._tax_lbls["basic_tax"].config(
            text=f"{cur}{t['basic_tax']:,.2f}")
        self._tax_lbls["higher_tax"].config(
            text=f"{cur}{t['higher_tax']:,.2f}")
        self._tax_lbls["total_tax"].config(
            text=f"{cur}{t['total_tax']:,.2f}")

        self._draw_bands()

    def _draw_bands(self):
        c = self._band_canvas
        c.delete("all")
        W = c.winfo_width()
        if W < 20:
            return

        BAR_Y1, BAR_Y2 = 24, 54
        SCALE = HIGHER_RATE_LIMIT

        def xp(amt):
            return int(W * min(float(amt), SCALE) / SCALE)

        boundaries = [0, TAX_FREE_ALLOWANCE, BASIC_RATE_LIMIT, HIGHER_RATE_LIMIT]

        for i in range(3):
            x1 = xp(boundaries[i])
            x2 = xp(boundaries[i + 1])
            c.create_rectangle(x1, BAR_Y1, x2, BAR_Y2,
                               fill=self._BAND_BG[i], outline="#bbb")

        # Filled portion per band
        income = self._income
        for i in range(3):
            band_start = boundaries[i]
            fill_end   = min(income, boundaries[i + 1])
            if fill_end > band_start:
                c.create_rectangle(xp(band_start), BAR_Y1, xp(fill_end), BAR_Y2,
                                   fill=self._BAND_FG[i], outline="")

        # Current income marker
        if income > 0:
            xi    = min(xp(income), W - 2)
            label = f"£{income:,.0f}" + (" +" if income >= HIGHER_RATE_LIMIT else "")
            tx    = max(28, min(xi, W - 28))
            c.create_line(xi, BAR_Y1 - 6, xi, BAR_Y2 + 4, fill="#1565C0", width=2)
            c.create_text(tx, BAR_Y1 - 8, text=label,
                          font=("Segoe UI", 8, "bold"), fill="#1565C0", anchor="s")

        # Boundary lines and threshold labels
        for amt, lbl in [(TAX_FREE_ALLOWANCE, f"£{TAX_FREE_ALLOWANCE//1000:.1f}k"),
                          (BASIC_RATE_LIMIT,   f"£{BASIC_RATE_LIMIT//1000:.1f}k"),
                          (HIGHER_RATE_LIMIT,  f"£{HIGHER_RATE_LIMIT//1000:.0f}k")]:
            x = xp(amt)
            c.create_line(x, BAR_Y1, x, BAR_Y2, fill="#888", width=1, dash=(3, 2))
            anchor = "ne" if amt == HIGHER_RATE_LIMIT else "n"
            c.create_text(x, BAR_Y2 + 4, text=lbl,
                          font=("Segoe UI", 7), fill="#555", anchor=anchor)

        # Band label inside each segment
        mid_xp = [xp((boundaries[i] + boundaries[i+1]) / 2) for i in range(3)]
        for i, lbl in enumerate(self._BAND_LBL):
            seg_w = xp(boundaries[i+1]) - xp(boundaries[i])
            if seg_w > 40:
                c.create_text(mid_xp[i], (BAR_Y1 + BAR_Y2) // 2, text=lbl,
                              font=("Segoe UI", 7, "bold"), fill="white")

        # Zero label
        c.create_text(2, BAR_Y2 + 4, text="£0", font=("Segoe UI", 7), fill="#555", anchor="nw")


# ── Main Dashboard Window ─────────────────────────────────────────────────────

class MainWindow(tk.Toplevel):
    def __init__(self, parent, db, app):
        super().__init__(parent)
        self.db  = db
        self.app = app
        self.title(APP_NAME)
        self.geometry("740x520")
        self.minsize(600, 420)
        self._tick_id = None
        self._build()
        self.refresh()
        self._tick()

    def _build(self):
        # Header bar
        hdr = tk.Frame(self, bg="#1565C0", padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text=APP_NAME, font=("Segoe UI", 13, "bold"),
                 fg="white", bg="#1565C0").pack(side="left")
        self._status_lbl = tk.Label(hdr, text="", font=("Segoe UI", 10),
                                     fg="#BBDEFB", bg="#1565C0")
        self._status_lbl.pack(side="left", padx=16)

        # Toolbar
        tb = tk.Frame(self, pady=6)
        tb.pack(fill="x", padx=10)
        self._toggle_btn = ttk.Button(tb, text="▶  Start", command=self._toggle_track)
        self._toggle_btn.pack(side="left", padx=2)
        ttk.Button(tb, text="+ Log Past Time", command=self._log_past).pack(side="left", padx=2)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)
        ttk.Button(tb, text="Manage jobs and projects", command=self._manage_jobs).pack(side="left", padx=2)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)
        ttk.Button(tb, text="Generate Invoice", command=self._generate_invoice).pack(side="left", padx=2)
        ttk.Button(tb, text="Invoice Settings", command=self._invoice_settings).pack(side="left", padx=2)

        # Tabs
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._recent_frame = tk.Frame(nb)
        self._week_frame   = tk.Frame(nb)
        self._month_frame  = tk.Frame(nb)
        self._cal_frame    = tk.Frame(nb)
        self._tax_frame    = tk.Frame(nb)

        nb.add(self._recent_frame, text=" Recent Entries ")
        nb.add(self._week_frame,   text=" This Week ")
        nb.add(self._month_frame,  text=" This Month ")
        nb.add(self._cal_frame,    text=" Calendar ")
        nb.add(self._tax_frame,    text=" Tax Overview ")

        self._build_recent_tab(self._recent_frame)
        self._build_summary_tab(self._week_frame,  "week")
        self._build_summary_tab(self._month_frame, "month")
        self._cal_tab = WeekCalendarTab(self._cal_frame, self.db)
        self._tax_tab = TaxOverviewTab(self._tax_frame, self.db)

    def _build_recent_tab(self, parent):
        cols = ("Date", "Job", "Project", "Start", "End", "Duration", "Notes")
        widths = (90, 130, 130, 62, 62, 80, 160)
        tree = ttk.Treeview(parent, columns=cols, show="headings", height=16)
        for c, w in zip(cols, widths):
            tree.heading(c, text=c)
            tree.column(c, width=w, minwidth=40, anchor="w")
        sb = ttk.Scrollbar(parent, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.bind("<Double-1>", lambda _: self._open_edit())
        tk.Label(parent, text="Double-click to edit   ·   grey = invoiced",
                 fg="#888", font=("Segoe UI", 8)).place(relx=1.0, rely=1.0,
                 anchor="se", x=-20, y=-4)
        self._recent_tree = tree

    def _build_summary_tab(self, parent, period):
        hdr_row = tk.Frame(parent, pady=4)
        hdr_row.pack(fill="x", padx=8)
        lbl = tk.Label(hdr_row, text="", font=("Segoe UI", 10, "bold"))
        lbl.pack(side="left")

        cols = ("Job", "Project", "Time")
        tree = ttk.Treeview(parent, columns=cols, show="headings", height=15)
        tree.heading("Job",     text="Job");     tree.column("Job",     width=220, anchor="w")
        tree.heading("Project", text="Project"); tree.column("Project", width=200, anchor="w")
        tree.heading("Time",    text="Time");    tree.column("Time",    width=100, anchor="e")
        sb = ttk.Scrollbar(parent, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        foot = tk.Frame(parent)
        foot.pack(fill="x")
        total_lbl = tk.Label(foot, text="", font=("Segoe UI", 10, "bold"), anchor="e")
        total_lbl.pack(side="right", padx=12, pady=4)

        if period == "week":
            self._week_tree, self._week_hdr, self._week_total = tree, lbl, total_lbl
        else:
            self._month_tree, self._month_hdr, self._month_total = tree, lbl, total_lbl

    # ── Refresh ──────────────────────────────────────────────────────────────

    def refresh(self):
        self._refresh_recent()
        self._refresh_summary("week")
        self._refresh_summary("month")
        self._cal_tab.draw()
        self._tax_tab.refresh()
        self._refresh_toggle_btn()

    def _tick(self):
        self._refresh_status()
        self._refresh_toggle_btn()
        self._tick_id = self.after(1000, self._tick)

    def _refresh_status(self):
        entry = self.db.open_entry()
        if entry:
            elapsed = (datetime.now() - datetime.fromisoformat(entry["start_time"])).total_seconds()
            proj    = f" / {entry['project_name']}" if entry["project_name"] else ""
            self._status_lbl.config(text=f"Tracking: {entry['job_name']}{proj}  [{fmt_hms(elapsed)}]")
        else:
            self._status_lbl.config(text="Not tracking")

    def _refresh_toggle_btn(self):
        if self.app.is_tracking():
            self._toggle_btn.config(text="■  Stop")
        else:
            self._toggle_btn.config(text="▶  Start")

    def _toggle_track(self):
        if self.app.is_tracking():
            self.app.do_stop()
        else:
            self.app.show_start()
        self._refresh_toggle_btn()

    def _refresh_recent(self):
        for r in self._recent_tree.get_children():
            self._recent_tree.delete(r)
        for e in self.db.recent_entries():
            start = datetime.fromisoformat(e["start_time"])
            end   = datetime.fromisoformat(e["end_time"]).strftime("%H:%M") if e["end_time"] else "…"
            dur   = fmt_hm(e["duration_sec"]) if e["end_time"] else "live"
            if not e["end_time"]:
                tag = "live"
            elif e["invoiced"]:
                tag = "invoiced"
            else:
                tag = ""
            self._recent_tree.insert("", "end", iid=str(e["id"]), tags=(tag,), values=(
                start.strftime("%Y-%m-%d"),
                e["job_name"],
                e["project_name"] or "",
                start.strftime("%H:%M"),
                end,
                dur,
                e["notes"] or "",
            ))
        self._recent_tree.tag_configure("live",     foreground="#43A047")
        self._recent_tree.tag_configure("invoiced", foreground="#90A4AE")

    def _refresh_summary(self, period):
        if period == "week":
            start, end = week_bounds()
            tree, hdr, total_lbl = self._week_tree, self._week_hdr, self._week_total
            d = date.today() - timedelta(days=date.today().weekday())
            hdr.config(text=f"Week of {d.strftime('%B %d, %Y')}")
        else:
            start, end = month_bounds()
            tree, hdr, total_lbl = self._month_tree, self._month_hdr, self._month_total
            hdr.config(text=date.today().strftime("%B %Y"))

        for r in tree.get_children():
            tree.delete(r)

        total = 0
        cur_job = None
        for row in self.db.summary(start, end):
            if row["job_name"] != cur_job:
                cur_job = row["job_name"]
            tree.insert("", "end", values=(
                row["job_name"],
                row["project_name"] or "—",
                fmt_hm(row["seconds"]),
            ))
            total += row["seconds"] or 0

        total_lbl.config(text=f"Total: {fmt_hm(total)}")

    def _open_edit(self):
        sel = self._recent_tree.selection()
        if not sel:
            return
        entry_id = int(sel[0])
        EditEntryDialog(self, self.db, entry_id, self.refresh)

    def _log_past(self):
        LogTimeDialog(self, self.db, self.refresh)

    def _manage_jobs(self):
        ManageJobsWindow(self, self.db, self.refresh)

    def _invoice_settings(self):
        InvoiceSettingsDialog(self, self.db)

    def _generate_invoice(self):
        GenerateInvoiceDialog(self, self.db)


# ── Application ───────────────────────────────────────────────────────────────

class TimeTrackrApp:
    def __init__(self):
        self.db   = Database()
        self.root = tk.Tk()
        self.root.withdraw()
        # The root only hosts the Tk interpreter; it is never meant to be seen.
        # A messagebox with parent=self.root (e.g. the quit-while-tracking prompt)
        # forces its parent window to surface on macOS, flashing the empty root as
        # a stray blank window. Making the root fully transparent and off-screen
        # keeps it invisible even when a dialog surfaces it. (-alpha is a no-op on
        # platforms that don't support it, so this stays cross-platform-safe.)
        self.root.geometry("1x1-10000-10000")
        self.root.attributes("-alpha", 0.0)

        self._entry_id  = None
        self._main_win  = None
        self.tray       = None

        # Recover open entry from last session
        entry = self.db.open_entry()
        if entry:
            self._entry_id = entry["id"]

        self._setup_tray()

    # ── Tray ──────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        menu = [
            plat.MenuItem("Open Dashboard", self.show_dashboard, default=True),
            plat.SEPARATOR,
            plat.MenuItem("Start Tracking", self.show_start,
                          visible_when=lambda: self._entry_id is None),
            plat.MenuItem("Stop Tracking", self.do_stop,
                          visible_when=lambda: self._entry_id is not None),
            plat.SEPARATOR,
            plat.MenuItem("Quit", self._quit),
        ]
        self.tray = plat.make_tray(
            APP_NAME, menu, lambda: make_tray_icon(self._entry_id is not None))

    def _update_tray(self):
        self.tray.update_icon()

    def is_tracking(self):
        return self._entry_id is not None

    # ── Actions ───────────────────────────────────────────────────────────────

    def show_dashboard(self):
        if self._main_win and self._main_win.winfo_exists():
            self._main_win.deiconify()
        else:
            self._main_win = MainWindow(self.root, self.db, self)
            self._main_win.protocol("WM_DELETE_WINDOW", self._main_win.withdraw)
        # Activate first so the window becomes key on macOS (accessory app),
        # otherwise its buttons and close control swallow the first click.
        plat.activate_app()
        self._main_win.lift()
        self._main_win.focus_force()

    def show_start(self):
        if self._entry_id is not None:
            messagebox.showinfo(APP_NAME, "Already tracking. Stop first.", parent=self.root)
            return
        parent = self._main_win if (self._main_win and self._main_win.winfo_exists()) else self.root
        StartDialog(parent, self.db, self._on_started)

    def _on_started(self, eid):
        self._entry_id = eid
        self._update_tray()
        self._refresh_main()

    def do_stop(self):
        if not self._entry_id:
            return
        self.db.stop_entry(self._entry_id)
        self._entry_id = None
        self._update_tray()
        self._refresh_main()

    def _refresh_main(self):
        if self._main_win and self._main_win.winfo_exists():
            self._main_win.refresh()

    def _quit(self):
        if self._entry_id:
            if not messagebox.askyesno(
                "Quit", "You are currently tracking. Stop tracking and quit?",
                parent=self.root
            ):
                return
            self.db.stop_entry(self._entry_id)
        self.tray.stop()

    def run(self):
        self.tray.run(self.root)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    TimeTrackrApp().run()