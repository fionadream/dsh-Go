#!/usr/bin/env python3
"""Host contract tests: fake adb/compiler; host Node runs only tiny local fixtures."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

RUNTIME = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MEASURE = load("tawc_measure", RUNTIME / "prototypes/tawcroot/measure.py")
BUILD_TESTS = load("tawc_build_fixtures", RUNTIME / "tests/test_tawcroot_build.py")
FIXTURE = RUNTIME / "prototypes/tawcroot/fixtures/workloads.mjs"


def device_result(config, phase):
    return {"schema": 1, "prototype": True, "session": config["session"], "runId": config["runId"],
            "round": config["round"], "mode": config["mode"], "suite": config["suite"], "phase": phase,
            "actualExecutor": config["mode"], "status": "PASS", "failureStage": None, "exitCode": 0,
            "identity": {k: config[k] for k in ("apkSha256", "runtimeManifestSha256", "tawcManifestSha256", "abi")},
            "clocks": {"spawnBeforeNs": 1000, "portReadyNs": 2000},
            "readiness": {"token": True, "port": True},
            "stop": {"termSent": 1, "prootQuitSent": 0, "killSent": 0, "parentForced": False,
                     "remainingIdentities": 0, "observationUnavailable": 0, "enumerationFailed": False,
                     "signalErrors": 0, "clean": True},
            "workload": {"status": "PASS", "main": {"exitCode": 0, "wallNs": 123}}}


class FakeAdb:
    def __init__(self, failure=None):
        self.configs, self.starts = {}, []
        self.failure = failure

    def shell(self, args):
        assert args == ["getprop", "ro.kernel.qemu"]
        return subprocess.CompletedProcess(args, 0, stdout=b"1\n")

    def require_disposable_data(self):
        pass

    def private(self, args):
        assert args[0] == "mkdir"

    def put(self, path, data):
        if path.endswith("config.json"):
            config = json.loads(data)
            self.configs[config["session"]] = config

    def start(self, session, action, cold=False):
        self.starts.append((session, action, cold))
        return 11

    def await_result(self, session, action, seconds):
        config = self.configs[session]
        if self.failure == "run-timeout" and action == "run":
            raise TimeoutError("injected missing cancellation acknowledgement")
        row = device_result(config, action)
        if self.failure in ("main-failure", "main-and-cleanup") and action == "run" and config["suite"] == "node":
            row.update(status="FAILED", failureStage="guest", exitCode=7)
            row["workload"].update(status="FAILED", main={"exitCode": 7, "wallNs": 456})
        if self.failure == "main-and-cleanup" and action == "cleanup":
            row.update(status="FAILED", failureStage="cleanup-ownership")
        if self.failure == "prepare-deadline" and action == "prepare":
            row["hostDeadlineExceeded"] = True
        return row


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Consume actual 5040 build()/source.json. Archive/compiler calls are
        # mocked: this is a producer/consumer contract, not real NDK evidence.
        self.build_case = BUILD_TESTS.PrototypeBuildTests("test_offline_import_and_reuse_never_open_network")
        self.build_case.setUp()
        self.addCleanup(self.build_case.doCleanups)
        source = (RUNTIME / "prototypes/tawcroot/source.json").read_bytes()
        (self.build_case.prototype / "source.json").write_bytes(source)
        with mock.patch.object(BUILD_TESTS.BUILD, "cached_archive", return_value=self.build_case.archive), \
                mock.patch.object(BUILD_TESTS.BUILD.subprocess, "run", side_effect=self.build_case.fake_compiler):
            self.output = BUILD_TESTS.BUILD.build(self.build_case.runtime, self.build_case.ndk, offline=True)
        self.manifest = self.output / "manifest.json"
        self.apk = self.root / "synthetic.apk"
        with zipfile.ZipFile(self.apk, "w") as zipped:
            for item in (self.output / "assets").rglob("*"):
                if item.is_file():
                    zipped.write(item, "assets/" + str(item.relative_to(self.output / "assets")))
            zipped.write(self.output / "jniLibs/x86_64/libtawcroot.so", "lib/x86_64/libtawcroot.so")
            zipped.writestr("assets/runtime/manifest.json", json.dumps({"schema": 1, "abi": "x86_64", "runtimeVersion": "fixture"}))

    def run_driver(self, fake, name, extra=()):
        out = self.root / name
        with mock.patch.object(MEASURE, "Adb", return_value=fake), mock.patch("builtins.print"):
            code = MEASURE.main(["--serial", "fixture", "--apk", str(self.apk), "--manifest", str(self.manifest),
                                 "--out", str(out), "--modes", "proot", "--suites", "startup", "--rounds", "1", "--smoke", *extra])
        return code, [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()], out

    def test_real_5040_manifest_contract_is_consumed(self):
        result = MEASURE.inspect_apk(self.apk, self.manifest)
        self.assertEqual(result["abi"], "x86_64")
        self.assertEqual(result["apkSha256"], hashlib.sha256(self.apk.read_bytes()).hexdigest())
        self.assertEqual(json.loads(self.manifest.read_text())["source"]["repo"], "wmww/tawc")

    def test_different_or_missing_packaged_artifact_is_rejected(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                bad = self.root / ("missing.apk" if missing else "wrong.apk")
                with zipfile.ZipFile(self.apk) as original, zipfile.ZipFile(bad, "w") as zipped:
                    for item in original.infolist():
                        if item.filename.endswith("libtawcroot.so"):
                            if not missing:
                                zipped.writestr(item.filename, b"different binary")
                        else:
                            zipped.writestr(item.filename, original.read(item))
                with self.assertRaises((ValueError, KeyError)):
                    MEASURE.inspect_apk(bad, self.manifest)

    def test_wrong_out_manifest_does_not_accept_packaged_one(self):
        wrong = self.root / "wrong.json"
        wrong.write_bytes(self.manifest.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            MEASURE.inspect_apk(self.apk, wrong)

    def test_gate_failure_survives_successful_cleanup_and_keeps_exit(self):
        code, rows, out = self.run_driver(FakeAdb("main-failure"), "failed")
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["status"], "FAILED")
        self.assertEqual(rows[0]["workload"]["main"]["exitCode"], 7)
        self.assertEqual(rows[0]["cleanup"]["status"], "PASS")
        self.assertEqual(MEASURE.wall_ns(rows[0]), 456)
        self.assertTrue(all(row["status"] == "SKIPPED" for row in rows[1:]))
        self.assertIn("456", (out / "results.csv").read_text())

    def test_run_timeout_never_reuses_prepare_clean_for_cleanup(self):
        fake = FakeAdb("run-timeout")
        code, rows, _ = self.run_driver(fake, "timeout")
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["status"], "FAILED")
        self.assertFalse(any(action == "cleanup" for _, action, _ in fake.starts))
        self.assertEqual(len(fake.starts), 2)
        self.assertTrue(all(row["failureStage"] == "prior-stop-unconfirmed" for row in rows[1:]))

    def test_cleanup_failure_is_recorded_without_replacing_main_failure(self):
        code, rows, _ = self.run_driver(FakeAdb("main-and-cleanup"), "double-failure")
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["failureStage"], "guest")
        self.assertEqual(rows[0]["workload"]["main"]["exitCode"], 7)
        self.assertEqual(rows[0]["cleanup"]["failureStage"], "cleanup-ownership")

    def test_failed_git_digest_never_replaces_first_reference(self):
        archive = self.root / "repo.tar"
        with tarfile.open(archive, "w") as tar:
            head = tarfile.TarInfo("repo/.git/HEAD"); head.size = 4
            tar.addfile(head, io.BytesIO(b"head"))

        class GitAdb(FakeAdb):
            def await_result(self, session, action, seconds):
                row = super().await_result(session, action, seconds)
                if row["suite"] == "git" and action == "run":
                    row["workload"]["main"]["stdoutSha256"] = ("a" if row["mode"] == "proot" else "b") * 64
                return row

        code, rows, out = self.run_driver(GitAdb(), "git-reference", ["--modes", "proot,direct,tawcroot", "--suites", "git",
                                                                   "--git-archive", str(archive), "--git-commit", "1" * 40])
        self.assertEqual(code, 1)
        self.assertEqual([r["status"] for r in rows if r["suite"] == "git"], ["PASS", "FAILED", "FAILED"])
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual([r["passed"] for r in summary], [1, 0, 0])

    def test_prepare_deadline_remains_failure_even_when_device_returned_pass(self):
        fake = FakeAdb("prepare-deadline")
        code, rows, _ = self.run_driver(fake, "prepare-timeout")
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["failureStage"], "prepare")
        self.assertTrue(rows[0]["preparation"]["hostDeadlineExceeded"])
        self.assertFalse(any(action == "run" for _, action, _ in fake.starts))

    def test_successful_driver_runs_three_gates_and_one_smoke_round(self):
        fake = FakeAdb()
        code, rows, _ = self.run_driver(fake, "success")
        self.assertEqual(code, 0)
        self.assertEqual([(row["round"], row["suite"]) for row in rows], [(0, "node"), (0, "pty"), (0, "stop"), (1, "startup")])
        self.assertTrue(all(row["status"] == "PASS" for row in rows))
        self.assertTrue(all(cold for _, action, cold in fake.starts if action == "cleanup"))

    def test_rejected_device_record_is_saved_before_validation(self):
        class WrongIdentityAdb(FakeAdb):
            def await_result(self, session, action, seconds):
                row = super().await_result(session, action, seconds)
                if action == "run":
                    row["identity"]["apkSha256"] = "f" * 64
                return row

        fake = WrongIdentityAdb()
        code, rows, out = self.run_driver(fake, "bad-device-record")
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["status"], "FAILED")
        raw = json.loads((out / rows[0]["session"] / "run.json").read_text())
        self.assertEqual(raw["identity"]["apkSha256"], "f" * 64)
        self.assertEqual(raw["status"], "PASS")  # Preserve the producer's contradictory claim.
        self.assertFalse(any(action == "cleanup" for _, action, _ in fake.starts))

    def test_real_adb_guard_runs_before_any_force_stop(self):
        adb = MEASURE.Adb("fixture")
        with mock.patch.object(MEASURE.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")) as call:
            with self.assertRaises(RuntimeError):
                adb.start("a" * 32, "run", cold=True)
        self.assertEqual(call.call_count, 1)
        command = call.call_args.args[0][-1]
        self.assertIn("files/runtime.reset", command)
        self.assertNotIn("force-stop", command)
        parsed = shlex.split(command)
        self.assertEqual(parsed[:4], ["run-as", MEASURE.PACKAGE, "sh", "-c"])

    def test_rotating_paired_blocks_and_failure_exclusion(self):
        trials = list(MEASURE.planned_trials(list(MEASURE.MODES), ["startup"], 10))
        self.assertEqual(len(trials), 39)
        self.assertEqual([m for n, m, _ in trials if n == 2], ["direct", "tawcroot", "proot"])
        rows = []
        for number in range(1, 11):
            for mode, duration in (("proot", 1000), ("direct", 700)):
                rows.append({"round": number, "suite": "startup", "mode": mode, "status": "PASS",
                             "clocks": {"spawnBeforeNs": 100, "portReadyNs": 100 + duration}})
        direct = next(r for r in MEASURE.summaries(rows) if r["mode"] == "direct")
        self.assertEqual(direct["medianPairedDeltaNs"], -300)
        self.assertEqual(direct["pairedBootstrap95Ns"], [-300, -300])
        rows[-1]["status"] = "FAILED"
        direct = next(r for r in MEASURE.summaries(rows) if r["mode"] == "direct")
        self.assertEqual(direct["passed"], 9)
        self.assertIsNone(direct["pairedBootstrap95Ns"])

    def test_git_fixture_rejects_external_layout_and_links(self):
        for name, link in (("repo/file", None), ("../escape", None), ("repo/link", "../../escape"), ("repo/link", "/outside")):
            with self.subTest(name=name, link=link):
                archive = self.root / "repo.tar"
                with tarfile.open(archive, "w") as tar:
                    head = tarfile.TarInfo("repo/.git/HEAD"); head.size = 4
                    tar.addfile(head, io.BytesIO(b"head"))
                    item = tarfile.TarInfo(name)
                    if link is not None:
                        item.type = tarfile.SYMTYPE; item.linkname = link
                    tar.addfile(item)
                if name == "repo/file":
                    self.assertEqual(MEASURE.inspect_git_archive(archive), MEASURE.digest(archive))
                else:
                    with self.assertRaises(ValueError):
                        MEASURE.inspect_git_archive(archive)

    def test_pass_cannot_hide_missing_readiness_or_stop_or_wrong_identity(self):
        config = {"session": "1" * 32, "runId": "2" * 32, "round": 1, "mode": "proot", "suite": "startup",
                  **MEASURE.inspect_apk(self.apk, self.manifest)}
        original = device_result(config, "run")
        changes = [("readiness", "token", False), ("stop", "killSent", 1), ("identity", "abi", "arm64-v8a")]
        for group, key, value in changes:
            with self.subTest(group=group):
                row = copy.deepcopy(original); row[group][key] = value
                with self.assertRaises(ValueError):
                    MEASURE.check_result(row, config, "run")

    def node(self, code, env=None):
        executable = shutil.which("node")
        if not executable:
            self.skipTest("existing host Node is required for these local fixture checks")
        return subprocess.run([executable, "--input-type=module", "-e", code], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=15)

    def test_fixture_preserves_main_nonzero_exit_and_reports_only_hashes(self):
        code = f"""const {{runCommand}} = await import({json.dumps(FIXTURE.as_uri())});
          const result = await runCommand('fixture', process.execPath,
            ['-e', 'process.stdout.write("private-output");process.stderr.write("diagnostic");process.exit(7)']);
          console.log(JSON.stringify(result.evidence));"""
        result = json.loads(self.node(code).stdout)
        self.assertEqual(result["exitCode"], 7)
        self.assertEqual(result["stdoutSha256"], hashlib.sha256(b"private-output").hexdigest())
        self.assertGreater(result["wallNs"], 0)
        self.assertNotIn("private-output", json.dumps(result))

    def test_stop_marker_accepts_split_data_and_rejects_truncated_eof(self):
        code = f"""import {{PassThrough}} from 'node:stream';
          const {{waitForMarker}} = await import({json.dumps(FIXTURE.as_uri())});
          const stream = new PassThrough(); const result = waitForMarker(stream, 'PROBE_FIFO_ATTEMPT', 1000);
          stream.write('PROBE_'); setTimeout(()=>stream.write('FIFO_'), 5);
          setTimeout(()=>stream.end('ATTEMPT'), 10);
          const short = new PassThrough(); const bad = waitForMarker(short, 'PROBE_FIFO_ATTEMPT', 1000);
          short.end('PROBE_FIFO_'); console.log(JSON.stringify([await result, await bad]));"""
        self.assertEqual(json.loads(self.node(code).stdout), [True, False])

    def test_web_restart_rejects_nonzero_and_early_exit_after_readiness(self):
        # Deterministic child/connection event ordering, not a real DSH server.
        code = f"""import cp from 'node:child_process'; import net from 'node:net';
          import {{EventEmitter}} from 'node:events'; import {{PassThrough}} from 'node:stream';
          const {{restartWeb}} = await import({json.dumps(FIXTURE.as_uri())});
          const spawn = cp.spawn, connect = net.createConnection, results = [];
          const cases = [{{code:0,signal:null}}, {{code:null,signal:'SIGTERM'}},
            {{code:7,signal:null}}, {{code:0,signal:null,early:true}}, {{code:7,signal:null,early:true}}];
          try {{ for (const item of cases) {{
            let child;
            const exit = () => {{ child.exitCode = item.code; child.signalCode = item.signal; child.emit('exit', item.code, item.signal); }};
            cp.spawn = () => {{
              child = new EventEmitter(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
              child.exitCode = null; child.signalCode = null;
              child.kill = () => {{ setImmediate(exit); return true; }};
              setImmediate(() => child.stdout.write('http://127.0.0.1:12345/?token=local-fixture-only\\n'));
              return child;
            }};
            net.createConnection = () => {{
              const socket = new EventEmitter(); socket.setTimeout = () => socket; socket.destroy = () => socket;
              setImmediate(() => {{ if (item.early) exit(); socket.emit('connect'); }}); return socket;
            }};
            try {{ const result = await restartWeb({{webArguments:['web','--port','12345']}});
              results.push({{...result,status:result.status ?? 'PASS'}});
            }} catch (error) {{ results.push({{status:'FAILED',label:error.message}}); }}
          }} }} finally {{ cp.spawn = spawn; net.createConnection = connect; }}
          console.log(JSON.stringify(results));"""
        results = json.loads(self.node(code).stdout)
        self.assertEqual([r["status"] for r in results], ["PASS", "PASS", "FAILED", "FAILED", "FAILED"])

    def test_wasm_gate_executes_exported_function(self):
        code = f"const {{wasmProbe}} = await import({json.dumps(FIXTURE.as_uri())}); console.log(JSON.stringify(await wasmProbe()));"
        result = json.loads(self.node(code).stdout)
        self.assertEqual(result, {"exportedFunction": "i32-add", "arguments": [37, 5], "result": 42, "calls": 1})

    def test_plugin_contract_uses_array_membership(self):
        home = self.root / "dsh-home"
        profile = home / "profiles/web"; (profile / "node_modules/dsh-git-badge").mkdir(parents=True)
        (profile / "package.json").write_text(json.dumps({"dependencies": {"dsh-git-badge": "0.20.1"},
                                                         "dsh": {"profile": {"bundles": ["dsh-git-badge"]}}}))
        code = f"const {{pluginState}} = await import({json.dumps(FIXTURE.as_uri())}); console.log(JSON.stringify(pluginState(true)));"
        result = json.loads(self.node(code, env=dict(os.environ, DSH_HOME=str(home))).stdout)
        self.assertTrue(result["installed"])

    def test_direct_fixture_entry_reads_physical_config_without_fs_shim(self):
        root = self.root / "private-root"; config = root / "opt/dsh-prototype/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"mode": "direct", "suite": "startup"}))
        code = f"""const root = {json.dumps(str(root))};
          globalThis[Symbol.for('dsh.go.direct')] = {{toHost:(p)=>p.startsWith(root+'/') ? p : root+p}};
          process.argv = [process.execPath, {json.dumps(str(FIXTURE))}, 'prepare'];
          await import({json.dumps(FIXTURE.as_uri())});"""
        output = self.node(code).stdout
        value = json.loads(output.removeprefix("DSH_TAWC_PROBE "))
        self.assertEqual(value["status"], "PASS")
        self.assertEqual(value["nodeExecutor"], "direct-node")


if __name__ == "__main__":
    unittest.main()
