"""MR list + kanban views for agent-dashboard. Data and sync live in mr_track."""
import curses
import os
import subprocess
import threading

import mr_track as mt

BOARD = ["Review", "Created", "Self-QA", "QA", "Staging", "Prod"]
AUTO_SYNC = 600  # seconds
CARD_H = 4
DETAIL_H = 7
CLICKUP_TEAM = os.environ.get("CLICKUP_TEAM_ID", "14252037")

C_CARD, C_DIM, C_WARN, C_HEAD, C_SEL = range(30, 35)
COL_COLOR = {"Review": curses.COLOR_MAGENTA, "Created": curses.COLOR_WHITE, "Self-QA": curses.COLOR_BLUE,
             "QA": curses.COLOR_YELLOW, "Staging": curses.COLOR_CYAN, "Prod": curses.COLOR_GREEN}


class MRView:
    def __init__(self, dash):
        self.d = dash
        self.mode = "board"  # 'list' | 'board'
        self.rows, self.mtime = {}, 0
        self.key = ""
        self.col = 1
        self.scroll = 0
        self.search = ""
        self.show_closed = False
        self.msg = ""
        self.syncing = None

    def reload(self):
        try:
            m = os.path.getmtime(mt.MR_FILE)
        except OSError:
            m = 0
        if m != self.mtime:
            self.rows, self.mtime = mt.load(), m

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
        rows = list(self.rows.values())
        if not self.show_closed:
            rows = [r for r in rows if r["State"] == "opened"
                    or (r["State"] == "merged" and r["Role"] == "author" and not mt.reached(r["Prod"]))]
        if self.search:
            q = self.search.lower()
            rows = [r for r in rows if q in " ".join((r["Key"], r["TaskID"], r["Title"], r["Branch"])).lower()]
        order = {c: i for i, c in enumerate(BOARD)}
        return sorted(rows, key=lambda r: (order.get(mt.column(r), 99), -mt.current(r), r["Repo"], int(r["MR"])))

    def columns(self, rows):
        cols = {c: [] for c in BOARD}
        for r in rows:
            if (c := mt.column(r)) in cols:
                cols[c].append(r)
        return cols

    def selected(self, rows):
        """Selected row, keeping self.key/self.col consistent with what's on screen."""
        pool = self.columns(rows)[BOARD[self.col]] if self.mode == "board" else rows
        hit = next((r for r in pool if r["Key"] == self.key), None) or (pool[0] if pool else None)
        self.key = hit["Key"] if hit else ""
        return hit, pool

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
        sel, _ = self.selected(rows)
        title = "MR Board" if self.mode == "board" else "MR List"
        age = mt.last_sync_age()
        ago = "never" if age == float("inf") else f"{int(age // 60)}m ago"
        extra = f"  [/{self.search}]" if self.search else ""
        d._bar(0, f" {title}  {len(rows)} MRs  synced {ago}  {self.msg}{extra}", w)
        body_h = h - 2 - (DETAIL_H if h > 24 else 0)
        if self.mode == "board":
            self._draw_board(rows, 1, body_h, w)
        else:
            self._draw_list(rows, 1, body_h, w)
        if h > 24:
            self._draw_detail(sel, 1 + body_h, w)
        d._bar(h - 1, " tab:view  hjkl:nav  1-6:stage  >/<:advance/undo  s:sync  o:open  c:clickup  "
                      "t:task  e:note  n:add  enter:agent  H:closed  /:search  Q:quit", w)
        d.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_board(self, rows, y0, body_h, w):
        cols = self.columns(rows)
        narrow = w // len(BOARD) < 20
        shown = [self.col] if narrow else range(len(BOARD))
        cw = w if narrow else w // len(BOARD)
        for slot, ci in enumerate(shown):
            name, x = BOARD[ci], slot * cw
            cards = cols[name]
            head = f" {'◀ ' if narrow else ''}{name} ({len(cards)}){' ▶' if narrow else ''}"
            hattr = curses.color_pair(40 + ci) | curses.A_BOLD | (curses.A_UNDERLINE if ci == self.col else 0)
            self.d._write(y0, x, head[:cw - 1].ljust(cw - 1), hattr)
            per = max(1, (body_h - 1) // CARD_H)
            idx = next((i for i, r in enumerate(cards) if r["Key"] == self.key), 0) if ci == self.col else 0
            top = max(0, idx - per + 1)
            for n, r in enumerate(cards[top:top + per]):
                self._card(r, y0 + 1 + n * CARD_H, x, cw - 1, ci == self.col and r["Key"] == self.key)
            if len(cards) > top + per:
                self.d._write(y0 + body_h - 1, x, f" +{len(cards) - top - per} more"[:cw - 1],
                              curses.color_pair(C_DIM) | curses.A_DIM)

    def _card(self, r, y, x, cw, sel):
        base = curses.color_pair(C_SEL) if sel else curses.color_pair(C_CARD)
        ms = mt.MERGE_BADGE.get(r["MergeStatus"], "")
        who = f"@{r['Role']}" if r["Role"] != "author" else ""
        flags = " ".join(filter(None, [ms, r["StageNote"] if mt.column(r) == "Staging" else "",
                                       *(f"⚠{g}" for g in mt.gaps(r)), who]))
        lines = [f"{mt.short_repo(r['Repo'])}!{r['MR']} {r['TaskID']}", r["Title"], flags]
        for i, text in enumerate(lines):
            attr = base | (curses.A_BOLD if i == 0 else 0)
            if not sel and i == 2 and ("✗" in flags or "⚠" in flags or "stale" in flags):
                attr = curses.color_pair(C_WARN)
            self.d._write(y + i, x, (" " + text)[:cw].ljust(cw), attr)

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
        cells = "  ".join(f"{mt.LABELS[s]}:{r[s] or '·'}" for s in mt.STAGES)
        info = [
            r["Title"],
            f"{r['Branch']} → {r['Target']}   {r['Role']}   {r['State']}   merge:{r['MergeStatus'] or '·'}",
            cells + ("   (* = by hand, - = cleared)" if any("*" in r[s] or r[s] == "-" for s in mt.STAGES) else ""),
            r["URL"],
            f"note: {r['Notes']}" if r["Notes"] else "",
        ]
        for i, text in enumerate(info):
            self.d._write(y + 1 + i, 1, text[:w - 2], curses.color_pair(C_CARD))

    def handle(self, key):
        """False when the key is not an MR-view key."""
        rows = self.visible()
        r, pool = self.selected(rows)
        i = next((n for n, x in enumerate(pool) if x["Key"] == self.key), 0)

        def move(n):
            if pool:
                self.key = pool[max(0, min(len(pool) - 1, i + n))]["Key"]

        def shift_col(n):
            self.col = max(0, min(len(BOARD) - 1, self.col + n))
            self.key = ""

        if key in (curses.KEY_DOWN, ord("j")):
            move(1)
        elif key in (curses.KEY_UP, ord("k")):
            move(-1)
        elif key in (curses.KEY_LEFT, ord("h")) and self.mode == "board":
            shift_col(-1)
        elif key in (curses.KEY_RIGHT, ord("l")) and self.mode == "board":
            shift_col(1)
        elif ord("1") <= key <= ord("6") and r:
            mt.set_stage([r["Key"]], mt.STAGES[key - ord("1")])
        elif key == ord(">") and r:
            nxt = next((s for s in mt.STAGES[mt.current(r) + 1:] if s != "Merged"), None)
            if nxt:
                mt.set_stage([r["Key"]], nxt, "set")
                self._follow(r["Key"])
        elif key == ord("<") and r and mt.current(r) > 0:
            mt.set_stage([r["Key"]], mt.STAGES[mt.current(r)], "clear")
            self._follow(r["Key"])
        elif key in (ord("s"), ord("S")):
            self.sync(force=True)
        elif key == ord("o") and r:
            subprocess.run(["open", r["URL"]], stderr=subprocess.DEVNULL)
        elif key == ord("c") and r and r["TaskID"]:
            subprocess.run(["open", f"https://app.clickup.com/t/{CLICKUP_TEAM}/{r['TaskID']}"],
                           stderr=subprocess.DEVNULL)
        elif key == ord("t") and r:
            v = self.d._prompt(f" task for {r['Key']} [{r['TaskID']}]: ")
            if v is not None:
                mt.set_field([r["Key"]], "TaskID", mt.task_of(v) if v.strip() else "")
        elif key == ord("e") and r:
            v = self.d._prompt(f" note for {r['Key']}: ")
            if v is not None:
                mt.set_field([r["Key"]], "Notes", v.strip())
        elif key == ord("n"):
            v = self.d._prompt(" add MR url: ")
            if v:
                try:
                    self.key = mt.add(v.strip())
                    self._follow(self.key)
                except Exception as e:
                    self.msg = f"add failed: {str(e)[:60]}"
        elif key == ord("H"):
            self.show_closed = not self.show_closed
        elif key == ord("/"):
            v = self.d._prompt(" /search: ")
            if v is not None:
                self.search = v.strip()
        elif key == 27:
            self.search = ""
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")) and r:
            self.d.search = r["TaskID"] or r["Branch"]
            self.d.view = "agents"
            self.d.selected = 0
        else:
            return False
        self.mtime = 0  # writes above must show up on the next frame
        return True

    def _follow(self, key):
        """Board selection tracks a card that changed column."""
        self.mtime = 0
        self.reload()
        r = self.rows.get(key)
        if r and mt.column(r) in BOARD:
            self.col = BOARD.index(mt.column(r))
            self.key = key
