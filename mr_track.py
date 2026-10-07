#!/usr/bin/env python3
"""
mr — track merge requests through Created → Self-QA → QA → Staging → Prod → Merged.

  mr                         open the dashboard on the MR board
  mr sync                    pull from GitLab + tardis/CI
  mr add [url|branch]        track an MR (default: current branch)
  mr set <ref> <stage> [--clear|--auto]
                             ref = TECH-1234 | repo!iid | iid; stage = created selfqa qa staging prod merged
  mr task <ref> TECH-1234    attach an MR to a task
  mr ls [--all] [--json]     list
  mr badge TECH-1234         one-line status for other tools
  mr annotate [file]         sync, then write 🔀 lines into the sprint tracker
  mr import-notes [file]     track MR links already typed into the tracker
  mr selftest

Stage cells: '' unset · 'YYYY-MM-DD' synced · 'YYYY-MM-DD*' set by hand · '-' cleared by hand.
Sync only fills empty cells, so hand-set values always win.
"""
import csv, difflib, fcntl, json, os, re, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from glob import glob
from urllib.parse import quote

AGENT_DIR = os.path.expanduser(os.environ.get("AGENT_DIR", "~/.agents"))
MR_FILE   = os.path.expanduser(os.environ.get("MR_FILE", os.path.join(AGENT_DIR, "mrs.csv")))
AGENT_LOG = os.path.expanduser(os.environ.get("AGENT_LOG", os.path.join(AGENT_DIR, "agent-log.md")))
REPO_GLOB = os.path.expanduser(os.environ.get("MR_REPO_GLOB", "~/vidangel-repo/*"))
TRACKER   = os.path.expanduser("~/notes/work_notes/sprints/sprint_tracker.md")
ME        = os.environ.get("GITLAB_USER", "nodlac")

TARDIS_REPOS = {"vidangel-backend", "www.vidangel.com", "pier"}
# No tardis: a successful deploy job on the MR head means a build is out for testing.
BUILD_JOBS = {"android": re.compile(r"^deploy_")}
STAGING_ENV, PROD_ENV = "staging", "production"

STAGES = ["Created", "SelfQA", "Staging", "Merged", "QA", "Prod"]
# Tardis repos deploy the branch, QA it on prod, then merge; build repos merge first and QA the build.
TARDIS_ORDER = ["Created", "SelfQA", "Staging", "QA", "Prod", "Merged"]
LABELS = {"Created": "Created", "SelfQA": "Self-QA", "QA": "QA",
          "Staging": "Staging", "Prod": "Prod", "Merged": "Merged"}
GATES = ("SelfQA", "QA")  # human checks worth flagging when skipped
FIELDS = ["Key", "TaskID", "Repo", "ProjectID", "MR", "Role", "State", "Branch", "Title",
          "HeadSHA", "MergeSHA", "Target", "MergeStatus", "URL", *STAGES, "StageNote",
          "Updated", "Activity", "Agent", "Notes"]
ROLE_RANK = {"author": 0, "assignee": 1, "reviewer": 2}
TECH_RE = re.compile(r"tech[-_ ]?(\d{3,})", re.I)
MR_URL_RE = re.compile(r"gitlab\.com/([\w.\-/]+?)/-/merge_requests/(\d+)")
SHORT = {"vidangel-backend": "backend", "www.vidangel.com": "www"}
MERGE_BADGE = {"mergeable": "✓", "ci_must_pass": "…CI", "ci_still_running": "…CI",
               "conflict": "✗conflict", "not_approved": "needs✓", "draft_status": "draft",
               "discussions_not_resolved": "💬", "need_rebase": "rebase", "blocked_status": "blocked"}


def reached(cell):
    return bool(cell) and cell != "-"


def synced_set(row, stage, day):
    """Fill a stage from a source. Never touches hand-set or hand-cleared cells."""
    if not row.get(stage):
        row[stage] = day
        return True
    return False


def toggle(row, stage):
    row[stage] = "-" if reached(row.get(stage)) else f"{date.today()}*"


def order(row):
    return TARDIS_ORDER if row.get("Repo") in TARDIS_REPOS else STAGES


def current(row):
    """Index into order(row) of the furthest stage reached, -1 if none."""
    return max((i for i, s in enumerate(order(row)) if reached(row.get(s))), default=-1)


def at(row):
    """Furthest stage reached, None if none."""
    cur = current(row)
    return order(row)[cur] if cur >= 0 else None


def gaps(row):
    if row.get("State") != "opened":
        return []
    cur = current(row)
    return [LABELS[s] for s in GATES if order(row).index(s) < cur and not reached(row.get(s))]


def column(row):
    """Board column, or None when the MR is off the board."""
    st = row.get("State")
    if st == "closed" or st == "merged" and (row.get("Role") != "author" or reached(row.get(order(row)[-1]))):
        return None
    if row.get("Role") != "author":
        return "Review"
    return LABELS[at(row) or "Created"]


def badge(row):
    dots = "".join("●" if reached(row.get(s)) else "○" for s in order(row))
    label = LABELS[at(row)] if at(row) else "—"
    if row.get("State") == "closed":
        label = "closed"
    note = f" {row['StageNote']}" if row.get("StageNote") and at(row) == "Staging" else ""
    warn = "".join(f" ⚠{g}" for g in gaps(row))
    return f"{dots} {label}{note}{warn}"


def ago(stamp):
    """GitLab UTC stamp '2026-10-05T14:02' → '3h' / '4d'."""
    try:
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)).total_seconds()
    except ValueError:
        return ""
    return f"{int(secs // 86400)}d" if secs >= 86400 else f"{int(secs // 3600)}h"


def short_repo(repo):
    return SHORT.get(repo, repo)


def ident(r):
    return " ".join(filter(None, [f"{short_repo(r['Repo'])}!{r['MR']}", r["TaskID"]]))


def task_of(*texts):
    for t in texts:
        if m := TECH_RE.search(t or ""):
            return f"TECH-{m.group(1)}"
    return ""


@contextmanager
def locked():
    os.makedirs(AGENT_DIR, exist_ok=True)
    with open(MR_FILE + ".lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield


def load():
    try:
        with open(MR_FILE) as f:
            return {r["Key"]: {k: r.get(k) or "" for k in FIELDS} for r in csv.DictReader(f)}
    except FileNotFoundError:
        return {}


def save(rows):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(MR_FILE), prefix=".mrs-")
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["Repo"], int(r["MR"] or 0))))
    os.chmod(tmp, 0o644)
    os.replace(tmp, MR_FILE)


def related(a, b):
    """Same parent work: one agent, one task, or one branch pushed to several repos."""
    return a["Key"] != b["Key"] and a["Role"] == b["Role"] == "author" and (
        (a["Agent"] and a["Agent"] == b["Agent"]) or (a["TaskID"] and a["TaskID"] == b["TaskID"])
        or (a["Branch"] == b["Branch"] and a["Repo"] != b["Repo"]))


def propagate(rows):
    """Tagging one MR with a task or agent tags its whole multi-repo set. Only fills blanks."""
    # ponytail: O(n²) per pass, fine at a few hundred MRs
    changed = True
    while changed:
        changed = False
        for a in rows.values():
            for b in rows.values():
                for f in ("TaskID", "Agent"):
                    if a[f] and not b[f] and related(a, b):
                        b[f], changed = a[f], True


def group(rows, r):
    return [r] + [x for x in rows if related(r, x)]


def for_agent(rows, session, task=""):
    return [r for r in rows if r["Role"] == "author" and r["State"] != "closed"
            and (r["Agent"] == session or (task and r["TaskID"] == task))]


def mutate(fn):
    """Locked read-modify-write; fn(rows) edits in place."""
    with locked():
        rows = load()
        fn(rows)
        propagate(rows)
        save(rows)
        return rows


def log(msg):
    with open(AGENT_LOG, "a") as f:
        f.write(f"- [{date.today()}] mr: {msg}\n")


def resolve(rows, ref):
    """TECH-1234 → all its MRs; repo!iid / www!945 / 945 → that MR."""
    ref = ref.strip()
    if ref.upper().startswith("TECH"):
        return [r for r in rows.values() if r["TaskID"] == task_of(ref)]
    repo, _, iid = ref.rpartition("!")
    return [r for r in rows.values() if r["MR"] == iid
            and (not repo or repo in (r["Repo"], short_repo(r["Repo"])))]


def gl(path):
    out = subprocess.run(["glab", "api", path], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(f"glab api {path}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def git(repo_dir, *args):
    return subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=120)


def repo_dirs():
    """Project name → local clone (not a worktree)."""
    found = {}
    for d in sorted(glob(REPO_GLOB)):
        if os.path.isdir(os.path.join(d, ".git")):
            url = git(d, "remote", "get-url", "origin").stdout.strip()
            found.setdefault(url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1], d)
    return found


def from_api(m, role):
    """Source-owned fields for one MR payload."""
    repo = m["references"]["full"].split("!")[0].rsplit("/", 1)[-1]
    return {
        "Key": f"{repo}!{m['iid']}", "Repo": repo, "ProjectID": str(m["project_id"]),
        "MR": str(m["iid"]), "Role": role, "State": m["state"], "Branch": m["source_branch"],
        "Title": m["title"], "HeadSHA": m["sha"] or "", "Target": m["target_branch"],
        "MergeSHA": m.get("merge_commit_sha") or m.get("squash_commit_sha") or "",
        "MergeStatus": m.get("detailed_merge_status") or "", "URL": m["web_url"],
        "_created": m["created_at"][:10], "_merged": (m.get("merged_at") or "")[:10],
        "_task": task_of(m["source_branch"], m["title"]),
    }


def parse_state(msg):
    """tardis state commit message → {branch: short_sha}. State branches carry no merge
    commits, so this NEW STATE list is the only record of what is deployed."""
    out, on = {}, False
    for line in msg.splitlines():
        if line.startswith("NEW STATE:"):
            on = True
        elif on and (m := re.match(r"\s*-\s+([0-9a-f]{7,})\s+(\S+)\s*$", line)):
            out[m.group(2)] = m.group(1)
    return out


def tardis_updates(repo_dir, rows, target):
    """Stage fills for one tardis repo. Raises if git can't answer, so callers skip the repo."""
    envs = [STAGING_ENV, PROD_ENV]
    refs = [f"tardis/{e}/state" for e in envs] + [target]
    f = git(repo_dir, "fetch", "-q", "origin", *refs)
    if f.returncode:
        raise RuntimeError(f"fetch {repo_dir}: {f.stderr.strip()[:200]}")
    states = {}
    for e in envs:
        r = git(repo_dir, "log", "-1", "--format=%cs%n%B", f"origin/tardis/{e}/state")
        if r.returncode:
            raise RuntimeError(f"read tardis/{e}: {r.stderr.strip()[:200]}")
        day, _, msg = r.stdout.partition("\n")
        states[e] = (day, parse_state(msg))
    ups = {}
    for r in rows:
        u = ups.setdefault(r["Key"], {})
        for e, stage in ((STAGING_ENV, "Staging"), (PROD_ENV, "Prod")):
            day, state = states[e]
            sha = state.get(r["Branch"])
            if sha and r["State"] == "opened":
                u[stage] = day
                if stage == "Staging":
                    u["StageNote"] = "" if r["HeadSHA"].startswith(sha) else "stale"
            elif r["State"] == "merged" and r["MergeSHA"] and state.get(target) and git(
                    repo_dir, "merge-base", "--is-ancestor", r["MergeSHA"], state[target]).returncode == 0:
                u[stage] = day
        if r["State"] == "opened" and not states[STAGING_ENV][1].get(r["Branch"]):
            u["StageNote"] = ""
    return ups


def build_updates(rows, job_re):
    ups = {}
    for r in rows:
        if r["State"] != "opened" or not r["HeadSHA"]:
            continue
        pipes = gl(f"projects/{r['ProjectID']}/pipelines?sha={r['HeadSHA']}&per_page=20")
        ok = any(job_re.match(j["name"])
                 for p in pipes
                 for j in gl(f"projects/{r['ProjectID']}/pipelines/{p['id']}/jobs?scope[]=success&per_page=100"))
        if ok:
            ups[r["Key"]] = {"Staging": str(date.today()), "StageNote": "build"}
        elif reached(r["Staging"]):
            ups[r["Key"]] = {"StageNote": "stale"}
    return ups


def sync(only=None):
    """Pull every source, then apply under lock. Returns (rows, failed_repos).
    Raises if GitLab itself is unreachable — callers must not write notes then."""
    rows = load()
    fetched = {}
    for role, q in (("reviewer", f"scope=all&reviewer_username={ME}"),
                    ("assignee", "scope=assigned_to_me"),
                    ("author", "scope=created_by_me")):  # last wins → author outranks
        for m in gl(f"merge_requests?{q}&state=opened&per_page=100"):
            fetched[f"{m['references']['full'].split('!')[0].rsplit('/', 1)[-1]}!{m['iid']}"] = from_api(m, role)

    # Tracked MRs that left the open list: merged or closed since the last sync.
    gone = [r for k, r in rows.items() if k not in fetched and r["State"] == "opened"]
    for r in gone:
        fetched[r["Key"]] = from_api(gl(f"projects/{r['ProjectID']}/merge_requests/{r['MR']}"), r["Role"])

    # Deploy checks run against the post-fetch view; merged MRs keep checking until Prod.
    view = {k: {**r, **fetched.get(k, {})} for k, r in rows.items()}
    view.update({k: {**{f: "" for f in FIELDS}, **v} for k, v in fetched.items() if k not in view})
    todo = [r for r in view.values() if r["Role"] == "author" and r["State"] != "closed"
            and not reached(r.get("Prod")) and (not only or r["Key"] in only)]
    dirs = repo_dirs()
    failed, ups = set(), {}

    def run(repo):
        mine = [r for r in todo if r["Repo"] == repo]
        if repo in TARDIS_REPOS:
            return tardis_updates(dirs[repo], mine, mine[0]["Target"] or "main")
        return build_updates(mine, BUILD_JOBS[repo])

    repos = {r["Repo"] for r in todo if (r["Repo"] in TARDIS_REPOS and r["Repo"] in dirs) or r["Repo"] in BUILD_JOBS}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {repo: ex.submit(run, repo) for repo in repos}
    for repo, fut in futs.items():
        try:
            ups.update(fut.result())
        except Exception as e:
            failed.add(repo)
            print(f"mr sync: {repo} skipped — {e}", file=sys.stderr)

    transitions = []

    def apply(rows):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for k, f in fetched.items():
            r = rows.setdefault(k, {**{x: "" for x in FIELDS}, "Key": k})
            before = current(r)
            if ROLE_RANK.get(r["Role"], 9) < ROLE_RANK[f["Role"]]:
                f = {**f, "Role": r["Role"]}  # hand-added as author stays author
            r.update({x: v for x, v in f.items() if not x.startswith("_")})
            if not r["TaskID"]:
                r["TaskID"] = f["_task"]
            if r["Role"] == "author":
                synced_set(r, "Created", f["_created"])
            if f["_merged"]:
                synced_set(r, "Merged", f["_merged"])
            r["Updated"] = now
            r["_before"] = before
        for k, u in ups.items():
            if k not in rows:
                continue
            r = rows[k]
            r.setdefault("_before", current(r))
            for x, v in u.items():
                if x in STAGES:
                    synced_set(r, x, v)
                else:
                    r[x] = v
        for r in rows.values():
            b = r.pop("_before", None)
            if b is not None and current(r) > max(b, 0):  # first sighting isn't news
                transitions.append(f"{ident(r)} → {LABELS[at(r)]}")

    rows = mutate(apply)
    for t in transitions:
        log(t)
    with open(MR_FILE + ".synced", "w") as f:
        f.write(datetime.now().isoformat())
    return rows, failed


def last_sync_age():
    try:
        return (datetime.now() - datetime.fromtimestamp(os.path.getmtime(MR_FILE + ".synced"))).total_seconds()
    except OSError:
        return float("inf")


def add(ref=None, task="", agent="", cwd=None):
    """Track an MR by URL, or the current branch's MR."""
    if ref and (m := MR_URL_RE.search(ref)):
        data = gl(f"projects/{quote(m.group(1), safe='')}/merge_requests/{m.group(2)}")
    else:
        args = ["glab", "mr", "view", "-F", "json"] + ([ref] if ref else [])
        out = subprocess.run(args, capture_output=True, text=True, timeout=60, cwd=cwd)
        if out.returncode:
            raise RuntimeError(out.stderr.strip() or "no MR for this branch")
        data = json.loads(out.stdout)
    role = "author" if data["author"]["username"] == ME else "reviewer"
    f = from_api(data, role)

    def apply(rows):
        r = rows.setdefault(f["Key"], {**{x: "" for x in FIELDS}, "Key": f["Key"]})
        r.update({x: v for x, v in f.items() if not x.startswith("_")})
        r["TaskID"] = task or r["TaskID"] or f["_task"]
        r["Agent"] = agent or r["Agent"]
        if role == "author":
            synced_set(r, "Created", f["_created"])
        if f["_merged"]:
            synced_set(r, "Merged", f["_merged"])
    mutate(apply)
    return f["Key"]


def add_here(task="", agent=""):
    """MRs for the current repo's branch, or for every repo directly under a
    multi-repo working dir. Repos without an MR are skipped."""
    here = os.getcwd()
    dirs = [here] if os.path.exists(os.path.join(here, ".git")) else \
        sorted(d for d in glob(os.path.join(here, "*")) if os.path.exists(os.path.join(d, ".git")))
    keys = []
    for d in dirs:
        try:
            keys.append(add(None, task, agent, cwd=d))
        except Exception:
            pass
    return keys


def set_stage(keys, stage, mode="toggle"):
    def apply(rows):
        for k in keys:
            r = rows[k]
            if mode == "auto":
                r[stage] = ""
            elif mode == "clear":
                r[stage] = "-"
            elif mode == "set":
                r[stage] = f"{date.today()}*"
            else:
                toggle(r, stage)
            if reached(r[stage]):
                log(f"{ident(r)} → {LABELS[stage]} (by hand)")
    return mutate(apply)


def set_field(keys, field, value):
    def apply(rows):
        for k in keys:
            rows[k][field] = value
    return mutate(apply)


TASK_LINE = re.compile(r"^(\s*)- \[([^\]]*)\]\s+\[?TECH-(\d+)\b")
# Same ranking sprint-sync uses for # Current Sprint, so the board matches the tracker.
MARKER_RANK = {"!": 0, ">": 1, "/": 2, " ": 3, "~": 4, "h": 5}


def task_priorities(path=TRACKER):
    """TECH id → (rank, line no, marker). Closed tasks rank after open ones; first mention wins."""
    out = {}
    try:
        lines = open(path).readlines()
    except OSError:
        return out
    for n, line in enumerate(lines):
        if (m := TASK_LINE.match(line)) and f"TECH-{m.group(3)}" not in out:
            out[f"TECH-{m.group(3)}"] = (MARKER_RANK.get(m.group(2), 6), n, m.group(2))
    return out
MR_LINE = "🔀 "


def tracker_line(r):
    return f"- {MR_LINE}{short_repo(r['Repo'])}!{r['MR']} {badge(r)}"


def annotate_lines(lines, by_task):
    """Replace each task's 🔀 child lines with fresh ones. Tasks absent from by_task are left alone."""
    out, i, done = [], 0, set()
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        m = TASK_LINE.match(line)
        if not m or f"TECH-{m.group(3)}" not in by_task or f"TECH-{m.group(3)}" in done:
            continue
        tid = f"TECH-{m.group(3)}"
        done.add(tid)
        indent = len(m.group(1))
        child = " " * (indent + 4)
        while i < len(lines) and lines[i].strip().startswith("- " + MR_LINE) \
                and len(lines[i]) - len(lines[i].lstrip()) > indent:
            i += 1
        out.extend(f"{child}{tracker_line(r)}\n" for r in by_task[tid])
    return out


def untasked(r):
    """Your own open MR with no task — every MR should trace back to one."""
    return r["Role"] == "author" and r["State"] == "opened" and not r["TaskID"]


def link_from_notes(lines):
    """MR links written under a TECH line claim untasked MRs. This is how an MR
    filed via request_task() picks up its ID once sprint-sync creates the task."""
    links, tid, depth = {}, "", 0
    for line in lines:
        indent = len(line) - len(line.lstrip())
        if m := TASK_LINE.match(line):
            tid, depth = f"TECH-{m.group(3)}", indent
            continue
        if line.strip() and indent <= depth:
            tid = ""
        for proj, iid in MR_URL_RE.findall(line):
            if tid:
                links[f"{proj.rsplit('/', 1)[-1]}!{iid}"] = tid

    def apply(rows):
        for k, t in links.items():
            if k in rows and not rows[k]["TaskID"]:
                rows[k]["TaskID"] = t
    mutate(apply)


def request_task(key, title, path=TRACKER):
    """File a NEW_ line under # Current Sprint with the MR link beneath it;
    sprint-sync creates the ClickUp task and link_from_notes() attaches it."""
    r = load()[key]
    kind = {"bug": "NEW_B", "fix": "NEW_B", "chore": "NEW_C"}.get(re.split(r"[/_-]", r["Branch"])[0], "NEW_F")
    with open(path) as f:
        lines = f.readlines()
    at = next(i for i, l in enumerate(lines) if l.strip() == "# Current Sprint") + 1
    lines[at:at] = [f"    - [/] {kind} {title}\n", f"        - MR {r['URL']}\n"]
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mr-task-")
    with os.fdopen(fd, "w") as f:
        f.writelines(lines)
    os.replace(tmp, path)
    return kind


def annotate(path=TRACKER):
    """Sync first; only MRs whose sources answered get their lines rewritten."""
    with open(path) as f:
        link_from_notes(f.readlines())
    rows, failed = sync()
    by_task = {}
    for r in sorted(rows.values(), key=lambda r: (r["Repo"], int(r["MR"]))):
        if r["TaskID"] and r["Role"] == "author" and r["State"] != "closed":
            by_task.setdefault(r["TaskID"], []).append(r)
    by_task = {t: rs for t, rs in by_task.items() if not any(r["Repo"] in failed for r in rs)}
    with open(path) as f:
        lines = f.readlines()
    new = annotate_lines(lines, by_task)
    if new != lines:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mr-annotate-")
        with os.fdopen(fd, "w") as f:
            f.writelines(new)
        os.replace(tmp, path)
    return sum(l[:2] in ("+ ", "- ") for l in difflib.ndiff(lines, new))


def import_notes(path=TRACKER):
    """Track MR links already written under tasks in the tracker."""
    added, tid = [], ""
    for line in open(path):
        if m := TASK_LINE.match(line):
            tid = f"TECH-{m.group(3)}"
        for proj, iid in MR_URL_RE.findall(line):
            try:
                added.append(add(f"https://gitlab.com/{proj}/-/merge_requests/{iid}", tid))
            except Exception as e:
                print(f"  skip {proj}!{iid}: {e}", file=sys.stderr)
    return added


def fmt_row(r):
    who = "" if r["Role"] == "author" else f" [{r['Role']}]"
    ms = MERGE_BADGE.get(r["MergeStatus"], "")
    task = r["TaskID"] or ("⚠ no task" if untasked(r) else "")
    return (f"{short_repo(r['Repo'])+'!'+r['MR']:<14} {task:<10} {badge(r):<34} "
            f"{ms:<9} {r['Title'][:60]}{who}")


def stage_arg(s):
    key = {x.lower(): x for x in STAGES}.get(s.lower().replace("-", ""))
    if not key:
        sys.exit(f"stage must be one of: {' '.join(x.lower() for x in STAGES)}")
    return key


def selftest():
    msg = "Deploy: 1 added\n\nADD:\n    - c3e20d2c  feat-x\n                body\n\nNEW STATE:\n    - 303527c4  main\n    - 2a6d9ce7  bugfix/TECH-5167_x\n"
    assert parse_state(msg) == {"main": "303527c4", "bugfix/TECH-5167_x": "2a6d9ce7"}
    assert task_of("feature/TECH-5395_upcoming", "") == "TECH-5395"
    assert task_of("chore/tech_5311-x") == "TECH-5311"
    r = {s: "" for s in STAGES} | {"Role": "author", "State": "opened", "StageNote": ""}
    assert not gaps({**r, "Prod": "x", "State": "merged"})
    assert synced_set(r, "Created", "2026-10-01") and r["Created"] == "2026-10-01"
    r["QA"] = "-"
    assert not synced_set(r, "QA", "2026-10-02") and r["QA"] == "-"
    r["Prod"] = "2026-10-03"
    assert current(r) == 4 and gaps(r) == ["Self-QA", "QA"] and column(r) == "Prod"
    toggle(r, "SelfQA")
    assert r["SelfQA"].endswith("*") and gaps(r) == ["QA"]
    toggle(r, "SelfQA")
    assert r["SelfQA"] == "-"
    assert column(r | {"Role": "reviewer"}) == "Review"
    assert column(r | {"State": "merged"}) is None
    lines = ["    - [/] TECH-5311 thing\n", "        - 🔀 www!1 old\n", "        - note\n", "    - [/] TECH-9 other\n"]
    row = r | {"Repo": "www.vidangel.com", "MR": "945"}
    out = annotate_lines(lines, {"TECH-5311": [row]})
    assert out[1].startswith("        - 🔀 www!945 ") and out[2] == "        - note\n" and len(out) == 4
    assert annotate_lines(out, {"TECH-5311": [row]}) == out
    print("ok")


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("board", [])
    if cmd == "board":
        os.execvp("agent-dashboard", ["agent-dashboard", "--view", "board"])
    if cmd == "selftest":
        return selftest()
    if cmd == "sync":
        rows, failed = sync()
        print(f"{len(rows)} MRs tracked" + (f"; skipped: {', '.join(sorted(failed))}" if failed else ""))
    elif cmd == "add":
        task = next((a for a in args if a.upper().startswith("TECH")), "")
        ref = next((a for a in args if a != task), None)
        print(f"tracking {add(ref, task_of(task))}")
    elif cmd in ("set", "task"):
        if len(args) < 2:
            sys.exit(__doc__)
        rows = load()
        hits = resolve(rows, args[0])
        if not hits:
            sys.exit(f"no tracked MR matches {args[0]}")
        keys = [r["Key"] for r in hits]
        if cmd == "task":
            set_field(keys, "TaskID", task_of(args[1]))
        else:
            mode = "clear" if "--clear" in args else "auto" if "--auto" in args else "set"
            set_stage(keys, stage_arg(args[1]), mode)
        for r in load().values():
            if r["Key"] in keys:
                print(fmt_row(r))
    elif cmd == "ls":
        rows = list(load().values())
        if "--all" not in args:
            rows = [r for r in rows if r["State"] == "opened" or (r["State"] == "merged" and not reached(r["Prod"]))]
        if "--json" in args:
            print(json.dumps(rows, indent=1))
        else:
            for r in sorted(rows, key=lambda r: (r["Role"] != "author", -current(r), r["Repo"])):
                print(fmt_row(r))
    elif cmd == "badge":
        hits = resolve(load(), args[0]) if args else []
        print("  ".join(f"{short_repo(r['Repo'])}!{r['MR']} {badge(r)}" for r in hits))
    elif cmd == "annotate":
        print(f"{annotate(*args[:1])} tracker lines changed")
    elif cmd == "import-notes":
        print(f"tracking {len(import_notes(*args[:1]))} MRs from notes")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
