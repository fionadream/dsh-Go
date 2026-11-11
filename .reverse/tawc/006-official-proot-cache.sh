#!/usr/bin/env bash
# request-006 only: recover the original pinned bytes from the official run.
# Run on the owner's Linux CI runner, never as a production build dependency.
set -euo pipefail
repo="${1:?usage: bash 006-official-proot-cache.sh REPOSITORY_ROOT}"
: "${GH_TOKEN:?Set GH_TOKEN to the runner github.token without printing it}"
proot_run=39199950210
proot_commit=9aedff9c28af3ca93c42eb2c3074221893232ae9
proot_tmp="$(mktemp -d)"
trap 'rm -rf "$proot_tmp"' EXIT
for proot_abi in aarch64 x86_64; do
  gh run download "$proot_run" --repo termux/termux-packages \
    --name "debs-$proot_abi-$proot_commit" --dir "$proot_tmp/$proot_abi"
done
python3 -I - "$repo" "$proot_tmp" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
import tarfile

repo, downloaded = map(Path, sys.argv[1:])
commit = "9aedff9c28af3ca93c42eb2c3074221893232ae9"
expected = (
    ("aarch64", 101288, "29de40efea4daa2a6168b617915206511c63e8d46bf5f4b07d48723441ef7f3b"),
    ("x86_64", 111780, "1c31ad7e1046f4d418fef19b6dfaa9a14801d2047ade6ad0375b3f5501e3e227"),
)
cache = repo / "runtime/.cache/termux"
cache.mkdir(parents=True, exist_ok=True)
for abi, size, digest in expected:
    name = f"proot_5.1.107.98_{abi}.deb"
    archive = downloaded / abi / f"debs-{abi}-{commit}.tar"
    with tarfile.open(archive, "r:") as tar:
        member = tar.getmember(f"debs/{name}")
        if not member.isfile() or member.size != size:
            raise SystemExit(f"unexpected official member: {name}")
        data = tar.extractfile(member).read()
    actual = hashlib.sha256(data).hexdigest()
    if len(data) != size or actual != digest:
        raise SystemExit(f"original pin mismatch: {name}")
    partial = cache / f"{name}.request006.part"
    try:
        partial.write_bytes(data)
        partial.replace(cache / name)
    finally:
        partial.unlink(missing_ok=True)
    print(json.dumps({"name": name, "bytes": size, "sha256": actual,
                      "source": f"termux/termux-packages run 39199950210 debs-{abi}-{commit}"}))
PY
