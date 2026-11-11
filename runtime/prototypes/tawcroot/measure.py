#!/usr/bin/env python3
"""Isolated Android prototype; never selects an existing runtime/rootfs.

Requires an already installed opt-in debug APK and adb. Python's standard library
is sufficient. Guest execution occurs in TawcrootPrototypeActivity, not run-as.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path, PurePosixPath
import random
import re
import shlex
import statistics
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile


HERE = Path(__file__).resolve().parent
PACKAGE = "io.github.ccch1mneyyy.dsh_app"
ACTIVITY = PACKAGE + "/.runtime.probe.TawcrootPrototypeActivity"
BASE = "files/tawcroot-prototype"
COMMIT = "a49a4075228f4801dadc1eee584aed33fe8ea4ce"
ARCHIVE_SHA256 = "74487243bde86c303e786e79cd4a13e98c3279cae4ab7725dcf5fa51c0b530c1"
MODES = ("proot", "direct", "tawcroot")
SUITES = ("startup", "pnpm", "apt", "git")
GATES = ("node", "pty", "stop")
SESSION = re.compile(r"[0-9a-f]{32}\Z")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def selected(text, choices):
    values = text.split(",")
    if not values or len(values) != len(set(values)) or any(v not in choices for v in values):
        raise argparse.ArgumentTypeError("choose distinct comma-separated values: " + ",".join(choices))
    return values


def inspect_apk(apk, manifest):
    """Check the artifact actually supplied to this measurement, including packaging."""
    expected = Path(manifest).read_bytes()
    with zipfile.ZipFile(apk) as archive:
        actual = archive.read("assets/tawcroot-prototype/manifest.json")
        if actual != expected:
            raise ValueError("APK prototype manifest differs from OUT/manifest.json")
        build = json.loads(actual)
        if (build["schema"] != 1 or build["source"]["repo"] != "wmww/tawc"
                or build["source"]["commit"] != COMMIT
                or build["source"]["archiveSha256"] != ARCHIVE_SHA256 or build["ndkVersion"] != "28.2.13676358"):
            raise ValueError("unexpected prototype source identity")
        runtime_bytes = archive.read("assets/runtime/manifest.json")
        runtime = json.loads(runtime_bytes)
        abi = runtime["abi"]
        if abi not in ("arm64-v8a", "x86_64"):
            raise ValueError("unsupported runtime ABI")
        records = [r for r in build["artifacts"] if r["abi"] == abi]
        if len(records) != 1 or records[0]["path"] != f"jniLibs/{abi}/libtawcroot.so":
            raise ValueError("missing or ambiguous prototype artifact")
        record = records[0]
        binary = archive.read(f"lib/{abi}/libtawcroot.so")
        if len(binary) != record["bytes"] or hashlib.sha256(binary).hexdigest() != record["sha256"]:
            raise ValueError("packaged tawcroot bytes differ from build manifest")
        for name in ("LICENSE.MIT", "THIRD_PARTY_NOTICES.draft.md"):
            if not archive.read("assets/tawcroot-prototype/" + name).strip():
                raise ValueError("missing prototype notice: " + name)
    return {"apkSha256": digest(apk), "runtimeManifestSha256": hashlib.sha256(runtime_bytes).hexdigest(),
            "tawcManifestSha256": hashlib.sha256(expected).hexdigest(), "abi": abi}


def inspect_git_archive(path):
    """Only the explicitly supplied offline repository copy is transferred."""
    has_head = False
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "repo":
                raise ValueError("git fixture entries must stay under repo/")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("git fixture supports files, directories and internal symlinks only")
            if member.issym():
                target = PurePosixPath(member.linkname)
                parts = list(name.parent.parts)
                if target.is_absolute():
                    raise ValueError("git fixture contains an absolute symlink")
                for part in target.parts:
                    if part == "..":
                        if len(parts) <= 1:
                            raise ValueError("git fixture symlink leaves repo/")
                        parts.pop()
                    elif part != ".":
                        parts.append(part)
            has_head |= member.name.rstrip("/") == "repo/.git/HEAD" and member.isfile()
    if not has_head:
        raise ValueError("git fixture must contain a complete repo/.git/HEAD, not a worktree pointer")
    return digest(path)


class Adb:
    def __init__(self, serial, executable="adb"):
        self.prefix = [executable, "-s", serial]

    def shell(self, args, data=None, check=True, timeout=60):
        # adb forwards a shell command. Quote every argument once, including sh -c's body.
        result = subprocess.run(self.prefix + ["shell", "-T", shlex.join(args)], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"adb command failed ({result.returncode}): {args[0]}")
        return result

    def private(self, args, **kwargs):
        return self.shell(["run-as", PACKAGE, *args], **kwargs)

    def put(self, path, content):
        self.private(["sh", "-c", "umask 077; cat > " + shlex.quote(path)], data=content)

    def get_json(self, path):
        result = self.private(["cat", path], check=False)
        return json.loads(result.stdout) if result.returncode == 0 else None

    def require_disposable_data(self):
        # Do not inspect, rename, clear or install over a user's runtime.
        self.private(["sh", "-c", "test ! -e files/runtime && test ! -e files/runtime.reset && test ! -e files/runtime-keep"])

    def start(self, session, action, cold=False):
        if cold:
            self.require_disposable_data()
            self.shell(["am", "force-stop", PACKAGE])
        result = self.shell(["am", "start", "-W", "-n", ACTIVITY, "--es", "session", session,
                             "--es", "action", action])
        text = result.stdout.decode("utf-8", "replace")
        if "Error:" in text or "Exception" in text:
            raise RuntimeError("Activity launch rejected; inspect debug manifest and shell permission")
        match = re.search(r"^TotalTime:\s*(\d+)\s*$", text, re.M)
        return int(match.group(1)) if match else None

    def await_result(self, session, action, seconds):
        path = (f"files/tawcroot-prototype-cleanup/{session}.json" if action == "cleanup"
                else f"{BASE}/{session}/{action}.json")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.get_json(path)
            if value is not None:
                return value
            time.sleep(0.5)
        if action != "cleanup":
            self.put(f"{BASE}/{session}/cancel", b"1")
            for _ in range(30):
                value = self.get_json(path)
                if value is not None:
                    value["hostDeadlineExceeded"] = True
                    return value
                time.sleep(0.5)
        raise TimeoutError("probe did not acknowledge cancellation; session retained; stop this run")


def check_result(row, config, phase):
    if (row.get("schema") != 1 or row.get("prototype") is not True or row.get("session") != config["session"]
            or row.get("phase") != phase or row.get("mode") != config["mode"] or row.get("suite") != config["suite"]
            or row.get("status") not in ("PASS", "FAILED", "SKIPPED")):
        raise ValueError("probe result does not match this session")
    if row["status"] == "PASS":
        if phase != "cleanup":
            identity = row["identity"]
            for key in ("apkSha256", "runtimeManifestSha256", "tawcManifestSha256", "abi"):
                if identity[key] != config[key]:
                    raise ValueError("running APK identity differs: " + key)
        if not row["stop"]["clean"] or row["stop"]["killSent"] or row["stop"]["parentForced"]:
            raise ValueError("PASS contradicts stop evidence")
        if phase == "run" and config["suite"] == "startup":
            if not row["readiness"]["token"] or not row["readiness"]["port"]:
                raise ValueError("startup PASS lacks token and port")
        if phase == "run" and config["suite"] not in ("startup", "stop"):
            if row["exitCode"] != 0 or row["workload"]["status"] != "PASS":
                raise ValueError("workload PASS contradicts exit or checks")
    return row


def planned_trials(modes, suites, rounds):
    for mode in modes:
        for suite in GATES:
            yield 0, mode, suite
    for number in range(1, rounds + 1):
        ordered = modes[(number - 1) % len(modes):] + modes[:(number - 1) % len(modes)]
        for suite in suites:
            for mode in ordered:
                yield number, mode, suite


def wall_ns(row):
    if row["suite"] == "startup":
        clocks = row.get("clocks", {})
        if clocks.get("portReadyNs") is not None and clocks.get("spawnBeforeNs") is not None:
            return clocks["portReadyNs"] - clocks["spawnBeforeNs"]
        return None
    return (row.get("workload") or {}).get("main", {}).get("wallNs")


def summaries(rows):
    results = []
    for suite in SUITES:
        selected_rows = [r for r in rows if r["suite"] == suite and r["round"] > 0]
        baseline = {r["round"]: wall_ns(r) for r in selected_rows if r["mode"] == "proot" and r["status"] == "PASS"}
        for mode in MODES:
            group = [r for r in selected_rows if r["mode"] == mode]
            if not group:
                continue
            values = [wall_ns(r) for r in group if r["status"] == "PASS" and wall_ns(r) is not None]
            differences = [wall_ns(r) - baseline[r["round"]] for r in group
                           if r["status"] == "PASS" and wall_ns(r) is not None and baseline.get(r["round"]) is not None]
            interval = None
            if len(differences) >= 10:
                rng = random.Random(5042)
                bootstrap = sorted(statistics.median(rng.choices(differences, k=len(differences))) for _ in range(2000))
                interval = [bootstrap[49], bootstrap[1949]]
            results.append({"suite": suite, "mode": mode, "attempts": len(group),
                            "passed": len(values), "failedOrSkipped": len(group) - len(values),
                            "medianWallNs": statistics.median(values) if values else None,
                            "pairedRounds": len(differences),
                            "medianPairedDeltaNs": statistics.median(differences) if differences else None,
                            "pairedBootstrap95Ns": interval,
                            "interpretation": "descriptive only; correlated cache/network rounds and debug instrumentation; no stable p95"})
    return results


def write_tables(out, rows):
    (out / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    columns = ("round", "mode", "actualExecutor", "suite", "status", "failureStage", "wallNs", "exitCode",
               "api", "actualTarget", "abi", "token", "port", "termSent", "killSent", "remainingIdentities", "clean")
    with (out / "results.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            flat = {key: row.get(key) for key in columns}
            flat["wallNs"] = wall_ns(row)
            flat.update({k: row.get("identity", {}).get(k) for k in ("api", "actualTarget", "abi")})
            flat.update({k: row.get("readiness", {}).get(k) for k in ("token", "port")})
            flat.update({k: row.get("stop", {}).get(k) for k in ("termSent", "killSent", "remainingIdentities", "clean")})
            writer.writerow(flat)
    (out / "summary.json").write_text(json.dumps(summaries(rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", required=True)
    p.add_argument("--adb", default="adb")
    p.add_argument("--apk", required=True, type=Path, help="the exact already installed opt-in debug APK")
    p.add_argument("--manifest", type=Path, default=HERE.parents[1] / ".work/tawcroot-prototype/out/manifest.json")
    p.add_argument("--out", required=True, type=Path, help="new host result directory")
    p.add_argument("--modes", type=lambda v: selected(v, MODES), default=list(MODES))
    p.add_argument("--suites", type=lambda v: selected(v, SUITES), default=list(SUITES))
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--smoke", action="store_true", help="allow fewer than 10 rounds; never a performance claim")
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--pnpm-cache", choices=("cold", "warm"), default="cold")
    p.add_argument("--git-archive", type=Path, help="trusted offline tar copy under repo/, including .git")
    p.add_argument("--git-commit", help="expected 40-hex commit of that copy")
    p.add_argument("--allow-device", action="store_true", help="explicitly allow a disposable physical-device test installation")
    p.add_argument("--keep-sessions", action="store_true", help="retain private session units after verified stop")
    p.add_argument("--timeout", type=int, default=600, help="per prepare/run phase seconds; cancellation is still verified")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.rounds < 1 or (args.rounds < 10 and not args.smoke and not args.preflight_only) or not 10 <= args.timeout <= 3600:
        raise ValueError("use at least 10 rounds, or explicitly --smoke; timeout must be 10..3600 seconds")
    if args.out.exists():
        raise ValueError("--out must not already exist")
    identities = inspect_apk(args.apk, args.manifest)
    git_sha = None
    if not args.preflight_only and "git" in args.suites:
        if not args.git_archive or not args.git_commit or not re.fullmatch("[0-9a-f]{40}", args.git_commit):
            raise ValueError("git suite requires --git-archive and --git-commit")
        git_sha = inspect_git_archive(args.git_archive)
    fixture = (HERE / "fixtures/workloads.mjs").read_bytes()
    adb = Adb(args.serial, args.adb)
    if not args.allow_device and adb.shell(["getprop", "ro.kernel.qemu"]).stdout.strip() != b"1":
        raise ValueError("default scope is an emulator; a disposable device installation needs --allow-device")
    adb.require_disposable_data()
    args.out.mkdir(parents=True)
    run_id = uuid.uuid4().hex
    rows, failed_modes = [], set()
    signatures = {}
    fatal = False
    trials = list(planned_trials(args.modes, [] if args.preflight_only else args.suites, args.rounds))
    for number, mode, suite in trials:
        session = uuid.uuid4().hex
        config = {"schema": 1, "session": session, "runId": run_id, "round": number, "mode": mode, "suite": suite,
                  "fixtureSha256": hashlib.sha256(fixture).hexdigest(), "gitSha256": git_sha,
                  "gitCommit": args.git_commit, "pnpmCache": args.pnpm_cache, "timeoutSeconds": args.timeout,
                  "aptVersion": signatures.get("aptVersion"), **identities}
        row = {"schema": 1, "prototype": True, "session": session, "runId": run_id, "round": number,
               "mode": mode, "suite": suite, "phase": "run", "actualExecutor": None, "status": "SKIPPED",
               "failureStage": "prior-stop-unconfirmed" if fatal else "compatibility-gate"}
        if fatal or mode in failed_modes:
            rows.append(row)
            write_tables(args.out, rows)
            continue
        trial_out = args.out / session
        trial_out.mkdir()
        clean = False
        try:
            adb.private(["mkdir", "-p", f"{BASE}/{session}"])
            adb.put(f"{BASE}/{session}/config.json", (json.dumps(config) + "\n").encode())
            adb.put(f"{BASE}/{session}/workloads.mjs", fixture)
            if suite == "git":
                adb.put(f"{BASE}/{session}/git.tar", args.git_archive.read_bytes())
            adb.start(session, "prepare", cold=True)
            preparation = adb.await_result(session, "prepare", args.timeout)
            (trial_out / "prepare.json").write_text(json.dumps(preparation, indent=2) + "\n", encoding="utf-8")
            check_result(preparation, config, "prepare")
            row = preparation | {"phase": "run", "preparation": preparation}
            clean = preparation.get("stop", {}).get("clean", False)
            if preparation["status"] == "PASS" and not preparation.get("hostDeadlineExceeded"):
                apt = (preparation.get("workload") or {}).get("apt")
                if apt:
                    signature = (apt["version"], apt["archivesSha256"])
                    if "apt" in signatures and signatures["apt"] != signature:
                        raise ValueError("apt version/cache differs between paired preparations")
                    signatures["apt"], signatures["aptVersion"] = signature, apt["version"]
                clean = False  # Preparation's stop is not evidence about the next process.
                am_time = adb.start(session, "run", cold=True)
                row = adb.await_result(session, "run", args.timeout)
                (trial_out / "run.json").write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
                check_result(row, config, "run")
                row["preparation"] = preparation
                row["host"] = {"amStartTotalTimeMs": am_time, "deviceClockIndependent": True, "smoke": args.smoke}
                clean = row.get("stop", {}).get("clean", False)
                if suite == "git" and row["status"] == "PASS":
                    signature = row["workload"]["main"]["stdoutSha256"]
                    if "git" in signatures and signatures["git"] != signature:
                        row.update(status="FAILED", failureStage="git-paired-output")
                    signatures.setdefault("git", signature)
            else:
                row.update(status="FAILED", failureStage="prepare")
            if row.get("hostDeadlineExceeded"):
                if row["status"] == "PASS":
                    row["failureStage"] = "host-deadline"
                row["status"] = "FAILED"
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            row.update(status="FAILED", failureStage="host-driver", error={"type": type(error).__name__, "detail": str(error)})
            # A host/transport exception after launch is not proof that the guest stopped.
            fatal = True
        finally:
            if clean and not args.keep_sessions:
                try:
                    # The result is published just before the Activity worker returns.
                    # A confirmed stop permits a fresh process without racing its busy flag.
                    adb.start(session, "cleanup", cold=True)
                    cleanup = adb.await_result(session, "cleanup", 60)
                    (trial_out / "cleanup.json").write_text(json.dumps(cleanup, indent=2) + "\n", encoding="utf-8")
                    row["cleanup"] = cleanup
                    check_result(cleanup, config, "cleanup")
                    if cleanup.get("status") != "PASS":
                        if row["status"] == "PASS":
                            row["failureStage"] = "cleanup"
                        row["status"] = "FAILED"
                        fatal = True
                except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
                    row["cleanupError"] = {"type": type(error).__name__}
                    if row["status"] == "PASS":
                        row["failureStage"] = "cleanup"
                    row["status"] = "FAILED"
                    fatal = True
            elif not clean:
                fatal = True
            if number == 0 and row["status"] != "PASS":
                failed_modes.add(mode)
            rows.append(row)
            write_tables(args.out, rows)
            print(json.dumps({k: row.get(k) for k in ("session", "round", "mode", "suite", "status", "failureStage")}), flush=True)
    return 0 if rows and all(r["status"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, tarfile.TarError, RuntimeError) as error:
        print(f"FAILED prototype measurement preflight: {error}", file=sys.stderr)
        sys.exit(2)
