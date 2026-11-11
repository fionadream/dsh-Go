#!/usr/bin/env python3
"""Build the isolated tawcroot prototype with the project's existing NDK.

The normal runtime/APK build does not invoke this entry point. Source archives are
verified before extraction; upstream production sources are compiled unchanged.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parents[1]
sys.path.insert(0, str(RUNTIME / "lib"))
from rtbuild import Elf


ABIS = {
    "arm64-v8a": ("aarch64", 183, 0x2000000000),
    "x86_64": ("x86_64", 62, 0x40000000),
}
COMPILE_FLAGS = [
    "-O2", "-fno-strict-aliasing", "-fno-stack-protector",
    "-fvisibility=hidden", "-fno-exceptions",
    "-fno-asynchronous-unwind-tables", "-ffreestanding",
    "-Wall", "-Wextra", "-Werror", "-Wframe-larger-than=1024", "-Iinclude",
]
LINK_FLAGS = [
    "-static", "-nostdlib", "-nostartfiles", "-Wl,--build-id=none",
    "-Wl,-z,noexecstack", "-Wl,-z,relro", "-Wl,-z,max-page-size=16384",
]


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cached_archive(source, cache, supplied=None, offline=False):
    """Return the pinned archive; never publish an unverified cache entry."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (source["commit"] + ".zip")
    if supplied is None and target.exists():
        if sha256(target) != source["archiveSha256"]:
            raise ValueError("归档缓存 SHA256 不符；请用 --archive 提供正确原件")
        return target
    if supplied is None and offline:
        raise ValueError("离线缓存缺少固定归档；请先用 --archive 导入")
    with tempfile.NamedTemporaryFile(dir=cache, suffix=".part", delete=False) as temp:
        pending = Path(temp.name)
        try:
            if supplied is not None:
                stream = Path(supplied).open("rb")
            else:
                stream = urllib.request.urlopen(source["archiveUrl"], timeout=60)
            with stream:
                shutil.copyfileobj(stream, temp)
            temp.flush()
            if sha256(pending) != source["archiveSha256"]:
                raise ValueError("来源归档 SHA256 不符；未更新缓存")
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)
    return target


def extract_source(archive, source, destination):
    """Only the fixed production source/include trees and its MIT notice."""
    prefix = source["archiveRoot"] + "/"
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            if not member.filename.startswith(prefix) or member.is_dir():
                continue
            relative = member.filename[len(prefix):]
            if relative != "LICENSE.MIT" and not relative.startswith(
                    ("tawcroot/src/", "tawcroot/include/")):
                continue
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as incoming, path.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    return destination / "tawcroot"


def toolchain(ndk, source):
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("本原型构建入口需要 Linux x86_64 host")
    ndk = Path(ndk).resolve()
    properties = {}
    for line in (ndk / "source.properties").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key.strip()] = value.strip()
    if properties.get("Pkg.Revision") != source["ndkVersion"]:
        raise ValueError("需要现有 NDK " + source["ndkVersion"] + "；不自动选择或安装其他版本")
    binary = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin"
    names = ["clang", "ld.lld", "llvm-strip"] + [
        f"{arch}-linux-android{source['androidApi']}-clang"
        for arch, _, _ in ABIS.values()
    ]
    for name in names:
        if not os.access(binary / name, os.X_OK):
            raise ValueError("缺少已有 NDK 工具：" + str(binary / name))
    return binary


def recipe(source, abi):
    arch, _, base = ABIS[abi]
    compile_flags = COMPILE_FLAGS + (["-mno-outline-atomics"] if arch == "aarch64" else [])
    link_flags = LINK_FLAGS + [f"-Wl,--image-base={hex(base)}", "-Wl,-soname,libtawcroot.so"]
    return {
        "compiler": f"{arch}-linux-android{source['androidApi']}-clang",
        "compile": compile_flags,
        "link": link_flags,
        "sources": source["cSources"] + [
            f"src/arch/{arch}_stub.S", f"src/arch/{arch}_loader_jump.S",
        ],
    }


def audit_artifact(path, abi):
    data = path.read_bytes()
    elf = Elf(data)
    _, machine, base = ABIS[abi]
    if elf.bits != 64 or elf.type != 2 or elf.machine != machine:
        raise ValueError(abi + "：需要对应架构的 64-bit ET_EXEC")
    interpreter = elf.interp()
    needed = elf.dyn_strings()["needed"]
    if interpreter is not None or needed:
        raise ValueError(abi + "：产物必须无 PT_INTERP 和 DT_NEEDED")
    loads = [segment for segment in elf.phdrs if segment["type"] == 1]
    if not loads or any(segment["align"] < 16384 or
                        segment["align"] % 16384 or
                        (segment["vaddr"] - segment["offset"]) % 16384
                        for segment in loads):
        raise ValueError(abi + "：PT_LOAD 不是 16 KiB 对齐")
    entry = struct.unpack_from("<Q", data, 24)[0]
    if not base <= entry <= base + 0x100000:
        raise ValueError(abi + "：入口不在上游固定基址附近")
    return {
        "abi": abi, "path": f"jniLibs/{abi}/libtawcroot.so",
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
        "elf": {"type": elf.type, "machine": elf.machine,
                "interpreter": interpreter, "loadAlignment": elf.min_load_align(),
                "needed": needed, "entry": entry},
    }


def publish(staged, output, previous):
    """One writer; retain the last complete OUT if replacement fails."""
    try:
        if output.exists():
            os.replace(output, previous)
        os.replace(staged, output)
    except BaseException:
        # Cancellation may arrive after a rename, before its caller resumes.
        if previous.exists() and not output.exists():
            os.replace(previous, output)
        raise


def build(runtime, ndk, supplied=None, offline=False):
    prototype = runtime / "prototypes/tawcroot"
    source = json.loads((prototype / "source.json").read_text())
    binary = toolchain(ndk, source)
    archive = cached_archive(source, runtime / ".cache/tawcroot", supplied, offline)
    work = runtime / ".work/tawcroot-prototype"
    work.mkdir(parents=True, exist_ok=True)
    output = work / "out"
    with tempfile.TemporaryDirectory(prefix="build-", dir=work) as scratch:
        scratch = Path(scratch)
        src = extract_source(archive, source, scratch / "source")
        staged = scratch / "out"
        flags, artifacts = {}, []
        for abi in ABIS:
            selected = recipe(source, abi)
            flags[abi] = selected
            objects = scratch / "objects" / abi
            objects.mkdir(parents=True)
            object_paths = []
            for name in selected["sources"]:
                obj = objects / (Path(name).name + ".o")
                subprocess.run([str(binary / selected["compiler"]), *selected["compile"],
                                "-c", name, "-o", str(obj)], cwd=src, check=True)
                object_paths.append(str(obj))
            artifact = staged / "jniLibs" / abi / "libtawcroot.so"
            artifact.parent.mkdir(parents=True)
            subprocess.run([str(binary / selected["compiler"]), *selected["link"],
                            *object_paths, "-o", str(artifact)], cwd=src, check=True)
            subprocess.run([str(binary / "llvm-strip"), str(artifact)], check=True)
            artifacts.append(audit_artifact(artifact, abi))
        versions = {}
        for field, name in (("clang", "clang"), ("linker", "ld.lld")):
            versions[field] = subprocess.run(
                [str(binary / name), "--version"], check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            ).stdout.strip()
        manifest = {
            "schema": 1,
            "source": {key: source[key] for key in ("repo", "commit", "archiveSha256")},
            "ndkVersion": source["ndkVersion"], **versions,
            "flags": flags, "artifacts": artifacts,
        }
        encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        assets = staged / "assets/tawcroot-prototype"
        assets.mkdir(parents=True)
        (staged / "manifest.json").write_bytes(encoded)
        (assets / "manifest.json").write_bytes(encoded)
        shutil.copyfile(scratch / "source/LICENSE.MIT", assets / "LICENSE.MIT")
        shutil.copyfile(prototype / "THIRD_PARTY_NOTICES.draft.md",
                        assets / "THIRD_PARTY_NOTICES.draft.md")
        publish(staged, output, scratch / "previous-out")
    return output


def main():
    parser = argparse.ArgumentParser(description="tawcroot 隔离构建原型（不合入）")
    parser.add_argument("--ndk", default=os.environ.get("ANDROID_NDK_HOME"),
                        help="已有 NDK 28.2.13676358；也可设置 ANDROID_NDK_HOME")
    parser.add_argument("--archive", type=Path, help="导入固定 SHA256 的离线源码 ZIP")
    parser.add_argument("--offline", action="store_true", help="禁止联网，只读缓存或 --archive")
    args = parser.parse_args()
    if not args.ndk:
        parser.error("必须指定 --ndk 或 ANDROID_NDK_HOME；不会安装 NDK")
    try:
        output = build(RUNTIME, args.ndk, args.archive, args.offline)
    except (OSError, ValueError, struct.error, zipfile.BadZipFile,
            subprocess.CalledProcessError) as error:
        print("tawcroot 原型构建失败：" + str(error), file=sys.stderr)
        return 1
    print("tawcroot 原型输出：" + str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
