"""MR list view for agent-dashboard. Data and sync live in mr_track."""
import curses
import os
import subprocess
import threading

import mr_track as mt

# (key, reverse). ISO timestamps sort as strings; blanks sink to the bottom.
SORTS = {
    "activity": (lambda r: r["Activity"], True),
    "age": (lambda r: r["Created"] or "~", False),
    "priority": (lambda r: r["_prio"][:2], False),
    "repo": (lambda r: (r["Repo"], int(r["MR"])), False),
}
NO_PRIO = (7, 0, "")
BOARD = ["Review", "Created", "Self-QA", "Staging", "Merged", "QA", "Prod"]
AUTO_SYNC = 600  # seconds
DETAIL_H = 7
CLICKUP_TEAM = os.environ.get("CLICKUP_TEAM_ID", "14252037")

C_CARD, C_DIM, C_WARN, C_HEAD, C_SEL = range(30, 35)
COL_COLOR = {"Review": curses.COLOR_MAGENTA, "Created": curses.COLOR_WHITE, "Self-QA": curses.COLOR_BLUE,
             "QA": curses.COLOR_YELLOW, "Staging": curses.COLOR_CYAN, "Merged": curses.COLOR_RED,
             "Prod": curses.COLOR_GREEN}


class MRView:
    def __init__(self, dash):
        self.d = dash
        self.rows, self.mtime = {}, 0
        self.prio, self.prio_mtime = {}, 0
        self.key = ""
        self.scroll = 0
        self.search = ""
        self.show_closed = False
        self.sort = "activity"
        self.msg = ""
        self.syncing = None

    def reload(self):
        try:
            m = os.path.getmtime(mt.MR_FILE)
        except OSError:
            m = 0
        if m != self.mtime:
            self.rows, self.mtime = mt.load(), m
        try:
            pm = os.path.getmtime(mt.TRACKER)
        except OSError:
            pm = 0
        if pm != self.prio_mtime:
            self.prio, self.prio_mtime = mt.task_priorities(), pm

    def sync(self, force=False):
        if self.syncing and self.syncing.is_alive():
            return
        if not force and mt.last_sync_age() < AUTO_SYNC:
            return

        def work():
            self.msg = "syncing…"
            try:
                rows, failed = mt.sync()
                self.msg = f"synced {len(rows)}" + (f" · skipped {', '.join(sorted(failed))}" if failed else "")
            except Exception as e:
                self.msg = f"sync failed: {str(e)[:80]}"
        # Thread only touches CSV + self.msg; curses stays on the main thread.
        self.syncing = threading.Thread(target=work, daemon=True)
        self.syncing.start()

    def visible(self):
        rows = [{**r, "_prio": self.prio.get(r["TaskID"], NO_PRIO)} for r in self.rows.values()]
        if not self.show_closed:
            rows = [r for r in rows if mt.column(r)]
        if self.search:
            q = self.search.lower()
            rows = [r for r in rows if q in " ".join((r["Key"], r["TaskID"], r["Title"], r["Branch"])).lower()]
        order = {c: i for i, c in enumerate(BOARD)}
        key, rev = SORTS[self.sort]
        rows.sort(key=key, reverse=rev)
        return sorted(rows, key=lambda r: order.get(mt.column(r), 99))  # stable: sort holds within each column

    def selected(self, rows):
        """Selected row, keeping self.key consistent with what's on screen."""
        hit = next((r for r in rows if r["Key"] == self.key), None) or (rows[0] if rows else None)
        self.key = hit["Key"] if hit else ""
        return hit

    def _colors(self):
        curses.init_pair(C_CARD, curses.COLOR_WHITE, -1)
        curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_HEAD, curses.COLOR_CYAN, -1)
        curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)
        for i, c in enumerate(BOARD):
            curses.init_pair(40 + i, COL_COLOR[c], -1)

    def draw(self):
        self.reload()
        self.sync()
        self._colors()
        d = self.d
        h, w = d.stdscr.getmaxyx()
        d.stdscr.erase()
        rows = self.visible()
        sel = self.selected(rows)
        title = f"MR List  sort:{self.sort}"
        age = mt.last_sync_age()
        ago = "never" if age == float("inf") else f"{int(age // 60)}m ago"
        extra = f"  [/{self.search}]" if self.search else ""
        n_open = sum(map(mt.untasked, rows))
        todo = f"  [{n_open} need a task — t]" if n_open else ""
        d._bar(0, f" {title}  {len(rows)} MRs{todo}  synced {ago}  {self.msg}{extra}", w)
        body_h = h - 2 - (DETAIL_H if h > 24 else 0)
        self._draw_list(rows, 1, body_h, w)
        if h > 24:
            self._draw_detail(sel, 1 + body_h, w)
        d._bar(h - 1, " tab:agents  jk:nav  1-6:stage  >/<:advance/undo  s:sync  o:open  c:clickup  "
                      "O:sort  t:task  e:note  n:add  a:agent  x:reset  enter:agent  H:closed  /:search  Q:quit", w)
        d.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_list(self, rows, y0, body_h, w):
        self.d._write(y0, 0, f" {'MR':<15}{'TASK':<11}{'STATUS':<34}{'MERGE':<10}TITLE"[:w],
                      curses.color_pair(C_HEAD) | curses.A_BOLD)
        avail = body_h - 1
        idx = next((i for i, r in enumerate(rows) if r["Key"] == self.key), 0)
        if idx >= self.scroll + avail:
            self.scroll = idx - avail + 1
        self.scroll = min(self.scroll, idx)
        for n, r in enumerate(rows[self.scroll:self.scroll + avail]):
            line = " " + mt.fmt_row(r)
            attr = curses.color_pair(C_SEL) | curses.A_BOLD if r["Key"] == self.key else \
                curses.color_pair(C_DIM) | curses.A_DIM if r["State"] != "opened" else \
                curses.color_pair(40 + BOARD.index(mt.column(r))) if mt.column(r) else 0
            self.d._write(y0 + 1 + n, 0, line[:w].ljust(w), attr)

    def _draw_detail(self, r, y, w):
        self.d._write(y, 0, ("─── " + (r["Key"] if r else "") + " " + "─" * w)[:w], curses.color_pair(C_HEAD))
        if not r:
            return
        cells = "  ".join(f"{mt.LABELS[s]}:{r[s] or '·'}" for s in mt.order(r))
        info = [
            r["Title"],
            f"{r['Branch']} → {r['Target']}   {r['Role']}   {r['State']}   merge:{r['MergeStatus'] or '·'}",
            cells + ("   (* = by hand, - = cleared)" if any("*" in r[s] or r[s] == "-" for s in mt.STAGES) else ""),
            r["URL"],
            f"note: {r['Notes']}" if r["Notes"] else "",
            "⇄ " + "   ".join(f"{mt.short_repo(x['Repo'])}!{x['MR']} {mt.badge(x)}" for x in mt.group(self.rows.values(), r)[1:])
            + (f"   agent: {r['Agent']}" if r["Agent"] else "") if len(mt.group(self.rows.values(), r)) > 1 or r["Agent"] else "",
        ]
        for i, text in enumerate(info):
            self.d._write(y + 1 + i, 1, text[:w - 2], curses.color_pair(C_CARD))

    def handle(self, key):
        """False when the key is not an MR-view key."""
        rows = self.visible()
        r = self.selected(rows)
        i = next((n for n, x in enumerate(rows) if x["Key"] == self.key), 0)

        def move(n):
            if rows:
                self.key = rows[max(0, min(len(rows) - 1, i + n))]["Key"]

        if key in (curses.KEY_DOWN, ord("j")):
            move(1)
        elif key in (curses.KEY_UP, ord("k")):
            move(-1)
        elif ord("1") <= key <= ord("6") and r:
            mt.set_stage([r["Key"]], mt.STAGES[key - ord("1")])
        elif key == ord(">") and r:
            nxt = next((s for s in mt.order(r)[mt.current(r) + 1:] if s != "Merged"), None)
            if nxt:
                mt.set_stage([r["Key"]], nxt, "set")
        elif key == ord("<") and r and mt.current(r) > 0:
            mt.set_stage([r["Key"]], mt.at(r), "clear")
        elif key in (ord("s"), ord("S")):
            self.sync(force=True)
        elif key == ord("o") and r:
            subprocess.run(["open", r["URL"]], stderr=subprocess.DEVNULL)
        elif key == ord("c") and r and r["TaskID"]:
            subprocess.run(["open", f"https://app.clickup.com/t/{CLICKUP_TEAM}/{r['TaskID']}"],
                           stderr=subprocess.DEVNULL)
        elif key == ord("t") and r:
            v = self.d._prompt(f" task for {r['Key']} — TECH id, or a title to file in notes: ")
            v = (v or "").strip()
            if mt.task_of(v) or v.isdigit():
                mt.set_field([r["Key"]], "TaskID", mt.task_of(v) or f"TECH-{v}")
            elif v:
                kind = mt.request_task(r["Key"], v)
                self.msg = f"filed {kind} in tracker — linked on next sprint-sync"
        elif key == ord("e") and r:
            v = self.d._prompt(f" note for {r['Key']}: ")
            if v is not None:
                mt.set_field([r["Key"]], "Notes", v.strip())
        elif key == ord("n"):
            v = self.d._prompt(" add MR url: ")
            if v:
                try:
                    self.key = mt.add(v.strip())
                except Exception as e:
                    self.msg = f"add failed: {str(e)[:60]}"
        elif key == ord("O"):
            modes = list(SORTS)
            self.sort = modes[(modes.index(self.sort) + 1) % len(modes)]
            self.key = ""
        elif key == ord("H"):
            self.show_closed = not self.show_closed
        elif key == ord("/"):
            v = self.d._prompt(" /search: ")
            if v is not None:
                self.search = v.strip()
        elif key == 27:
            self.search = ""
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")) and r:
            self.d.search = ""
            rows_a = self.d._enrich()
            hit = next((n for n, a in enumerate(rows_a) if r in mt.for_agent([r], a["Session"], a.get("TaskID", ""))), None)
            if hit is None:
                self.msg = f"no agent for {r['TaskID'] or r['Key']}"
            elif rows_a[hit]["Session"] in self.d.live:
                subprocess.run(["tmux", "switch-client", "-t", rows_a[hit]["Session"]], stderr=subprocess.DEVNULL)
            else:
                self.d.view, self.d.selected = "agents", hit
                self.d._flash(f"{rows_a[hit]['Session']} isn't running — s restarts it")
        elif key == ord("a") and r:
            v = self.d._prompt(f" agent session for {r['Key']} (+ related MRs) [{r['Agent']}]: ")
            if v is not None:
                mt.set_field([r["Key"]], "Agent", v.strip())
        elif key == ord("x") and r:
            for s in mt.STAGES:
                if s != "Created" and (r[s] == "-" or r[s].endswith("*")):
                    mt.set_stage([r["Key"]], s, "auto")
            self.sync(force=True)
            self.msg = f"{r['Key']}: hand-set stages reset, syncing"
        else:
            return False
        self.mtime = 0  # writes above must show up on the next frame
        return True
