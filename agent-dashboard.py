#!/usr/bin/env python3
"""
agent-dashboard — terminal dashboard for reviewing AI agents
"""
import curses
import csv
import subprocess
import re
import os
import time
import tempfile
from datetime import datetime

import mr_track
from mr_views import MRView

AGENT_DIR  = os.path.expanduser(os.environ.get("AGENT_DIR",  "~/.agents"))
AGENT_FILE = os.path.expanduser(os.environ.get("AGENT_FILE", os.path.join(AGENT_DIR, "agents.csv")))
AGENT_LOG  = os.path.expanduser(os.environ.get("AGENT_LOG",  os.path.join(AGENT_DIR, "agent-log.md")))
EXCLUDED   = re.compile(os.environ.get("AGENT_EXCLUDED", r'^(control-center|settings|notes)$'))
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[mGKHFABCDsuJh]|\x1b\(B|\x1b=|\r')
REFRESH_INTERVAL = 30  # seconds

# Layout
HEADER_LINES = 2
FOOTER_LINES = 1
DIVIDER_LINE = 1

# Column widths (minimum / fixed)
W_STATUS  = 10
W_CLICKUP = 11
W_MR      = 8

# Color pair indices
C_ACTIVE   = 1
C_BLOCKED  = 2
C_STALE    = 3
C_DONE     = 4
C_HEADER   = 5
C_SELECTED = 6
C_BAR      = 7
C_PREVIEW  = 8
C_REVIEW   = 9
C_HOLD     = 10
C_FOCUS    = 11
C_QA       = 12
C_DEPLOY   = 13

STATUS_ORDER  = {'active': 0, 'review': 1, 'qa': 2, 'deploy': 3, 'hold': 4, 'blocked': 5, 'stale': 6, 'done': 7}
STATUS_MARKER = {'active': '*', 'review': 'R', 'qa': 'Q', 'deploy': 'D', 'hold': '=', 'blocked': '!', 'stale': '?', 'done': 'd'}
STATUS_ALIAS  = {'testing': 'review'}  # legacy rows still in agents.csv
COLOR_MAP = None  # initialized after curses.start_color()


# ── CSV helpers ──────────────────────────────────────────────────────────────

# Each entry: [(session, old_row)] — row-level, not whole-file, so undo
# doesn't clobber rows agent-update wrote in the meantime.
UNDO = []


def _rewrite_csv(transform, record=True):
    """Read agents.csv, apply transform(rows) -> rows, write back."""
    with open(AGENT_FILE) as f:
        rows = list(csv.reader(f))
    before = {r[3]: list(r) for r in rows[1:] if len(r) > 3}
    rows = transform(rows)
    after = {r[3]: r for r in rows[1:] if len(r) > 3}
    # Ignore blank padding toggle_flag adds when it creates a new column.
    trim = lambda r: ','.join(r or []).rstrip(',')
    changed = [(s, r) for s, r in before.items() if s not in after or trim(after[s]) != trim(r)]
    if record and changed:
        UNDO.append(changed)
    with open(AGENT_FILE, 'w', newline='') as f:
        csv.writer(f).writerows(rows)


def undo_last():
    """Restore rows from the last change. Returns affected sessions, or []."""
    if not UNDO:
        return []
    changed = UNDO.pop()
    def transform(rows):
        width = len(rows[0]) if rows else 0
        for session, old in changed:
            old = old + [''] * (width - len(old))
            for i, r in enumerate(rows):
                if len(r) > 3 and r[3] == session:
                    rows[i] = old
                    break
            else:
                rows.append(old)
        return rows
    _rewrite_csv(transform, record=False)
    return [s for s, _ in changed]


def load_agents():
    try:
        with open(AGENT_FILE) as f:
            rows = [dict(r) for r in csv.DictReader(f)]
    except FileNotFoundError:
        return []
    for r in rows:
        r['Status'] = STATUS_ALIAS.get(r.get('Status', ''), r.get('Status', ''))
    return rows


def update_agent_status(session, new_status):
    def transform(rows):
        for row in rows:
            if len(row) > 3 and row[3] == session:
                row[0] = new_status
        return rows
    _rewrite_csv(transform)


def toggle_flag(session, col='Focus'):
    """Toggle a boolean column ('Focus' / 'Hackathon') for a session."""
    def transform(rows):
        if not rows:
            return rows
        header = rows[0]
        try:
            fi = header.index(col)
        except ValueError:
            header.append(col)
            fi = len(header) - 1
            for row in rows[1:]:
                while len(row) <= fi:
                    row.append('')
        for row in rows[1:]:
            while len(row) <= fi:
                row.append('')
            if len(row) > 3 and row[3] == session:
                row[fi] = '' if row[fi] == '1' else '1'
        return rows
    _rewrite_csv(transform)


def remove_agent(session):
    _rewrite_csv(lambda rows: [r for r in rows if not (len(r) > 3 and r[3] == session)])


def append_log(session, message):
    today = datetime.now().strftime('%Y-%m-%d')
    with open(AGENT_LOG, 'a') as f:
        f.write(f"- [{today}] {session}: {message}\n")


# ── tmux helpers ─────────────────────────────────────────────────────────────

def get_live_sessions():
    try:
        out = subprocess.check_output(["tmux", "ls"], stderr=subprocess.DEVNULL, text=True)
        return {line.split(":")[0] for line in out.splitlines()
                if not EXCLUDED.match(line.split(":")[0])}
    except Exception:
        return set()


# Claude Code retitles its process to its version (e.g. 2.1.286).
TOOL_PROC = re.compile(r'^(claude|opencode|cursor|aider|codex|\d+\.\d+\.\d+)$')


def get_tool_sessions():
    """Sessions with an AI CLI still running in some pane."""
    try:
        out = subprocess.check_output(["tmux", "list-panes", "-a", "-F", "#S\t#{pane_current_command}"],
                                      stderr=subprocess.DEVNULL, text=True)
        return {sess for sess, _, cmd in (l.partition("\t") for l in out.splitlines()) if TOOL_PROC.match(cmd)}
    except Exception:
        return set()


def get_pane_path(session):
    """Get the current working directory of a tmux session's active pane."""
    try:
        return subprocess.check_output(
            ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return ""


def remove_worktree(path, multi=False):
    """Remove a git worktree directory, falling back to rm -rf."""
    if multi and path and os.path.isdir(path) and not os.path.exists(os.path.join(path, ".git")):
        # A multi-repo agent's dir only holds worktrees, so it goes once they do.
        for d in os.listdir(path):
            remove_worktree(os.path.join(path, d))
        try:
            os.rmdir(path)
        except OSError:
            pass
        return
    if not path or not os.path.isfile(os.path.join(path, ".git")):
        return
    try:
        main_repo = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--git-common-dir"],
            stderr=subprocess.DEVNULL, text=True
        ).strip().removesuffix("/.git")
    except Exception:
        main_repo = ""

    if main_repo:
        result = subprocess.run(
            ["git", "-C", main_repo, "worktree", "remove", path, "--force"],
            stderr=subprocess.DEVNULL
        )
        if result.returncode != 0:
            import shutil
            shutil.rmtree(path, ignore_errors=True)
            subprocess.run(["git", "-C", main_repo, "worktree", "prune"],
                           stderr=subprocess.DEVNULL)
    else:
        import shutil
        shutil.rmtree(path, ignore_errors=True)


def get_pane_output(session, lines=50):
    try:
        out = subprocess.check_output(
            ["tmux", "capture-pane", "-t", session, "-p", "-e"],
            stderr=subprocess.DEVNULL, text=True
        )
        out = ANSI_ESCAPE.sub('', out)
        content = out.splitlines()
        return content[-lines:] if content else []
    except Exception:
        return []


def age_str(started):
    if not started:
        return ''
    try:
        delta = datetime.now() - datetime.strptime(started.strip(), '%Y-%m-%d')
        return f"{delta.days}d" if delta.days > 0 else f"{delta.seconds // 3600}h"
    except Exception:
        return ''


# ── Dashboard ────────────────────────────────────────────────────────────────

class Dashboard:
    def __init__(self, stdscr):
        self.stdscr      = stdscr
        self.agents      = []
        self.live        = set()
        self.selected    = 0
        self.scroll      = 0
        self.pscroll     = 0
        self.last_refresh = 0
        self.hide_done   = False
        self.search      = ''
        self._pending_g  = False
        self._show_help  = False
        # sort_mode: 'status' | 'alpha' | 'age'
        self.sort_mode   = 'status'
        # pane_cache: {session: (lines, timestamp)}
        self.pane_cache  = {}
        self.view        = 'agents'  # 'agents' | 'mrs'
        self.mrv         = MRView(self)
        self.mr_dots     = {}
        self.mrs         = []
        self._init_curses()

    def _init_curses(self):
        global COLOR_MAP
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_ACTIVE,   curses.COLOR_GREEN,   -1)
        curses.init_pair(C_BLOCKED,  curses.COLOR_RED,     -1)
        curses.init_pair(C_STALE,    curses.COLOR_YELLOW,  -1)
        curses.init_pair(C_DONE,     curses.COLOR_WHITE,   -1)
        curses.init_pair(C_HEADER,   curses.COLOR_CYAN,    -1)
        curses.init_pair(C_SELECTED, curses.COLOR_BLACK,   curses.COLOR_CYAN)
        curses.init_pair(C_BAR,      curses.COLOR_BLACK,   curses.COLOR_WHITE)
        curses.init_pair(C_PREVIEW,  curses.COLOR_WHITE,   -1)
        curses.init_pair(C_REVIEW,   curses.COLOR_MAGENTA, -1)
        curses.init_pair(C_HOLD,     curses.COLOR_BLUE,    -1)
        curses.init_pair(C_FOCUS,    curses.COLOR_CYAN,    -1)
        curses.init_pair(C_QA,       curses.COLOR_YELLOW,  -1)
        curses.init_pair(C_DEPLOY,   curses.COLOR_CYAN,    -1)
        COLOR_MAP = {
            'active':  curses.color_pair(C_ACTIVE),
            'review':  curses.color_pair(C_REVIEW)  | curses.A_BOLD,
            'qa':      curses.color_pair(C_QA)      | curses.A_BOLD,
            'deploy':  curses.color_pair(C_DEPLOY)  | curses.A_BOLD,
            'hold':    curses.color_pair(C_HOLD)    | curses.A_BOLD,
            'blocked': curses.color_pair(C_BLOCKED) | curses.A_BOLD,
            'stale':   curses.color_pair(C_STALE),
            'done':    curses.color_pair(C_DONE)    | curses.A_DIM,
        }
        self.stdscr.nodelay(True)
        self.stdscr.timeout(100)

    def refresh_data(self):
        self.agents = load_agents()
        self.live   = get_live_sessions()
        self.tooled = get_tool_sessions()
        self.mrs    = list(mr_track.load().values())
        # A multi-repo agent is only as far along as its furthest-behind MR.
        self.mr_dots = {}
        for a in self.agents:
            mine = mr_track.for_agent(self.mrs, a.get('Session', ''), a.get('TaskID', ''))
            if mine:
                slow = min(mine, key=mr_track.current)
                self.mr_dots[a.get('Session')] = mr_track.badge(slow).split()[0] + (f"{len(mine)}" if len(mine) > 1 else '')
        self.last_refresh = time.time()

    def _enrich(self):
        enriched = []
        for a in self.agents:
            status  = a.get('Status', '')
            session = a.get('Session', '')
            ds = 'stale' if status == 'active' and session not in self.tooled else status
            focus = a.get('Focus', '') == '1'
            hack  = a.get('Hackathon', '') == '1'
            typ = a.get('Type', '')
            sec = 3 if focus else (2 if hack else (1 if typ == 'analytics' else (0.5 if typ == 'auto' else 0)))
            enriched.append({**a, '_ds': ds, '_focus': focus, '_hack': hack, '_sec': sec})
        # Sections: 0 main, 0.5 auto (agent-auto), 1 analytics, 2 hackathon, 3 focus (bottom, out of the way).
        # Chosen sort mode applies within each section.
        if self.sort_mode == 'alpha':
            enriched.sort(key=lambda a: (a['_sec'], a.get('Session', '').lower()))
        elif self.sort_mode == 'age':
            enriched.sort(key=lambda a: a.get('Started', '') or '', reverse=True)
            enriched.sort(key=lambda a: a['_sec'])  # stable — keeps age order within section
        else:  # status (default)
            enriched.sort(key=lambda a: (a['_sec'], STATUS_ORDER.get(a['_ds'], 9), a.get('Session', '').lower()))
        if self.hide_done:
            # Keep focused items visible even when done — user pinned them
            # for a reason and should see them through.
            enriched = [a for a in enriched
                        if a['_ds'] not in ('done', 'hold') or a['_focus'] or a['_hack']]
        if self.search:
            q = self.search.lower()
            enriched = [a for a in enriched if q in a.get('Session', '').lower()
                        or q in a.get('Task', '').lower()]
        return enriched

    def _selected_agent(self, enriched):
        """Return the currently selected agent dict, or None."""
        if enriched and 0 <= self.selected < len(enriched):
            return enriched[self.selected]
        return None

    def _pin_selection(self, enriched):
        """Remember the session name of the currently selected agent."""
        agent = self._selected_agent(enriched)
        return agent.get('Session', '') if agent else ''

    def _restore_selection(self, enriched, session):
        """Move self.selected to the row matching session, if found."""
        if not session:
            return
        for i, a in enumerate(enriched):
            if a.get('Session') == session:
                self.selected = i
                return

    def _refresh_pinned(self, enriched):
        """Refresh data while keeping the cursor on the same session."""
        pinned = self._pin_selection(enriched)
        self.refresh_data()
        new_enriched = self._enrich()
        self._restore_selection(new_enriched, pinned)
        return new_enriched

    def _get_pane(self, session, lines=50):
        now = time.time()
        cached = self.pane_cache.get(session)
        if cached and now - cached[1] < 3:
            return cached[0]
        output = get_pane_output(session, max(lines, 50))
        self.pane_cache[session] = (output, now)
        return output

    def _invalidate_pane(self, session):
        self.pane_cache.pop(session, None)
        self.pscroll = 0

    # ── drawing ──────────────────────────────────────────────────────────────

    def draw(self, enriched):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()

        # clamp selection
        self.selected = max(0, min(self.selected, len(enriched) - 1)) if enriched else 0

        available    = h - HEADER_LINES - FOOTER_LINES - DIVIDER_LINE
        # Labeled separator above each section change
        sec_labels = {0.5: ' auto ', 1: ' analytics ', 2: ' hackathon ', 3: ' focus '}
        sep_labels = {ei: sec_labels.get(enriched[ei]['_sec'], ' ')
                      for ei in range(1, len(enriched))
                      if enriched[ei]['_sec'] != enriched[ei - 1]['_sec']}
        sep_rows     = len(sep_labels)
        list_rows    = max(1, min(len(enriched), available - 3 - sep_rows))
        preview_rows = max(0, available - list_rows - sep_rows)

        # Proportional columns based on terminal width
        flex = w - W_STATUS - W_CLICKUP - W_MR - 6  # remaining space after fixed cols
        W_SESSION = max(16, int(flex * 0.30))
        W_TASK    = max(10, int(flex * 0.50))
        W_TYPE    = max(8, flex - W_SESSION - W_TASK)

        # title bar
        ago    = int(time.time() - self.last_refresh)
        hide   = "  [H: show done+hold]" if self.hide_done else "  [H: hide done+hold]"
        search = f"  [/{self.search}]" if self.search else ""
        sort   = f"  sort:{self.sort_mode}"
        n_rev  = sum(a['_ds'] in ('review', 'blocked') for a in self._enrich())
        sort  += f"  [{n_rev} need you — w: walkthrough]" if n_rev else ''
        self._bar(0, f" Agent Dashboard  {datetime.now():%Y-%m-%d %H:%M}  {ago}s ago{hide}{sort}{search}", w)

        # column header
        col_hdr = (f" {'ST':<{W_STATUS}} {'SESSION':<{W_SESSION}} "
                   f"{'TASK':<{W_TASK}} {'CLICKUP':<{W_CLICKUP}} {'MR':<{W_MR}} {'TYPE/AGE':<{W_TYPE}}")
        self._write(1, 0, col_hdr[:w], curses.color_pair(C_HEADER) | curses.A_BOLD)

        # list rows
        if self.selected >= self.scroll + list_rows:
            self.scroll = self.selected - list_rows + 1
        elif self.selected < self.scroll:
            self.scroll = self.selected

        extra = 0  # extra rows consumed by separators
        for i in range(list_rows):
            idx = i + self.scroll
            if idx >= len(enriched):
                break
            row_y = HEADER_LINES + i + extra
            if row_y >= h - FOOTER_LINES - 1:
                break

            label = sep_labels.get(idx)
            if label is not None:
                self._write(row_y, 0, ("─" * 3 + label + "─" * w)[:w],
                            curses.color_pair(C_HEADER) | curses.A_DIM)
                extra += 1
                row_y += 1
                if row_y >= h - FOOTER_LINES - 1:
                    break

            a      = enriched[idx]
            ds     = a['_ds']
            mark   = '▸' if a.get('_focus') else ('⚑' if a.get('_hack') else ' ')
            status = f"{mark}{STATUS_MARKER.get(ds, ' ')} {ds}"
            type_age = (a.get('Type','') or age_str(a.get('Started','')))[:W_TYPE]
            line   = (f" {status:<{W_STATUS}} {a.get('Session','')[:W_SESSION]:<{W_SESSION}} "
                      f"{a.get('Task','')[:W_TASK]:<{W_TASK}} "
                      f"{a.get('ClickUp','')[:W_CLICKUP]:<{W_CLICKUP}} "
                      f"{self.mr_dots.get(a.get('Session'), ''):<{W_MR}} "
                      f"{type_age:<{W_TYPE}}")
            attr = (curses.color_pair(C_SELECTED) | curses.A_BOLD) if idx == self.selected \
                   else COLOR_MAP.get(ds, 0)
            self._write(row_y, 0, line[:w].ljust(min(w, len(line) + 1)), attr)

        # divider
        div_y = HEADER_LINES + list_rows + extra
        if div_y < h - FOOTER_LINES:
            sel = self._selected_agent(enriched)
            label   = f" Preview: {sel['Session']} " if sel else " Preview "
            divider = ("─" * 3 + label + "─" * max(0, w - 3 - len(label)))[:w]
            self._write(div_y, 0, divider, curses.color_pair(C_HEADER))

        # preview
        if enriched and preview_rows > 0:
            sel     = self._selected_agent(enriched)
            session = sel.get('Session', '') if sel else ''
            py0     = div_y + 1

            if session in self.live:
                lines = self._get_pane(session, preview_rows)
                max_ps = max(0, len(lines) - preview_rows)
                self.pscroll = max(0, min(self.pscroll, max_ps))
                start = max(0, len(lines) - preview_rows - self.pscroll)
                for i, pline in enumerate(lines[start: start + preview_rows]):
                    if py0 + i >= h - FOOTER_LINES:
                        break
                    self._write(py0 + i, 0, pline[:w - 1], curses.color_pair(C_PREVIEW))
                if not lines:
                    self._write(py0, 0, " (no output captured)", curses.color_pair(C_STALE))
            else:
                self._write(py0, 0, " (session not running)", curses.color_pair(C_BLOCKED))

        # footer
        if self._show_help:
            footer = (" j/k:nav  enter:jump  w:walkthrough  s:restart  c:cmd  K:^C  n:new  f:focus  "
                       "a:active  r/t:review  q:qa  m:deploy  h:hold  b:block  d:done  x:kill  u:undo  p:hack  "
                       "/:search  o:sort  H:hide  PgUp/Dn:scroll  v:refresh  tab:MRs  l:open MR  L:task MRs  Q:quit")
        else:
            footer = " j/k:nav  enter:jump  w:walkthrough  s:restart  c:cmd  u:undo  /:search  tab:MRs  Q:quit  ?:help"
        self._bar(h - 1, footer, w)

        self.stdscr.noutrefresh()
        curses.doupdate()

    def _bar(self, y, text, w):
        try:
            self.stdscr.attron(curses.color_pair(C_BAR) | curses.A_BOLD)
            self.stdscr.addstr(y, 0, text[:w].ljust(w))
            self.stdscr.attroff(curses.color_pair(C_BAR) | curses.A_BOLD)
        except curses.error:
            pass

    def _flash(self, msg):
        """Show a brief message on the footer bar."""
        h, w = self.stdscr.getmaxyx()
        self._bar(h - 1, f' {msg}', w)
        self.stdscr.noutrefresh()
        curses.doupdate()
        curses.napms(1200)

    def _status(self, msg):
        """Paint a status message immediately (no blocking sleep) — for
        progress updates during slow operations."""
        h, w = self.stdscr.getmaxyx()
        self._bar(h - 1, f' {msg}', w)
        self.stdscr.noutrefresh()
        curses.doupdate()

    def _write(self, y, x, text, attr=0):
        try:
            self.stdscr.attron(attr)
            self.stdscr.addstr(y, x, text)
            self.stdscr.attroff(attr)
        except curses.error:
            pass

    # ── event loop ───────────────────────────────────────────────────────────

    def run(self):
        self.refresh_data()

        while True:
            if time.time() - self.last_refresh > REFRESH_INTERVAL:
                self._refresh_pinned(self._enrich())

            if self.view != 'agents':
                self.mrv.draw()
                try:
                    key = self.stdscr.getch()
                except (curses.error, KeyboardInterrupt):
                    break
                if key == ord('Q'):
                    break
                if key == ord('\t'):
                    self.view = 'agents'
                elif key == curses.KEY_RESIZE:
                    self.stdscr.clear()
                elif key != -1:
                    self.mrv.handle(key)
                continue

            enriched = self._enrich()
            self.draw(enriched)

            try:
                key = self.stdscr.getch()
            except (curses.error, KeyboardInterrupt):
                break

            if key == -1:
                continue

            agent = self._selected_agent(enriched)

            # reset pending-g on any key that isn't 'g'
            if key != ord('g'):
                self._pending_g = False

            if key == ord('Q'):
                break

            elif key == ord('q'):
                if agent:
                    update_agent_status(agent['Session'], 'qa')
                    append_log(agent['Session'], 'handed to QA from dashboard')
                    self._refresh_pinned(enriched)

            elif key in (ord('u'), ord('U')):
                sessions = undo_last()
                if sessions:
                    for sess in sessions:
                        append_log(sess, 'undo from dashboard')
                    self._refresh_pinned(enriched)
                    self._restore_selection(self._enrich(), sessions[0])
                    self._flash(f"Undid change to {', '.join(sessions)}")
                else:
                    self._flash('Nothing to undo')

            elif key == ord('K'):
                if agent:
                    subprocess.run(["tmux", "send-keys", "-t", agent['Session'], "C-c"],
                                   stderr=subprocess.DEVNULL)
                    self._invalidate_pane(agent['Session'])

            elif key in (curses.KEY_UP, ord('k')):
                self.selected = max(0, self.selected - 1)
                self.pscroll = 0

            elif key in (curses.KEY_DOWN, ord('j')):
                self.selected = min(len(enriched) - 1, self.selected + 1) if enriched else 0
                self.pscroll = 0

            elif key == ord('G'):
                self.selected = max(0, len(enriched) - 1)
                self.pscroll = 0

            elif key == ord('g'):
                if self._pending_g:
                    self.selected = 0
                    self.scroll = 0
                    self.pscroll = 0
                    self._pending_g = False
                else:
                    self._pending_g = True

            elif key == ord('/'):
                query = self._prompt(f" /search [{self.search}]: " if self.search else " /search: ")
                if query is None:
                    pass  # ESC — leave search unchanged
                else:
                    self.search = query.strip()
                self.selected = 0
                self.scroll = 0

            elif key == ord('?'):
                self._show_help = not self._show_help

            elif key == ord('\t'):
                self.view = 'mrs'

            elif key == ord('l'):
                urls = [r['URL'] for r in mr_track.for_agent(self.mrs, agent['Session'], agent.get('TaskID', ''))] if agent else []
                for u in urls:
                    subprocess.run(['open', u], stderr=subprocess.DEVNULL)
                if not urls:
                    self._flash('No MR tracked for this agent')

            elif key == ord('L'):
                if agent and (agent.get('TaskID') or agent.get('ClickUp')):
                    self.mrv.search = agent.get('TaskID') or agent.get('ClickUp')
                    self.view = 'mrs'

            elif key == 27:  # ESC clears search
                self.search = ''
                self.selected = 0
                self.scroll = 0

            elif key in (ord('v'), ord('V')):
                self._refresh_pinned(enriched)

            elif key == ord('H'):
                self.hide_done = not self.hide_done
                self.selected = 0
                self.scroll = 0

            elif key in (ord('o'), ord('O')):
                modes = ['status', 'alpha', 'age']
                self.sort_mode = modes[(modes.index(self.sort_mode) + 1) % len(modes)]
                self.selected = 0
                self.scroll = 0

            elif key == curses.KEY_RESIZE:
                self.stdscr.clear()

            elif key == curses.KEY_PPAGE:
                self.pscroll += 5

            elif key == curses.KEY_NPAGE:
                self.pscroll = max(0, self.pscroll - 5)

            elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
                if agent and agent['Session'] in self.live:
                    subprocess.run(["tmux", "switch-client", "-t", agent['Session']],
                                   stderr=subprocess.DEVNULL)

            elif key in (ord('d'), ord('D')):
                if agent:
                    update_agent_status(agent['Session'], 'done')
                    append_log(agent['Session'], 'marked done from dashboard')
                    self._refresh_pinned(enriched)

            elif key in (ord('t'), ord('T')):
                # 't' kept as an alias for review — testing and review merged
                if agent:
                    update_agent_status(agent['Session'], 'review')
                    append_log(agent['Session'], 'marked needs review from dashboard')
                    self._refresh_pinned(enriched)

            elif key in (ord('m'), ord('M')):
                if agent:
                    update_agent_status(agent['Session'], 'deploy')
                    append_log(agent['Session'], 'marked ready for deploy/merge from dashboard')
                    self._refresh_pinned(enriched)

            elif key == ord('h'):
                if agent:
                    update_agent_status(agent['Session'], 'hold')
                    append_log(agent['Session'], 'put on hold from dashboard')
                    self._refresh_pinned(enriched)

            elif key in (ord('b'), ord('B')):
                if agent:
                    update_agent_status(agent['Session'], 'blocked')
                    append_log(agent['Session'], 'marked blocked from dashboard')
                    self._refresh_pinned(enriched)

            elif key in (ord('x'), ord('X')):
                if agent:
                    session = agent['Session']
                    choice = self._prompt(f" '{session}': [k]ill  [u]ntrack  [c]ancel: ")
                    choice = (choice or '').strip().lower()
                    if choice in ('k', 'kill'):
                        append_log(session, 'session killed from dashboard')
                        self._status(f"Killing '{session}'…  reading worktree path")
                        pane_path = get_pane_path(session)
                        self._status(f"Killing '{session}'…  tmux kill-session")
                        subprocess.run(["tmux", "kill-session", "-t", session],
                                       stderr=subprocess.DEVNULL)
                        self._status(f"Killing '{session}'…  removing worktree")
                        remove_worktree(pane_path, agent.get('Type') == 'multi')
                        self._status(f"Killing '{session}'…  updating tracker")
                        remove_agent(session)
                        self._flash(f"Killed '{session}'")
                        self._refresh_pinned(enriched)
                    elif choice in ('u', 'untrack'):
                        append_log(session, 'untracked from dashboard')
                        remove_agent(session)
                        self._refresh_pinned(enriched)

            elif key in (ord('f'), ord('F')):
                if agent:
                    is_focused = agent.get('_focus')
                    if not is_focused:
                        focus_count = sum(1 for a in enriched if a.get('_focus'))
                        if focus_count >= 5:
                            self._flash('Focus limit reached (max 5)')
                            continue
                    toggle_flag(agent['Session'], 'Focus')
                    new_state = 'unfocused' if is_focused else 'focused'
                    append_log(agent['Session'], f'{new_state} from dashboard')
                    self._refresh_pinned(enriched)

            elif key in (ord('p'), ord('P')):
                if agent:
                    toggle_flag(agent['Session'], 'Hackathon')
                    verb = 'removed from' if agent.get('_hack') else 'pulled into'
                    append_log(agent['Session'], f'{verb} hackathon from dashboard')
                    self._refresh_pinned(enriched)

            elif key in (ord('a'), ord('A')):
                if agent:
                    update_agent_status(agent['Session'], 'active')
                    append_log(agent['Session'], 'marked active from dashboard')
                    self._refresh_pinned(enriched)

            elif key in (ord('n'), ord('N')):
                script = os.environ.get("AGENT_TOOLS_SCRIPT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-tools.sh"))
                # Don't pipe output through tee — process substitution breaks
                # nvim's TTY detection (and editing breaks). If you need to
                # debug, run the runner manually outside the popup.
                runner_body = (
                    f"#!/usr/bin/env zsh\n"
                    f"source {script}\n"
                    f"agent-start\n"
                    f"echo\n"
                    f"echo 'Press enter to close'\n"
                    f"read\n"
                )
                with tempfile.NamedTemporaryFile('w', suffix='.zsh', delete=False) as tf:
                    tf.write(runner_body)
                    runner = tf.name
                os.chmod(runner, 0o755)
                # Tear down curses so its raw-mode terminal settings don't
                # eat the popup's keystrokes (breaks editing in the nvim
                # yaml template). Rebuild after the popup closes.
                curses.endwin()
                try:
                    if os.environ.get("TMUX"):
                        os.system(f"tmux display-popup -E -w 90% -h 85% {runner}")
                    else:
                        os.system(f"zsh {runner}")
                finally:
                    try: os.unlink(runner)
                    except OSError: pass
                    os.system("stty sane")
                    self.stdscr = curses.initscr()
                    self._init_curses()
                self._refresh_pinned(enriched)

            elif key == ord('s'):
                if agent and not agent['Session']:
                    self._flash("row has no session — start one from the MR view (Enter)")
                elif agent:
                    session = agent['Session']
                    script = os.environ.get("AGENT_TOOLS_SCRIPT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-tools.sh"))
                    # Session gone → rebuild it; session alive but CLI exited → relaunch in place.
                    cmd = 'agent-restart "$1" --bg' if session not in self.live else 'agent-resume "$1"'
                    self._status(f"restarting {session}…")
                    res = subprocess.run(["zsh", "-c", f"source {script} >/dev/null 2>&1; {cmd}", "_", session],
                                         capture_output=True, text=True)
                    lines = (res.stdout + res.stderr).strip().splitlines()
                    self._flash(lines[-1] if lines else f"restarted {session}")
                    self._invalidate_pane(session)
                    self.refresh_data()

            elif key in (ord('w'), ord('W')):
                if agent:
                    script = os.environ.get("AGENT_TOOLS_SCRIPT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-tools.sh"))
                    subprocess.run(["zsh", "-c", f"source {script}; agent-review \"$1\"", "_", agent['Session']],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            elif key in (ord('r'), ord('R')):
                if agent:
                    update_agent_status(agent['Session'], 'review')
                    append_log(agent['Session'], 'marked needs review from dashboard')
                    self._refresh_pinned(enriched)

            elif ord('0') <= key <= ord('9'):
                if agent:
                    session = agent['Session']
                    subprocess.run(["tmux", "send-keys", "-t", session, chr(key)],
                                   stderr=subprocess.DEVNULL)
                    self._invalidate_pane(session)
                    time.sleep(0.05)  # let tmux process the keystroke
                    self._get_pane(session)  # force re-capture into cache
                    if agent.get('Status') != 'active':
                        update_agent_status(session, 'active')
                        append_log(session, 'reset to active after receiving input from dashboard')
                    self.last_refresh = 0  # mark stale, refresh on next cycle

            elif key in (ord('c'), ord('C')):
                if agent:
                    session = agent['Session']
                    msg = self._popup_edit(f"Message for {session}")
                    if msg:
                        subprocess.run(["tmux", "send-keys", "-t", session, msg, "Enter"],
                                       stderr=subprocess.DEVNULL)
                        self._invalidate_pane(session)
                        if agent.get('Status') != 'active':
                            update_agent_status(session, 'active')
                            append_log(session, 'reset to active after receiving input from dashboard')
                        self.last_refresh = 0  # mark stale, refresh on next cycle

    def _prompt(self, label):
        """Inline input at the footer bar. Returns text on Enter, None on ESC/cancel."""
        h, w = self.stdscr.getmaxyx()
        buf  = []
        curses.flushinp()  # discard any stale keypresses before opening prompt
        curses.curs_set(1)
        self.stdscr.nodelay(False)
        self.stdscr.timeout(-1)
        try:
            while True:
                display  = (label + ''.join(buf))[:w - 1]
                cursor_x = min(len(display), w - 1)
                self._bar(h - 1, display.ljust(w), w)
                try:
                    self.stdscr.move(h - 1, cursor_x)
                except curses.error:
                    pass
                self.stdscr.noutrefresh()
                curses.doupdate()
                try:
                    ch = self.stdscr.getch()
                except (curses.error, KeyboardInterrupt):
                    return None
                if ch in (curses.KEY_ENTER, ord('\n'), ord('\r')):
                    return ''.join(buf)
                elif ch == 27:
                    return None
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    if buf:
                        buf.pop()
                elif 32 <= ch < 127:
                    buf.append(chr(ch))
        finally:
            curses.flushinp()
            curses.curs_set(0)
            self.stdscr.nodelay(True)
            self.stdscr.timeout(100)

    def _popup_edit(self, title=""):
        """Open nvim in a tmux popup for multiline editing. Returns text or ''."""
        tmp = tempfile.NamedTemporaryFile(suffix='.md', prefix='agent-msg-', delete=False)
        tmp.close()
        try:
            curses.endwin()
            subprocess.run(
                ["tmux", "display-popup", "-E", "-w", "80%", "-h", "60%",
                 "-T", title, "nvim", tmp.name],
            )
            os.system("stty sane")
            self.stdscr = curses.initscr()
            self._init_curses()
            with open(tmp.name) as f:
                return f.read().strip()
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def main():
    import sys
    view = sys.argv[sys.argv.index('--view') + 1] if '--view' in sys.argv[:-1] else 'agents'
    os.system("stty sane")

    def start(s):
        d = Dashboard(s)
        d.view = view
        d.run()
    curses.wrapper(start)


if __name__ == '__main__':
    main()
