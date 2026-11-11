#!/usr/bin/env python3
"""TEMP (reverse/tawcroot-proto, never merge): PRoot vs tawcroot vs native chroot.

Linux x86_64 GitHub runner, NOT Android. Every mode gets its own disposable copy of the
same built x86_64 rootfs; nothing here touches an app install or user data.

  proot     Linux build of the pinned PRoot source with the production flags
  tawcroot  the prototype's x86_64 libtawcroot.so (5040 build), directory binds only
  chroot    sudo chroot, native kernel path resolution: the ceiling any launcher can reach
            (the app's Direct mode only takes the Node main process out of PRoot)

Per round the mode order rotates. Startup segments come from the host clock (spawn, address
line, first response) plus an --import probe (Node time origin, listen); both are epoch based
on the same machine. The address-line token is used for the first request and never stored.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import statistics
import subprocess
import threading
import time

MODES = ("proot", "tawcroot", "chroot")
TOKEN = re.compile(rb"^dsh web: (http://127\.0\.0\.1:([0-9]+)/)\?token=([A-Za-z0-9_-]+)", re.M)
FAKE_KEY = "DEEPSEEK_API_KEY=sk-bench-not-a-real-key"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def guest_template(manifest):
    argv = manifest["launch"]["argv"]
    tail = argv[argv.index("/usr/bin/env"):]
    sh = tail.index("/bin/sh")
    env = []
    for item in tail[:sh]:
        if item == "{GUEST_ENV}":
            env += [FAKE_KEY, "TZ=UTC"]
        elif item.startswith("pnpm_config_registry="):
            env.append("pnpm_config_registry=https://registry.npmjs.org/")  # US runner
        else:
            env.append(item)
    return env, tail[-1]


class Mode:
    def __init__(self, name, root, args):
        self.name, self.root, self.args = name, root, args
        self.tmp = root.parent / "proot-tmp"
        self.tmp.mkdir(exist_ok=True)
        dirs = ["var/lib/dsh-app/empty:/sys/fs/selinux", "var/lib/dsh-app/shm:/dev/shm"]
        self.extra_binds = []
        for spec in dirs:
            host, guest = spec.split(":")
            if (root / host).is_dir():
                self.extra_binds.append(f"{root}/{host}:{guest}")
        if args.repo:
            # One copy per mode: git's index records st_uid, which differs between fake-root
            # launchers (0) and the real-root chroot view (the runner uid) and would force rehashes.
            repo = f"{args.repo}-{name}" if Path(f"{args.repo}-{name}").is_dir() else args.repo
            (root / "root/repo").mkdir(parents=True, exist_ok=True)
            self.extra_binds.append(f"{repo}:/root/repo")
        if name in ("proot", "direct"):
            help_text = subprocess.run([args.proot, "--help"], capture_output=True, text=True).stdout
            self.proot_flags = [f for f in ("--kill-on-exit", "--link2symlink", "--sysvipc", "-L") if f in help_text]

    def direct_plan(self, guest, plan_dir):
        """Ask the production DirectLaunch planner (HostPlan.kt jar), as runtime/tests/direct does."""
        plan_dir.mkdir(parents=True, exist_ok=True)
        prefix = [self.args.proot, *self.proot_flags, "-0", f"--rootfs={self.root}", "--cwd=/root",
                  "-b", "/dev", "-b", "/proc"]
        tail = list(guest)
        at = tail.index("/bin/sh")
        tail[at:at] = ["DSH_GO_RUNTIME_ROOT=" + str(self.root)]
        host_env = {"PATH": "/usr/bin:/bin", "HOME": str(plan_dir), "PROOT_L2S_DIR": str(self.root / "root/.l2s")}
        (plan_dir / "input-argv.nul").write_bytes(("\0".join(prefix + tail) + "\0").encode())
        (plan_dir / "input-env.nul").write_bytes(("\0".join(f"{k}={v}" for k, v in host_env.items()) + "\0").encode())
        subprocess.run(["java", "-jar", self.args.planner, str(self.root), "x86_64", str(plan_dir / "input-argv.nul"),
                        str(plan_dir / "input-env.nul"), str(plan_dir), "/bin/sh", "direct"], check=True,
                       capture_output=True)
        argv = [v for v in (plan_dir / "argv.nul").read_bytes().decode().split("\0") if v]
        env = dict(v.split("=", 1) for v in (plan_dir / "env.nul").read_bytes().decode().split("\0") if v)
        selection = json.loads((plan_dir / "selection.json").read_text())
        return argv, env, (plan_dir / "cwd.txt").read_text(), selection

    @property
    def cwd(self):
        return str(self.root / "root") if self.name != "chroot" else "/"

    @property
    def env(self):
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
        if self.name == "proot":
            env.update(PROOT_TMP_DIR=str(self.tmp), PROOT_L2S_DIR=str(self.root / "root/.l2s"))
            if self.args.proot_loader:
                env["PROOT_LOADER"] = self.args.proot_loader
        return env

    def argv(self, guest, pty_mode=False):
        binds = []
        for spec in ["/dev:/dev", "/proc:/proc", "/sys:/sys", *self.extra_binds]:
            binds += ["-b", spec]
        if self.name == "proot":
            return [self.args.proot, *self.proot_flags, "-0", f"--rootfs={self.root}", "--cwd=/root", *binds, *guest]
        if self.name == "tawcroot":
            return [self.args.tawcroot, *([] if pty_mode else ["-s"]), "-r", str(self.root), *binds, "--", *guest]
        return ["sudo", "-n", "chroot", str(self.root), *guest]

    def kill(self, pid, sig):
        if self.name == "chroot":
            return subprocess.run(["sudo", "-n", "kill", f"-{int(sig)}", str(pid)], capture_output=True).returncode == 0
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False

    def markers(self):
        return ["/opt/node/bin/node", "@deepseek-ai/dsh", "dsh web", "libtawcroot", "dsh-git-badge",
                "apt-get", "/usr/lib/git-core", f"--rootfs={self.root}", f"chroot {self.root}"]


def scan(markers, exclude):
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in exclude:
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        if cmd and any(m in cmd for m in markers):
            found.append({"pid": int(entry), "cmd": cmd[:160]})
    return found


def reap(mode, found):
    for item in found:
        mode.kill(item["pid"], signal.SIGKILL)


def tail(path, size=1500):
    try:
        data = Path(path).read_bytes()[-size:]
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def startup_once(mode, env, command, logs):
    root = mode.root
    probe = root / "tmp/bench/probe.jsonl"
    probe.unlink(missing_ok=True)
    pidfile = root / "tmp/dsh-web.pid"
    if pidfile.exists():
        subprocess.run(["sudo", "-n", "rm", "-f", str(pidfile)])
    port = free_port()
    genv = [e + " --import=/tmp/bench/probe.mjs" if e.startswith("NODE_OPTIONS=") else e for e in env]
    if not any(e.startswith("NODE_OPTIONS=") for e in genv):
        genv.append("NODE_OPTIONS=--import=/tmp/bench/probe.mjs")
    guest = ["/usr/bin/env", "-i", *genv, "/bin/sh", "-c", command.replace("{PORT}", str(port))]
    errlog = logs / f"{mode.name}-startup-{time.time_ns()}.stderr"
    row = {"mode": mode.name, "ok": False}
    if mode.name == "direct":
        # Planning (the app's Kotlin prepare step) happens before t0, like the other modes' argv building.
        plain = ["/usr/bin/env", "-i", *env, "/bin/sh", "-c", command.replace("{PORT}", str(port))]
        argv, launch_env, cwd, selection = mode.direct_plan(plain, logs / f"direct-plan-{time.time_ns()}")
        row["directSelection"] = selection
        launch_env["NODE_OPTIONS"] = (launch_env.get("NODE_OPTIONS", "") + " --import=" +
                                      str(root / "tmp/bench/probe.mjs")).strip()
        launch_env["BENCH_PROBE_OUT"] = str(probe)
    else:
        argv, launch_env, cwd = mode.argv(guest), mode.env, mode.cwd
    with errlog.open("wb") as err:
        t0 = time.time()
        proc = subprocess.Popen(argv, cwd=cwd, env=launch_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=err, start_new_session=True)
        token, buf, t_token = None, b"", None
        deadline = t0 + 180
        while time.time() < deadline and token is None:
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk
                match = TOKEN.search(buf)
                if match:
                    t_token = time.time()
                    token = match
            elif proc.poll() is not None:
                break
        drain = threading.Thread(target=lambda: proc.stdout.read(), daemon=True)
        drain.start()
        if token is None:
            row.update(stage="no-address-line", exit=proc.poll())
        else:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", int(token.group(2)), timeout=30)
                conn.request("GET", "/?token=" + token.group(3).decode())
                resp = conn.getresponse()
                resp.read()
                t_first = time.time()
                cookie = (resp.getheader("Set-Cookie") or "").split(";", 1)[0]
                conn.request("GET", "/", headers={"Cookie": cookie})
                page = conn.getresponse()
                page.read()
                row.update(firstStatus=resp.status, cookie=bool(cookie), pageStatus=page.status,
                           ok=resp.status == 303 and bool(cookie) and page.status == 200,
                           spawnToTokenMs=(t_token - t0) * 1000, tokenToFirstResponseMs=(t_first - t_token) * 1000,
                           totalMs=(t_first - t0) * 1000)
            except (OSError, http.client.HTTPException) as error:
                row.update(stage="first-request", error=type(error).__name__)
        events = {}
        try:
            for line in probe.read_text().splitlines():
                item = json.loads(line)
                events.setdefault(item["event"], item["epochMs"])
        except (OSError, ValueError):
            pass
        if "node-start" in events:
            row["spawnToNodeMs"] = events["node-start"] - t0 * 1000
            if "listen" in events:
                row["nodeToListenMs"] = events["listen"] - events["node-start"]
            if "token-line" in events:
                row["nodeToTokenLineMs"] = events["token-line"] - events["node-start"]
        # Stop like the app: TERM the guest dsh from its pid file, then wait for the launcher.
        s0 = time.time()
        try:
            gpid = int(pidfile.read_text().strip())
            row["termSent"] = mode.kill(gpid, signal.SIGTERM)
            row["stopMethod"] = "pidfile"
        except (OSError, ValueError):
            row["stopMethod"] = "process-group"
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                row["termSent"] = True
            except OSError:
                row["termSent"] = False
        try:
            proc.wait(timeout=20)
            row["launcherExited"] = True
        except subprocess.TimeoutExpired:
            row["launcherExited"] = False
        row["stopMs"] = (time.time() - s0) * 1000
        time.sleep(1.0)
        left = scan(mode.markers(), {os.getpid(), os.getppid()})
        row["residual"] = left
        if left:
            reap(mode, left)
        if not row["launcherExited"]:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                mode.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        row["exit"] = proc.returncode
        if row["residual"] or not row["launcherExited"]:
            row["ok"] = False
        if not row["ok"]:
            row["stderrTail"] = tail(errlog)
    return row


def run_guest(mode, env, shell, timeout=900):
    guest = ["/usr/bin/env", "-i", *env, "/bin/sh", "-c", shell]
    t0 = time.time()
    try:
        done = subprocess.run(mode.argv(guest), cwd=mode.cwd, env=mode.env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout)
        rc, out, err = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as error:
        rc, out, err = "timeout", error.stdout or b"", error.stderr or b""
    ms = (time.time() - t0) * 1000
    return {"exit": rc, "ms": ms, "ok": rc == 0,
            "out": out[-800:].decode("utf-8", "replace"), "err": err[-1500:].decode("utf-8", "replace")}


def ctrl_c_once(mode, env):
    guest = ["/usr/bin/env", "-i", *env, "/bin/bash", "--noprofile", "--norc", "-i"]
    argv = mode.argv(guest, pty_mode=True)
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(mode.cwd)
        os.execvpe(argv[0], argv, mode.env)
    seen = b""

    def read_until(pattern, seconds):
        nonlocal seen
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return False
                if not chunk:
                    return False
                seen += chunk
                if re.search(pattern, seen):
                    return True
        return False

    row = {"mode": mode.name}
    os.write(fd, b"echo PTY_READY_$((40+2))\n")
    row["ready"] = read_until(rb"PTY_READY_42", 30)
    os.write(fd, b"sleep 30; echo SLEEP_$((6*7))_END\n")
    time.sleep(1.5)
    t0 = time.time()
    os.write(fd, b"\x03")
    os.write(fd, b"echo CTRLC_DONE_$?\n")
    row["interrupted"] = read_until(rb"CTRLC_DONE_\d+", 10)
    row["interruptMs"] = (time.time() - t0) * 1000
    status = re.search(rb"CTRLC_DONE_(\d+)", seen)
    row["statusAfterInterrupt"] = int(status.group(1)) if status else None
    row["sleepFinished"] = b"SLEEP_42_END" in seen  # only the command's output, never the echoed input
    os.write(fd, b"exit\n")
    end = time.time() + 15
    exited = False
    while time.time() < end:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            exited = True
            break
        try:
            select.select([fd], [], [], 0.2) and os.read(fd, 65536)
        except OSError:
            time.sleep(0.2)
    row["exitedAfterExit"] = exited
    if not exited:
        mode.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    os.close(fd)
    time.sleep(0.5)
    left = scan(mode.markers() + ["/bin/bash --noprofile", "sleep 30"], {os.getpid(), os.getppid()})
    row["residual"] = left
    reap(mode, left)
    row["ok"] = bool(row["ready"] and row["interrupted"] and not row["sleepFinished"] and exited and not left)
    if not row["ok"]:
        row["transcriptTail"] = seen[-600:].decode("utf-8", "replace")
    return row


def describe(values):
    if not values:
        return None
    ordered = sorted(values)
    quart = statistics.quantiles(ordered, n=4) if len(ordered) >= 2 else [ordered[0]] * 3
    return {"n": len(ordered), "median": statistics.median(ordered), "min": ordered[0], "max": ordered[-1],
            "p25": quart[0], "p75": quart[2], "raw": [round(v, 1) for v in values]}


def rotate(seq, k):
    k %= len(seq)
    return list(seq[k:]) + list(seq[:k])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True, help="contains <mode>/rootfs copies")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--proot", required=True)
    p.add_argument("--proot-loader", default="")
    p.add_argument("--tawcroot", required=True)
    p.add_argument("--repo", default="")
    p.add_argument("--planner", default="", help="HostPlan jar (runtime/tests/direct/compile-host-plan.sh) for the direct mode")
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--modes", default=",".join(MODES))
    p.add_argument("--suites", default="startup,ctrlc,plugin,pnpm,apt,git")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    logs = args.out / "stderr"
    logs.mkdir(exist_ok=True)
    manifest = json.loads(args.manifest.read_text())
    env, command = guest_template(manifest)
    modes = [Mode(name, (args.work / name / "rootfs").resolve(), args) for name in args.modes.split(",")]
    suites = args.suites.split(",")
    results = {"scope": "Linux x86_64 GitHub runner, not Android", "rounds": args.rounds, "rows": []}

    def record(suite, rnd, row):
        row.update(suite=suite, round=rnd)
        results["rows"].append(row)
        (args.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
        brief = {k: row.get(k) for k in ("suite", "round", "mode", "ok", "ms", "totalMs", "exit", "interrupted")}
        print("BENCH", json.dumps(brief), flush=True)

    broken = set()
    # Direct only takes the DSH Node main process out of PRoot; the app's tools (apt, git,
    # pnpm under `dsh plugin` from a terminal) still run through PRoot, so it only joins startup.
    tools_only = [m for m in modes if m.name != "direct"]
    plugin_add = ("dsh plugin --profile web add dsh-git-badge@0.20.1 > /tmp/bench/plugin-add.log 2>&1"
                  " || { tail -20 /tmp/bench/plugin-add.log; exit 1; }")
    plugin_remove = "dsh plugin --profile web remove dsh-git-badge > /tmp/bench/plugin-remove.log 2>&1"
    # Round 0: warm-up (first DSH start initialises its home; pnpm's first add is online).
    for mode in modes:
        if "startup" in suites:
            record("startup-warmup", 0, startup_once(mode, env, command, logs))
        if mode.name == "direct":
            continue
        if "plugin" in suites:
            row = run_guest(mode, env, plugin_add)
            row["mode"] = mode.name
            record("plugin-warmup", 0, row)
            run_guest(mode, env, plugin_remove)
        if "pnpm" in suites:
            row = run_guest(mode, env, "command -v pnpm; mkdir -p /root/pb && cd /root/pb && printf '{}' > package.json"
                            " && pnpm add dsh-git-badge@0.20.1")
            row["mode"] = mode.name
            record("pnpm-online-first", 0, row)
            if not row["ok"]:
                broken.add((mode.name, "pnpm"))
        if "git" in suites and args.repo:
            row = run_guest(mode, env, "git -c safe.directory='*' -C /root/repo status --porcelain | wc -l")
            row["mode"] = mode.name
            record("git-warmup", 0, row)
    for rnd in range(1, args.rounds + 1):
        for mode in rotate(modes, rnd - 1):
            if "startup" in suites:
                record("startup", rnd, startup_once(mode, env, command, logs))
            if mode.name == "direct":
                continue
            if "plugin" in suites:
                row = run_guest(mode, env, plugin_add)
                row["mode"] = mode.name
                record("plugin", rnd, row)
                clean = run_guest(mode, env, plugin_remove)
                if not clean["ok"]:
                    record("plugin-remove-failed", rnd, dict(clean, mode=mode.name))
            if "ctrlc" in suites and rnd <= 3:
                row = ctrl_c_once(mode, env)
                record("ctrlc", rnd, row)
            if "pnpm" in suites and (mode.name, "pnpm") not in broken:
                row = run_guest(mode, env, "cd /root/pb && rm -rf node_modules pnpm-lock.yaml && printf '{}' > package.json"
                                " && pnpm add --offline dsh-git-badge@0.20.1 >/dev/null && test -d node_modules/dsh-git-badge")
                row["mode"] = mode.name
                record("pnpm", rnd, row)
            if "apt" in suites:
                row = run_guest(mode, env, "apt-get install -y --no-download hello >/dev/null && hello >/dev/null")
                row["mode"] = mode.name
                record("apt", rnd, row)
                clean = run_guest(mode, env, "apt-get remove -y hello >/dev/null")
                if not clean["ok"]:
                    record("apt-remove-failed", rnd, dict(clean, mode=mode.name))
            if "git" in suites and args.repo:
                row = run_guest(mode, env, "git -c safe.directory='*' -C /root/repo status --porcelain >/dev/null")
                row["mode"] = mode.name
                record("git", rnd, row)

    summary = {}
    for suite, metrics in (("startup", ("spawnToNodeMs", "nodeToListenMs", "nodeToTokenLineMs", "spawnToTokenMs",
                                        "tokenToFirstResponseMs", "totalMs", "stopMs")),
                           ("plugin", ("ms",)), ("pnpm", ("ms",)), ("apt", ("ms",)), ("git", ("ms",))):
        for mode in modes:
            rows = [r for r in results["rows"] if r["suite"] == suite and r["mode"] == mode.name]
            good = [r for r in rows if r.get("ok")]
            summary.setdefault(suite, {})[mode.name] = {
                "attempts": len(rows), "passed": len(good),
                **{m: describe([r[m] for r in good if isinstance(r.get(m), (int, float))]) for m in metrics}}
    ctrl = {}
    for mode in modes:
        rows = [r for r in results["rows"] if r["suite"] == "ctrlc" and r["mode"] == mode.name]
        ctrl[mode.name] = {"attempts": len(rows), "passed": sum(1 for r in rows if r.get("ok"))}
    summary["ctrlc"] = ctrl
    results["summary"] = summary
    (args.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print("SUMMARY", json.dumps({s: {m: {k: (v["median"] if isinstance(v, dict) and "median" in v else v)
                                          for k, v in d.items()} for m, d in ms.items()}
                                 for s, ms in summary.items()}), flush=True)


if __name__ == "__main__":
    main()
