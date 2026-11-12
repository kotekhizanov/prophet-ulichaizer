"""Prophet updates: the plugin's server (standard library only).

It looks at the Prophet checkout (PROPHET_REPO, ~/Projects/prophet_v2 by
default), fetches its upstream branch, says which merged pull requests the
checkout lacks, and on the owner's click updates the checkout and restarts the
core the way `make dev` runs it.

    GET  /status   what the last check found and how an update goes
    POST /check    fetch from GitHub now
    POST /update   pull, uv sync, database, migrate, restart the core

Only Omniscient's origins (from PROPHET_URL, plus UPDATER_ORIGINS) get CORS
answers, and POSTs from any other origin are refused.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path.home()
REPO = Path(os.path.expanduser(os.environ.get("PROPHET_REPO", "~/Projects/prophet_v2")))
PROPHET_URL = os.environ.get("PROPHET_URL", "http://127.0.0.1:8770").rstrip("/")
PORT = int(os.environ.get("UPDATER_PORT", "8797"))
BIND = os.environ.get("UPDATER_BIND", "127.0.0.1")
DATA = Path(os.path.expanduser(os.environ.get("UPDATER_DATA", "~/.local/share/prophet-updater")))
CORE_LOG = DATA / "core.log"
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", str(HOME / ".local/bin"), str(HOME / ".cargo/bin")]
MAX_PRS = 40
HEALTH_WAIT = 180


def log(*args) -> None:
    print("[updater]", *args, flush=True)


def origins() -> set[str]:
    """Omniscient's origins: PROPHET_URL, its localhost twin, and UPDATER_ORIGINS."""
    out = set()
    parts = urlsplit(PROPHET_URL)
    out.add(f"{parts.scheme}://{parts.netloc}")
    port = f":{parts.port}" if parts.port else ""
    if parts.hostname in ("127.0.0.1", "localhost"):
        out.add(f"{parts.scheme}://127.0.0.1{port}")
        out.add(f"{parts.scheme}://localhost{port}")
    for o in os.environ.get("UPDATER_ORIGINS", "").split(","):
        if o.strip():
            out.add(o.strip().rstrip("/"))
    return out


ORIGINS = origins()


def tool_env() -> dict:
    """The environment for git, uv, make: no node-only variables, the usual tool folders."""
    env = {k: v for k, v in os.environ.items() if k not in ("PROPHET_URL", "VIRTUAL_ENV", "PROPHET_KEY")}
    path = env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":")
    env["PATH"] = ":".join(path + [p for p in EXTRA_PATH if p not in path])
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run(args: list[str], timeout: float = 120, env: dict | None = None, check: bool = True) -> str:
    p = subprocess.run(
        args, cwd=REPO, env=env or tool_env(), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )  # fmt: skip
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[-600:]}")
    return p.stdout.strip()


def git(*args: str, timeout: float = 120) -> str:
    return run(["git", *args], timeout=timeout)


# ---------------------------------------------------------------- the check

state_lock = threading.Lock()
status: dict = {"checked_at": None, "error": None, "behind": 0, "current": None, "latest": None, "prs": [], "commits": []}
update: dict = {"state": "idle", "step": None, "log": [], "error": None, "started_at": None}
pr_cache: dict[str, list[dict]] = {}  # sha -> its pull requests


def repo_slug() -> str | None:
    """owner/name of the upstream remote on GitHub."""
    remote = git("config", "--get", f"remote.{upstream().split('/')[0]}.url")
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def upstream() -> str:
    return git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")


def commit_info(ref: str) -> dict:
    sha, subject, date = git("log", "-1", "--format=%H%x1f%s%x1f%cI", ref).split("\x1f")
    return {"sha": sha, "short": sha[:7], "subject": subject, "date": date}


def gh_api(path: str):
    """GitHub's REST API through the gh CLI (the owner's sign-in), else anonymous."""
    try:
        out = run(["gh", "api", "-H", "Accept: application/vnd.github+json", path], timeout=30)
        return json.loads(out)
    except Exception:
        pass
    req = urllib.request.Request(f"https://api.github.com/{path}", headers={"Accept": "application/vnd.github+json", "User-Agent": "prophet-updater"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def pr_view(p: dict) -> dict:
    body = (p.get("body") or "").strip()
    return {
        "number": p["number"],
        "title": p.get("title") or "",
        "body": body[:4000] + ("…" if len(body) > 4000 else ""),
        "url": p.get("html_url"),
        "author": (p.get("user") or {}).get("login"),
        "merged_at": p.get("merged_at"),
    }


def prs_for(slug: str, sha: str, subject: str) -> list[dict]:
    if sha in pr_cache:
        return pr_cache[sha]
    found: list[dict] = []
    m = re.match(r"Merge pull request #(\d+)", subject)
    try:
        if m:
            found = [pr_view(gh_api(f"repos/{slug}/pulls/{m.group(1)}"))]
        else:
            found = [pr_view(p) for p in gh_api(f"repos/{slug}/commits/{sha}/pulls") if p.get("merged_at")]
    except Exception as exc:
        log("no PR for", sha[:7], exc)
        return found  # not cached: try again next time
    pr_cache[sha] = found
    return found


def check() -> dict:
    """Fetch the upstream branch and see what the checkout lacks."""
    try:
        up = upstream()
        git("fetch", "--quiet", up.split("/")[0], timeout=120)
        current, latest = commit_info("HEAD"), commit_info(up)
        lines = git("log", "--first-parent", "--format=%H%x1f%s%x1f%cI", f"HEAD..{up}")
        commits = []
        for line in filter(None, lines.splitlines()):
            sha, subject, date = line.split("\x1f")
            commits.append({"sha": sha, "short": sha[:7], "subject": subject, "date": date})
        behind = int(git("rev-list", "--count", f"HEAD..{up}") or 0)
        prs, seen = [], set()
        slug = repo_slug()
        if slug:
            for c in commits[:MAX_PRS]:
                for p in prs_for(slug, c["sha"], c["subject"]):
                    if p["number"] not in seen:
                        seen.add(p["number"])
                        prs.append(p)
        new = {
            "checked_at": datetime.now(timezone.utc).isoformat(), "error": None, "behind": behind,
            "branch": up, "repo": slug, "current": current, "latest": latest,
            "prs": prs, "commits": commits[:100],
        }  # fmt: skip
    except Exception as exc:
        log("check failed:", exc)
        new = {**status, "checked_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
    with state_lock:
        status.clear()
        status.update(new)
    log(f"checked: {new.get('behind', 0)} commits behind" if not new.get("error") else "check failed")
    return new


# ---------------------------------------------------------------- the update

def step(name: str) -> None:
    with state_lock:
        update["step"] = name
        update["log"].append(f"{time.strftime('%H:%M:%S')} {name}")
    log(name)


def note(text: str) -> None:
    with state_lock:
        update["log"].append(text[-2000:])


def core_port() -> int:
    return urlsplit(PROPHET_URL).port or 8770


def listening_pids(port: int) -> list[int]:
    out = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], check=False)
    return sorted({int(x) for x in out.split()})


def proc(pid: int, field: str) -> str:
    return run(["ps", "-o", f"{field}=", "-p", str(pid)], check=False).strip()


def dev_db_url() -> str:
    m = re.search(r"^DEV_DB\s*:?=\s*(\S+)", (REPO / "Makefile").read_text(), re.M)
    return m.group(1) if m else "postgresql://postgres:postgres@localhost:5432/prophet"


def proc_cwd(pid: int) -> str:
    out = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], check=False)
    return next((x[1:] for x in out.splitlines() if x.startswith("n")), "")


def running_core() -> dict | None:
    """The core on its port: the uvicorn that listens, whether it reloads, its host and database."""
    for pid in listening_pids(core_port()):
        cmd = proc(pid, "command")
        if "uvicorn" not in cmd:
            continue
        env = run(["ps", "eww", "-o", "command=", "-p", str(pid)], check=False)
        db = re.search(r"\bPROPHET_DATABASE_URL=(\S+)", env)
        host = re.search(r"--host[ =](\S+)", cmd)
        return {
            "pid": pid, "reload": "--reload" in cmd,
            "in_repo": Path(proc_cwd(pid) or "/").resolve() == REPO.resolve(),
            "host": host.group(1) if host else "127.0.0.1",
            "db": db.group(1) if db else None,
        }  # fmt: skip
    return None


def worker_pids(reloader: int) -> set[int]:
    """The reloader's worker processes (not multiprocessing's resource tracker)."""
    out = run(["pgrep", "-P", str(reloader)], check=False)
    return {int(x) for x in out.split() if "resource_tracker" not in proc(int(x), "command")}


def compose_files() -> list[str]:
    """compose.dev.yaml, and the Mac's own compose.local.yaml override when there is one."""
    files = ["-f", "compose.dev.yaml"]
    if (REPO / "compose.local.yaml").exists():
        files += ["-f", "compose.local.yaml"]
    return files


def stop_core() -> str:
    """Stop the core on its port (and the `make dev` around it); its --host, to start it the same way."""
    host = "127.0.0.1"
    pids = listening_pids(core_port())
    targets = set()
    for pid in pids:
        cmd = proc(pid, "command")
        m = re.search(r"--host[ =](\S+)", cmd)
        if m:
            host = m.group(1)
        targets.add(pid)
        parent = int(proc(pid, "ppid") or 0)
        while parent > 1:  # uvicorn's reloader under `uv run` under `make`
            pcmd = proc(parent, "command")
            if not re.search(r"\b(uv|uvicorn|make)\b", pcmd):
                break
            targets.add(parent)
            if re.search(r"\bmake\b", pcmd):
                break
            parent = int(proc(parent, "ppid") or 0)
    if not targets:
        note("The core was not running.")
        return host
    note(f"Stopping the core: pids {sorted(targets)}")
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(40):
        if not listening_pids(core_port()):
            return host
        time.sleep(0.5)
    for pid in listening_pids(core_port()):
        os.kill(pid, signal.SIGKILL)
    time.sleep(1)
    return host


def start_core(host: str, db: str) -> None:
    """The core as `make dev` runs it (the database and migrations are done already)."""
    DATA.mkdir(parents=True, exist_ok=True)
    out = open(CORE_LOG, "ab")
    out.write(f"\n==== {datetime.now().isoformat()} core started by Prophet updates\n".encode())
    out.flush()
    args = ["uv", "run", "uvicorn", "--factory", "prophet.dev:app", "--reload",
            "--timeout-graceful-shutdown", "3", "--host", host, "--port", str(core_port())]  # fmt: skip
    subprocess.Popen(
        args, cwd=REPO, env={**tool_env(), "PROPHET_DATABASE_URL": db},
        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
        start_new_session=True,  # outlives this server and the node
    )  # fmt: skip
    note(f"Started the core on {host}:{core_port()}; its log: {CORE_LOG}")


def restart_core(core: dict | None, db: str) -> None:
    """A core that reloads from this checkout gets a fresh worker (its reloader stays, so
    whatever started it, a terminal or a preview, keeps it); any other is stopped and
    started again from this checkout."""
    if core and core["reload"] and core["in_repo"]:
        before = worker_pids(core["pid"])
        note(f"Reloading the core's worker (uvicorn --reload, pid {core['pid']})")
        (REPO / "src/prophet/dev.py").touch()
        for _ in range(60):
            now = worker_pids(core["pid"])
            if now and not (now & before):
                return
            time.sleep(0.5)
        note("The worker did not change in 30 s; stopping and starting the core instead")
    host = stop_core()
    start_core(core["host"] if core else host, db)


def core_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{PROPHET_URL}/health", timeout=3) as r:
            return json.load(r).get("status") == "ok"
    except Exception:
        return False


def do_update() -> None:
    try:
        step("Checking the checkout")
        dirty = git("status", "--porcelain", "--untracked-files=no")
        if dirty:
            raise RuntimeError(f"{REPO} has uncommitted changes:\n{dirty[:800]}")
        up = upstream()
        step(f"git pull --ff-only ({up})")
        note(git("pull", "--ff-only", timeout=300)[-1500:])
        step("uv sync")
        note(run(["uv", "sync"], timeout=600)[-800:])
        core = running_core()
        db = (core or {}).get("db") or dev_db_url()
        step("Database (docker compose " + " ".join(compose_files()[1::2]) + ")")
        run(["docker", "compose", *compose_files(), "up", "-d", "--wait"], timeout=300)
        step("Migrations")
        env = {**tool_env(), "PROPHET_DATABASE_URL": db}
        note(run(["uv", "run", "prophet", "migrate"], timeout=300, env=env)[-1500:])
        step("Restarting the core")
        with state_lock:
            update["state"] = "restarting"
        restart_core(core, db)
        step("Waiting for the core")
        for _ in range(HEALTH_WAIT):
            if core_healthy():
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"the core did not answer /health in {HEALTH_WAIT} s; see {CORE_LOG}")
        step("Done")
        with state_lock:
            update["state"] = "done"
        check()
    except Exception as exc:
        log("update failed:", exc)
        with state_lock:
            update["state"] = "failed"
            update["error"] = str(exc)
            update["log"].append(f"Failed: {exc}")


def start_update() -> bool:
    with state_lock:
        if update["state"] in ("running", "restarting"):
            return False
        update.update(state="running", step=None, log=[], error=None, started_at=datetime.now(timezone.utc).isoformat())
    threading.Thread(target=do_update, daemon=True).start()
    return True


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "prophet-updater"

    def log_message(self, fmt, *args):  # quiet: the node keeps stdout
        pass

    def cors(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Vary", "Origin")
        return bool(origin and origin in ORIGINS)

    def answer(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def snapshot(self) -> dict:
        with state_lock:
            return {**json.loads(json.dumps(status)), "update": json.loads(json.dumps(update)), "repo_path": str(REPO)}

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/status":
            return self.answer(200, self.snapshot())
        if self.path == "/":
            return self.answer(200, {"plugin": "prophet-updater", "repo": str(REPO)})
        self.answer(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("Origin") not in ORIGINS:
            return self.answer(403, {"error": "origin not allowed"})
        path = self.path.split("?")[0]
        if path == "/check":
            check()
            return self.answer(200, self.snapshot())
        if path == "/update":
            if not start_update():
                return self.answer(409, {"error": "an update is already running"})
            return self.answer(202, self.snapshot())
        self.answer(404, {"error": "not found"})


def main() -> None:
    if not (REPO / ".git").exists():
        log(f"no git checkout at {REPO}; set PROPHET_REPO")
        sys.exit(2)
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"listening on http://{BIND}:{PORT}, repo {REPO}, origins {sorted(ORIGINS)}")
    threading.Thread(target=check, daemon=True).start()
    server.serve_forever()
    log("stopped")


if __name__ == "__main__":
    main()
