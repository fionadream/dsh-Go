#!/usr/bin/env python3
"""Python-only prototype contract tests; no compiler, binary or APK is executed."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile


RUNTIME = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "tawcroot_build", RUNTIME / "prototypes/tawcroot/build.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


def synthetic_elf(abi, *, elf_type=2, machine=None, alignment=16384,
                  interpreter=None, needed=None, vaddr_delta=0, entry_delta=512,
                  load_type=1):
    """Small ELF metadata fixture only; it contains no executable workload."""
    _, expected_machine, base = BUILD.ABIS[abi]
    data = bytearray(1024)
    segments = [(load_type, 5, 0, base + vaddr_delta, 0, len(data), len(data), alignment)]
    if interpreter is not None:
        encoded = interpreter.encode() + b"\0"
        data[512:512 + len(encoded)] = encoded
        segments.append((3, 4, 512, base + 512, 0, len(encoded), len(encoded), 1))
    if needed is not None:
        strings = b"\0" + needed.encode() + b"\0"
        data[768:768 + len(strings)] = strings
        dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in
                           [(5, base + 768), (10, len(strings)), (1, 1), (0, 0)])
        data[640:640 + len(dynamic)] = dynamic
        segments.append((2, 4, 640, base + 640, 0, len(dynamic), len(dynamic), 8))
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    struct.pack_into("<16sHHIQQQIHHHHHH", data, 0, ident, elf_type,
                     expected_machine if machine is None else machine, 1,
                     base + entry_delta, 64, 0, 0, 64, 56, len(segments), 0, 0, 0)
    for index, segment in enumerate(segments):
        struct.pack_into("<IIQQQQQQ", data, 64 + index * 56, *segment)
    return bytes(data)


class PrototypeBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime"
        self.prototype = self.runtime / "prototypes/tawcroot"
        self.prototype.mkdir(parents=True)
        self.source = json.loads((RUNTIME / "prototypes/tawcroot/source.json").read_text())
        self.archive = self.root / "fixture.zip"
        prefix = self.source["archiveRoot"] + "/"
        self.license = b"MIT notice fixture; not a substitute for the upstream license.\n"
        with zipfile.ZipFile(self.archive, "w") as zipped:
            for name in self.source["cSources"] + [
                    f"src/arch/{arch}_{suffix}.S"
                    for arch, _, _ in BUILD.ABIS.values()
                    for suffix in ("stub", "loader_jump")]:
                zipped.writestr(prefix + "tawcroot/" + name, "/* fixture */\n")
            zipped.writestr(prefix + "tawcroot/include/example.h", "/* fixture header */\n")
            zipped.writestr(prefix + "LICENSE.MIT", self.license)
            zipped.writestr(prefix + "tawcroot/tests/testhost/example.c", "not production\n")
            zipped.writestr(prefix + "tawcroot/build.sh", "must not execute\n")
            zipped.writestr(prefix + "app/src/main/example.kt", "not part of this build\n")
        self.source["archiveSha256"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        (self.prototype / "source.json").write_text(json.dumps(self.source))
        (self.prototype / "THIRD_PARTY_NOTICES.draft.md").write_text("test notices\n")
        self.ndk = self.root / "existing-ndk"
        self.binary = self.ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin"
        self.binary.mkdir(parents=True)
        (self.ndk / "source.properties").write_text("Pkg.Revision = 28.2.13676358\n")
        for name in ["clang", "ld.lld", "llvm-strip",
                     "aarch64-linux-android29-clang", "x86_64-linux-android29-clang"]:
            tool = self.binary / name
            tool.write_bytes(b"")
            tool.chmod(0o755)
        self.calls = []

    def fake_compiler(self, command, **kwargs):
        # This mock intercepts every external-tool call in build(). None of these
        # empty tool files or synthetic ELF files is ever executed.
        self.calls.append(list(command))
        self.assertTrue(kwargs["check"])
        name = Path(command[0]).name
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout="fixture " + name + " version\n")
        if name == "llvm-strip":
            path = Path(command[1])
            path.write_bytes(path.read_bytes() + b"fixture strip")
            return subprocess.CompletedProcess(command, 0)
        output = Path(command[command.index("-o") + 1])
        if "-c" in command:
            source = Path(kwargs["cwd"]) / command[command.index("-c") + 1]
            self.assertTrue(source.is_file())
            output.write_bytes(b"fixture object")
        else:
            objects = [Path(arg) for arg in command if arg.endswith(".o")]
            self.assertEqual(len(objects), len(self.source["cSources"]) + 2)
            self.assertTrue(all(path.is_file() for path in objects))
            abi = "arm64-v8a" if name.startswith("aarch64") else "x86_64"
            output.write_bytes(synthetic_elf(abi))
        return subprocess.CompletedProcess(command, 0)

    def cache(self, supplied=None, offline=False):
        return BUILD.cached_archive(self.source, self.runtime / ".cache/tawcroot",
                                    supplied, offline)

    def test_offline_import_and_reuse_never_open_network(self):
        with mock.patch.object(BUILD.urllib.request, "urlopen") as network:
            path = self.cache(self.archive, offline=True)
            self.assertEqual(path.read_bytes(), self.archive.read_bytes())
            self.assertEqual(self.cache(offline=True), path)
            network.assert_not_called()

    def test_missing_offline_archive_fails(self):
        with mock.patch.object(BUILD.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "离线缓存缺少"):
                self.cache(offline=True)
            network.assert_not_called()

    def test_bad_import_preserves_verified_cache_and_removes_partial(self):
        path = self.cache(self.archive)
        original = path.read_bytes()
        bad = self.root / "bad.zip"
        bad.write_bytes(b"wrong bytes")
        with self.assertRaisesRegex(ValueError, "SHA256 不符"):
            self.cache(bad)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_corrupt_cache_fails_without_silent_redownload(self):
        path = self.cache(self.archive)
        path.write_bytes(b"corrupt cache")
        with mock.patch.object(BUILD.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "归档缓存 SHA256 不符"):
                self.cache()
            network.assert_not_called()

    def test_download_uses_pinned_url_then_verifies(self):
        with mock.patch.object(BUILD.urllib.request, "urlopen",
                               return_value=io.BytesIO(self.archive.read_bytes())) as network:
            path = self.cache()
            network.assert_called_once_with(self.source["archiveUrl"], timeout=60)
        self.assertEqual(BUILD.sha256(path), self.source["archiveSha256"])

    def test_failed_download_never_publishes_or_leaves_partial(self):
        for failure in ("hash", "transport"):
            with self.subTest(failure=failure):
                options = ({"return_value": io.BytesIO(b"incorrect archive")} if failure == "hash"
                           else {"side_effect": OSError("transport interrupted")})
                with mock.patch.object(BUILD.urllib.request, "urlopen", **options):
                    with self.assertRaises((ValueError, OSError)):
                        self.cache()
                self.assertEqual(list((self.runtime / ".cache/tawcroot").iterdir()), [])

    def test_extract_only_production_sources_headers_and_license(self):
        source = BUILD.extract_source(self.archive, self.source, self.root / "extracted")
        self.assertEqual((source / self.source["cSources"][0]).read_bytes(), b"/* fixture */\n")
        self.assertEqual((source / "include/example.h").read_bytes(), b"/* fixture header */\n")
        self.assertEqual((source.parent / "LICENSE.MIT").read_bytes(), self.license)
        self.assertFalse((source / "build.sh").exists())
        self.assertFalse((source / "tests").exists())
        self.assertFalse((source.parent / "app").exists())

    def test_existing_ndk_revision_and_required_tools(self):
        with mock.patch.object(BUILD.subprocess, "run") as external:
            self.assertEqual(BUILD.toolchain(self.ndk, self.source), self.binary)
            (self.ndk / "source.properties").write_text("Pkg.Revision = 27.2.12479018\n")
            with self.assertRaisesRegex(ValueError, "28.2.13676358"):
                BUILD.toolchain(self.ndk, self.source)
            (self.ndk / "source.properties").write_text("Pkg.Revision = 28.2.13676358\n")
            (self.binary / "llvm-strip").unlink()
            with self.assertRaisesRegex(ValueError, "缺少已有 NDK 工具"):
                BUILD.toolchain(self.ndk, self.source)
            external.assert_not_called()

    def test_elf_accepts_both_abis_and_reports_actual_bytes(self):
        path = self.root / "fixture.so"
        for abi in BUILD.ABIS:
            with self.subTest(abi=abi):
                payload = synthetic_elf(abi)
                path.write_bytes(payload)
                artifact = BUILD.audit_artifact(path, abi)
                self.assertEqual(artifact["sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(artifact["bytes"], len(payload))
                self.assertEqual(artifact["elf"]["loadAlignment"], 16384)
                self.assertIsNone(artifact["elf"]["interpreter"])
                self.assertEqual(artifact["elf"]["needed"], [])

    def test_elf_rejects_wrong_arch_type_dependencies_alignment_and_entry(self):
        path = self.root / "fixture.so"
        cases = [
            {"machine": 183}, {"elf_type": 3}, {"interpreter": "/system/bin/linker64"},
            {"needed": "libc.so"}, {"alignment": 4096}, {"vaddr_delta": 1},
            {"entry_delta": 0x200000}, {"load_type": 4},
        ]
        for values in cases:
            with self.subTest(values=values):
                path.write_bytes(synthetic_elf("x86_64", **values))
                with self.assertRaises(ValueError):
                    BUILD.audit_artifact(path, "x86_64")

    def test_build_publishes_two_abis_exact_manifests_and_notices_outside_normal_out(self):
        with mock.patch.object(BUILD.subprocess, "run", side_effect=self.fake_compiler), \
                mock.patch.object(BUILD.urllib.request, "urlopen") as network:
            output = BUILD.build(self.runtime, self.ndk, self.archive, offline=True)
            network.assert_not_called()
        self.assertEqual(output, self.runtime / ".work/tawcroot-prototype/out")
        self.assertFalse((self.runtime / "out").exists())
        encoded = (output / "manifest.json").read_bytes()
        self.assertEqual(encoded, (output / "assets/tawcroot-prototype/manifest.json").read_bytes())
        manifest = json.loads(encoded)
        self.assertEqual(manifest["source"], {key: self.source[key]
                         for key in ("repo", "commit", "archiveSha256")})
        self.assertEqual(manifest["ndkVersion"], "28.2.13676358")
        self.assertEqual(manifest["clang"], "fixture clang version")
        self.assertEqual(manifest["linker"], "fixture ld.lld version")
        self.assertEqual([item["abi"] for item in manifest["artifacts"]], list(BUILD.ABIS))
        for artifact in manifest["artifacts"]:
            payload = (output / artifact["path"]).read_bytes()
            self.assertTrue(payload.endswith(b"fixture strip"))
            self.assertEqual(artifact["bytes"], len(payload))
            self.assertEqual(artifact["sha256"], hashlib.sha256(payload).hexdigest())
            flags = manifest["flags"][artifact["abi"]]
            compile_calls = [call for call in self.calls
                             if Path(call[0]).name == flags["compiler"] and "-c" in call]
            self.assertEqual([call[call.index("-c") + 1] for call in compile_calls], flags["sources"])
            self.assertTrue(all(call[1:call.index("-c")] == flags["compile"] for call in compile_calls))
            link_call, = [call for call in self.calls
                          if Path(call[0]).name == flags["compiler"] and "-c" not in call]
            self.assertEqual(link_call[1:1 + len(flags["link"])], flags["link"])
        assets = output / "assets/tawcroot-prototype"
        self.assertEqual((assets / "LICENSE.MIT").read_bytes(), self.license)
        self.assertEqual((assets / "THIRD_PARTY_NOTICES.draft.md").read_text(), "test notices\n")
        self.assertEqual(list(output.parent.iterdir()), [output])

    def test_second_abi_compile_failure_preserves_complete_previous_out(self):
        output = self.runtime / ".work/tawcroot-prototype/out"
        output.mkdir(parents=True)
        (output / "manifest.json").write_bytes(b"previous complete output")

        def fail_second(command, **kwargs):
            if Path(command[0]).name.startswith("x86_64"):
                raise subprocess.CalledProcessError(1, command)
            return self.fake_compiler(command, **kwargs)

        with mock.patch.object(BUILD.subprocess, "run", side_effect=fail_second):
            with self.assertRaises(subprocess.CalledProcessError):
                BUILD.build(self.runtime, self.ndk, self.archive, offline=True)
        self.assertEqual((output / "manifest.json").read_bytes(), b"previous complete output")
        self.assertEqual(list(output.iterdir()), [output / "manifest.json"])
        self.assertEqual(list(output.parent.iterdir()), [output])

    def test_second_abi_audit_failure_does_not_publish_partial_build(self):
        def wrong_elf(command, **kwargs):
            result = self.fake_compiler(command, **kwargs)
            if Path(command[0]).name == "llvm-strip" and "x86_64" in command[1]:
                Path(command[1]).write_bytes(synthetic_elf("x86_64", interpreter="/linker"))
            return result

        with mock.patch.object(BUILD.subprocess, "run", side_effect=wrong_elf):
            with self.assertRaisesRegex(ValueError, "PT_INTERP"):
                BUILD.build(self.runtime, self.ndk, self.archive, offline=True)
        self.assertFalse((self.runtime / ".work/tawcroot-prototype/out").exists())
        self.assertEqual(list((self.runtime / ".work/tawcroot-prototype").iterdir()), [])

    def test_failed_out_replacement_restores_previous_out(self):
        staged, output, previous = [self.root / name for name in ("new", "out", "previous")]
        for path, payload in ((staged, "new"), (output, "old")):
            path.mkdir()
            (path / "manifest.json").write_text(payload)
        replace = BUILD.os.replace

        def fail_publish(source, target):
            if source == staged:
                raise OSError("replacement failed")
            replace(source, target)

        with mock.patch.object(BUILD.os, "replace", side_effect=fail_publish):
            with self.assertRaisesRegex(OSError, "replacement failed"):
                BUILD.publish(staged, output, previous)
        self.assertEqual((output / "manifest.json").read_text(), "old")
        self.assertFalse(previous.exists())

    def test_cancel_during_publish_preserves_previous_out_through_build_cleanup(self):
        output = self.runtime / ".work/tawcroot-prototype/out"
        replace = BUILD.os.replace
        for boundary in ("after-backup", "before-publish"):
            with self.subTest(boundary=boundary):
                output.mkdir(parents=True, exist_ok=True)
                (output / "manifest.json").write_bytes(b"previous complete output")

                def cancel(source, target):
                    if boundary == "after-backup" and source == output:
                        replace(source, target)
                        raise KeyboardInterrupt()
                    if boundary == "before-publish" and target == output and source.name == "out":
                        raise KeyboardInterrupt()
                    replace(source, target)

                with mock.patch.object(BUILD.subprocess, "run", side_effect=self.fake_compiler), \
                        mock.patch.object(BUILD.os, "replace", side_effect=cancel):
                    with self.assertRaises(KeyboardInterrupt):
                        BUILD.build(self.runtime, self.ndk, self.archive, offline=True)
                self.assertTrue((output / "manifest.json").is_file())
                self.assertEqual((output / "manifest.json").read_bytes(), b"previous complete output")
                self.assertEqual(list(output.parent.iterdir()), [output])


if __name__ == "__main__":
    unittest.main()
