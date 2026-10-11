#!/usr/bin/env python3
"""Make herdr see coding agents that run inside `docker exec` sessions.

Herdr recognizes agents by looking at the foreground process of a pane. When
the pane runs `docker exec -it <container> ...` (as dev-container wrappers
do), the foreground process on the host is the docker client, and the real
agent lives under the container's shim, invisible to herdr.

This daemon bridges that gap. Every tick it:

1. lists herdr panes and finds the ones whose foreground process is a
   `docker exec` client;
2. maps that client to the exec session's process on the host (Docker API
   gives the host PID of every exec; the session is the one that started
   right after the client did);
3. reads the tty foreground process group of that session from host /proc
   and checks whether it is a known agent (`claude`, `codex`, ...);
4. claims the pane for that agent with `pane.report_agent` and keeps its
   state current by running herdr's own screen classifier (`agent.explain`)
   on the pane, so idle/working/blocked match what herdr would have detected
   natively;
5. releases the pane when the agent exits.

Subcommands:
  ensure   start the daemon in the background unless it is already running
  run      run the daemon in the foreground
  stop     stop the running daemon
  status   print whether the daemon runs and which agents herdr sees
  scan     dry run: print the wrapped agents found, without reporting them
"""

import errno
import fcntl
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Tuple

SOURCE = "plugin:wrapped-agent"

# Executable basename -> herdr agent label (the name of herdr's screen manifest).
DEFAULT_AGENTS = {
    "claude": "claude",
    "codex": "codex",
    "copilot": "copilot",
    "cursor-agent": "cursor",
    "opencode": "opencode",
    "pi": "pi",
    "omp": "omp",
    "droid": "droid",
    "devin": "devin",
    "kimi": "kimi",
    "kilo": "kilo",
    "hermes": "hermes",
    "qodercli": "qodercli",
    "qwen": "qwen",
    "letta": "letta",
    "grok": "grok",
    "amp": "amp",
    "kiro-cli": "kiro",
    "gemini": "gemini",
    "cline": "cline",
}

# Interpreters whose script argument names the agent (`node .../bin/codex`).
INTERPRETERS = {"node", "nodejs", "bun", "deno", "python", "python3"}

CLIENTS = {"docker"}

# `docker exec` options that take a value in the next argv slot.
EXEC_VALUE_OPTS = {"-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir", "--detach-keys"}
EXEC_VALUE_SHORT = set("euw")
# `docker` global options that take a value in the next argv slot.
GLOBAL_VALUE_OPTS = {"-c", "--context", "-H", "--host", "--config", "-l", "--log-level",
                     "--tlscacert", "--tlscert", "--tlskey"}

LIVE_STATES = {"idle", "working", "blocked"}


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------

def parse_exec_container(argv: List[str]) -> Optional[str]:
    """Return the container named by a `docker [global opts] [container] exec` argv."""
    if not argv or os.path.basename(argv[0]) not in CLIENTS:
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        opt = argv[i]
        i += 2 if opt in GLOBAL_VALUE_OPTS else 1
    if i < len(argv) and argv[i] == "container":
        i += 1
    if i >= len(argv) or argv[i] != "exec":
        return None
    i += 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        if arg.startswith("--"):
            i += 1 if "=" in arg else (2 if arg in EXEC_VALUE_OPTS else 1)
            continue
        # Short cluster such as -it or -itu1000 or -e FOO=1.
        consumed_next = False
        for pos, ch in enumerate(arg[1:], start=1):
            if ch in EXEC_VALUE_SHORT:
                consumed_next = pos == len(arg) - 1
                break
        i += 2 if consumed_next else 1
    return argv[i] if i < len(argv) else None


def match_execs(clients: List[Tuple[int, int]], execs: List[Tuple[int, int]],
                taken: Iterable[int] = ()) -> Dict[int, int]:
    """Pair docker clients with the exec sessions they started.

    clients and execs are (pid, start_ticks). A client's session is the first
    exec that started at or after the client; clients are paired oldest first
    so that several sessions into one container resolve unambiguously.
    """
    free = sorted((e for e in execs if e[0] not in set(taken)), key=lambda e: e[1])
    result = {}
    for cpid, cstart in sorted(clients, key=lambda c: c[1]):
        for idx, (epid, estart) in enumerate(free):
            if estart >= cstart:
                result[cpid] = epid
                del free[idx]
                break
    return result


def identify_agent(comm: str, argv: List[str], env_hint: Optional[str],
                   agents: Dict[str, str]) -> Optional[str]:
    """Return the herdr agent label for a process, or None."""
    if env_hint:
        return env_hint
    names = [comm]
    if argv:
        names.append(os.path.basename(argv[0]))
        if os.path.basename(argv[0]).rstrip("0123456789.") in INTERPRETERS:
            for arg in argv[1:]:
                if not arg.startswith("-"):
                    base = os.path.basename(arg)
                    for ext in (".js", ".mjs", ".cjs", ".ts", ".py"):
                        if base.endswith(ext):
                            base = base[: -len(ext)]
                    names.append(base)
                    break
    for name in names:
        if name in agents:
            return agents[name]
    return None


# ---------------------------------------------------------------------------
# Host /proc access
# ---------------------------------------------------------------------------

class Proc:
    def __init__(self, pid: int, comm: str, ppid: int, pgrp: int, tpgid: int, start: int):
        self.pid, self.comm, self.ppid, self.pgrp, self.tpgid, self.start = pid, comm, ppid, pgrp, tpgid, start


def read_proc(pid: int) -> Optional[Proc]:
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
    except OSError:
        return None
    lparen, rparen = raw.find("("), raw.rfind(")")
    fields = raw[rparen + 2:].split()
    return Proc(pid, raw[lparen + 1:rparen], int(fields[1]), int(fields[2]), int(fields[5]), int(fields[19]))


def read_argv(pid: int) -> List[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def read_env_var(pid: int, name: str) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            data = f.read()
    except OSError:
        return None
    prefix = name.encode() + b"="
    for item in data.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix):].decode(errors="replace") or None
    return None


def children(pid: int) -> List[int]:
    out = []
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return out
    for tid in tids:
        try:
            with open(f"/proc/{pid}/task/{tid}/children") as f:
                out.extend(int(c) for c in f.read().split())
        except OSError:
            pass
    return out


def foreground_agent(exec_pid: int, agents: Dict[str, str]) -> Optional[str]:
    """Return the agent in the tty foreground of an exec session, if any."""
    root = read_proc(exec_pid)
    if root is None or root.tpgid <= 0 or root.tpgid == root.pgrp:
        return None
    fg, queue, seen = [], [exec_pid], 0
    while queue and seen < 2000:
        pid = queue.pop()
        seen += 1
        proc = read_proc(pid)
        if proc is None:
            continue
        if proc.pgrp == root.tpgid:
            fg.append(proc)
        queue.extend(children(pid))
    # Group leader first: it is the command the user typed.
    fg.sort(key=lambda p: p.pid != root.tpgid)
    for proc in fg:
        label = identify_agent(proc.comm, read_argv(proc.pid), read_env_var(proc.pid, "HERDR_AGENT"), agents)
        if label:
            return label
    return None


# ---------------------------------------------------------------------------
# Docker API
# ---------------------------------------------------------------------------

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 3.0):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


class DockerAPI:
    def __init__(self):
        host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
        self.path = host[len("unix://"):] if host.startswith("unix://") else "/var/run/docker.sock"
        self.exec_pids = {}  # exec id -> host pid (immutable once started)

    def _get(self, url: str):
        conn = UnixHTTPConnection(self.path)
        try:
            conn.request("GET", url)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                return None
            return json.loads(body)
        finally:
            conn.close()

    def exec_pids_for(self, container: str) -> List[int]:
        info = self._get(f"/containers/{container}/json")
        if not info:
            return []
        pids = []
        for exec_id in info.get("ExecIDs") or []:
            pid = self.exec_pids.get(exec_id)
            if pid is None:
                ex = self._get(f"/exec/{exec_id}/json")
                if not ex or not ex.get("Running") or not ex.get("Pid"):
                    continue
                pid = self.exec_pids[exec_id] = int(ex["Pid"])
            pids.append(pid)
        live = set(info.get("ExecIDs") or [])
        for exec_id in list(self.exec_pids):
            if exec_id not in live:
                del self.exec_pids[exec_id]
        return pids


# ---------------------------------------------------------------------------
# Herdr API
# ---------------------------------------------------------------------------

class HerdrError(Exception):
    pass


class Herdr:
    def __init__(self, socket_path: str, bin_path: str):
        self.socket_path = socket_path
        self.bin_path = bin_path
        self.counter = 0

    def call(self, method: str, params: dict, timeout: float = 3.0) -> dict:
        self.counter += 1
        req = {"id": f"{SOURCE}:{self.counter}", "method": method, "params": params}
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall((json.dumps(req) + "\n").encode())
            line = sock.makefile("rb").readline()
        finally:
            sock.close()
        resp = json.loads(line)
        if "error" in resp:
            raise HerdrError(f"{method}: {resp['error'].get('code')}: {resp['error'].get('message')}")
        return resp["result"]

    def report(self, pane_id: str, agent: str, state: str):
        self.call("pane.report_agent", {"pane_id": pane_id, "source": SOURCE, "agent": agent,
                                        "state": state, "seq": time.time_ns()})

    def release(self, pane_id: str, agent: str):
        self.call("pane.release_agent", {"pane_id": pane_id, "source": SOURCE, "agent": agent,
                                         "seq": time.time_ns()})

    def explain_live(self, pane_id: str) -> Optional[str]:
        result = self.call("agent.explain", {"target": pane_id})
        explain = result.get("explain", result)
        if explain.get("skip_state_update"):
            return None
        return explain.get("state")

    def explain_offline(self, pane_id: str, agent: str) -> Optional[str]:
        """Classify the pane's screen before we hold it (live explain needs a holder)."""
        text = self.call("pane.read", {"pane_id": pane_id, "source": "detection"})["read"]["text"]
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
        try:
            out = subprocess.run([self.bin_path, "agent", "explain", "--file", f.name, "--agent", agent, "--json"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if out.returncode != 0:
                return None
            explain = json.loads(out.stdout)
            return None if explain.get("skip_state_update") else explain.get("state")
        finally:
            os.unlink(f.name)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

def state_dir() -> str:
    d = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "herdr-wrapped-agent")
    os.makedirs(d, exist_ok=True)
    return d


def instance_paths(socket_path: str) -> Tuple[str, str, str]:
    """Lock, pid and log file for the daemon serving one herdr socket."""
    key = hashlib.sha1(socket_path.encode()).hexdigest()[:12]
    d = state_dir()
    return (os.path.join(d, f"daemon-{key}.lock"), os.path.join(d, f"daemon-{key}.pid"),
            os.path.join(d, f"daemon-{key}.log"))


def load_config() -> dict:
    cfg = {"interval": 1.0, "agents": dict(DEFAULT_AGENTS)}
    cdir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if cdir:
        try:
            with open(os.path.join(cdir, "config.json")) as f:
                user = json.load(f)
            cfg["interval"] = float(user.get("interval", cfg["interval"]))
            cfg["agents"].update(user.get("agents", {}))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log(f"ignoring bad config.json: {e}")
    return cfg


LOG_FILE = None


def log(msg: str):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
    if LOG_FILE is None:
        sys.stderr.write(line)
        return
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except OSError:
        pass


class Daemon:
    def __init__(self, herdr: Herdr, cfg: dict):
        self.herdr = herdr
        self.cfg = cfg
        self.docker = DockerAPI()
        self.client_exec = {}  # (client pid, client start) -> exec pid
        self.held = {}  # pane id -> (agent, last reported state)

    def resolve_exec(self, client: Proc, container: str) -> Optional[int]:
        key = (client.pid, client.start)
        exec_pid = self.client_exec.get(key)
        if exec_pid is not None and read_proc(exec_pid) is not None:
            return exec_pid
        candidates = []
        for pid in self.docker.exec_pids_for(container):
            proc = read_proc(pid)
            if proc is not None:
                candidates.append((pid, proc.start))
        taken = [pid for k, pid in self.client_exec.items() if k != key and read_proc(k[0]) is not None]
        exec_pid = match_execs([(client.pid, client.start)], candidates, taken).get(client.pid)
        if exec_pid is not None:
            self.client_exec[key] = exec_pid
            log(f"client {client.pid} ({container}) -> exec {exec_pid}")
        return exec_pid

    def wrapped_agent(self, pane_id: str) -> Tuple[bool, Optional[str]]:
        """Return (pane runs a docker exec client, agent in that session)."""
        info = self.herdr.call("pane.process_info", {"pane_id": pane_id})["process_info"]
        for fp in info.get("foreground_processes") or []:
            container = parse_exec_container(fp.get("argv") or [])
            if not container:
                continue
            client = read_proc(fp["pid"])
            if client is None:
                continue
            exec_pid = self.resolve_exec(client, container)
            if exec_pid is None:
                return True, None
            return True, foreground_agent(exec_pid, self.cfg["agents"])
        return False, None

    def update_pane(self, pane: dict):
        pane_id = pane["pane_id"]
        wrapped, agent = self.wrapped_agent(pane_id)
        held = self.held.get(pane_id)
        if held is None and wrapped and pane.get("agent"):
            # Nothing else claims docker exec panes: a previous daemon did.
            held = self.held[pane_id] = (pane["agent"], pane.get("agent_status"))
        if agent is None:
            if held:
                self.herdr.release(pane_id, held[0])
                del self.held[pane_id]
                log(f"{pane_id}: released {held[0]}")
            return
        if held is None or held[0] != agent:
            state = self.herdr.explain_offline(pane_id, agent) or "idle"
            self.herdr.report(pane_id, agent, state)
            self.held[pane_id] = (agent, state)
            log(f"{pane_id}: claimed {agent} ({state})")
            return
        state = self.herdr.explain_live(pane_id)
        if state in LIVE_STATES and state != held[1]:
            self.herdr.report(pane_id, agent, state)
            self.held[pane_id] = (agent, state)
            log(f"{pane_id}: {agent} {held[1]} -> {state}")

    def tick(self):
        panes = self.herdr.call("pane.list", {})["panes"]
        live = {p["pane_id"] for p in panes}
        for pane_id in list(self.held):
            if pane_id not in live:
                del self.held[pane_id]
        for pane in panes:
            try:
                self.update_pane(pane)
            except (HerdrError, OSError, ValueError, KeyError) as e:
                log(f"{pane['pane_id']}: {e}")
        for key in list(self.client_exec):
            proc = read_proc(key[0])
            if proc is None or proc.start != key[1]:
                del self.client_exec[key]

    def release_all(self):
        for pane_id, (agent, _) in list(self.held.items()):
            try:
                self.herdr.release(pane_id, agent)
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass
        self.held.clear()


def herdr_from_env() -> Herdr:
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        sys.exit("HERDR_SOCKET_PATH is not set; run this from a herdr plugin or pane")
    return Herdr(socket_path, os.environ.get("HERDR_BIN_PATH") or "herdr")


def cmd_run() -> int:
    global LOG_FILE
    herdr = herdr_from_env()
    lock_path, pid_path, LOG_FILE = instance_paths(herdr.socket_path)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES):
            log("another daemon already serves this herdr socket")
            return 0
        raise
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))

    cfg = load_config()
    daemon = Daemon(herdr, cfg)
    log(f"started pid={os.getpid()} socket={herdr.socket_path} interval={cfg['interval']}")
    failing_since = None
    while not stopping:
        try:
            daemon.tick()
            failing_since = None
        except (OSError, HerdrError, ValueError) as e:
            failing_since = failing_since or time.time()
            if time.time() - failing_since > 60:
                log(f"herdr unreachable for 60s, exiting: {e}")
                break
        time.sleep(cfg["interval"])
    daemon.release_all()
    log("stopped")
    try:
        os.unlink(pid_path)
    except OSError:
        pass
    return 0


def running_pid(socket_path: str) -> Optional[int]:
    lock_path, pid_path, _ = instance_paths(socket_path)
    try:
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None  # we got the lock, nobody holds it
    except OSError:
        pass
    try:
        with open(pid_path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return -1


def cmd_ensure() -> int:
    herdr = herdr_from_env()
    if running_pid(herdr.socket_path):
        return 0
    _, _, log_path = instance_paths(herdr.socket_path)
    with open(os.devnull, "rb") as devnull_in, open(log_path, "a") as out:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "run"], stdin=devnull_in, stdout=out,
                         stderr=out, start_new_session=True, close_fds=True)
    print("wrapped-agent daemon started")
    return 0


def cmd_stop() -> int:
    herdr = herdr_from_env()
    pid = running_pid(herdr.socket_path)
    if not pid or pid < 0:
        print("wrapped-agent daemon is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not running_pid(herdr.socket_path):
            print("wrapped-agent daemon stopped")
            return 0
        time.sleep(0.1)
    print(f"wrapped-agent daemon (pid {pid}) did not stop")
    return 1


def cmd_status() -> int:
    herdr = herdr_from_env()
    pid = running_pid(herdr.socket_path)
    _, _, log_path = instance_paths(herdr.socket_path)
    print(f"daemon: {'running pid ' + str(pid) if pid else 'not running'}")
    print(f"log: {log_path}")
    try:
        for agent in herdr.call("agent.list", {})["agents"]:
            print(f"  {agent['pane_id']}: {agent.get('agent')} {agent.get('agent_status')}")
    except (OSError, HerdrError) as e:
        print(f"herdr: {e}")
    return 0


def cmd_scan() -> int:
    """Dry run: show what the daemon would report, without touching herdr state."""
    herdr = herdr_from_env()
    daemon = Daemon(herdr, load_config())
    for pane in herdr.call("pane.list", {})["panes"]:
        pane_id = pane["pane_id"]
        try:
            _, agent = daemon.wrapped_agent(pane_id)
        except (HerdrError, OSError, ValueError, KeyError) as e:
            print(f"{pane_id}: error: {e}")
            continue
        if agent:
            print(f"{pane_id}: {agent} {herdr.explain_offline(pane_id, agent)}")
    return 0


def main(argv: List[str]) -> int:
    commands = {"run": cmd_run, "ensure": cmd_ensure, "stop": cmd_stop, "status": cmd_status, "scan": cmd_scan}
    if len(argv) != 2 or argv[1] not in commands:
        print(__doc__)
        return 2
    return commands[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
