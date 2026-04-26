#!/usr/bin/env python3
"""Run the libfabric build workflow locally with `act` and verify the .debs.

Drives the workflow with `workflow_dispatch` inputs (`tag`, `target`, `force`)
to build a single (tag, codename, arch) combination, then delegates to
scripts/verify_deb.py to validate the produced .debs.

Usage:
    scripts/test_build.py                       # latest tag, jammy-<host arch>
    scripts/test_build.py --tag v2.5.0
    scripts/test_build.py --target noble-amd64
    scripts/test_build.py --keep                # keep extracted .debs

Requires: act, docker, ar, tar (no dpkg-deb dependency).
"""
from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

UPSTREAM = "https://github.com/ofiwg/libfabric.git"

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "build.yml"

TARGETS = {
    "jammy-amd64": {"os": "ubuntu-22.04",     "codename": "jammy", "arch": "amd64"},
    "jammy-arm64": {"os": "ubuntu-22.04-arm", "codename": "jammy", "arch": "arm64"},
    "noble-amd64": {"os": "ubuntu-24.04",     "codename": "noble", "arch": "amd64"},
    "noble-arm64": {"os": "ubuntu-24.04-arm", "codename": "noble", "arch": "arm64"},
}

IMAGES = {
    "jammy": "catthehacker/ubuntu:act-22.04",
    "noble": "catthehacker/ubuntu:act-24.04",
}


def host_arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    sys.exit(f"unrecognized host architecture: {m!r}")


def latest_upstream_tag() -> str:
    out = subprocess.check_output(
        ["git", "ls-remote", "--tags", "--refs", UPSTREAM], text=True,
    )
    tags = []
    for line in out.splitlines():
        m = re.search(r"refs/tags/(v\d+\.\d+\.\d+)$", line)
        if m:
            tags.append(m.group(1))
    if not tags:
        sys.exit("no stable upstream tags found")
    tags.sort(key=lambda t: tuple(int(p) for p in t.lstrip("v").split(".")))
    return tags[-1]


def run_act(tag: str, target_name: str, target: dict, image: str, artifact_dir: Path) -> int:
    cmd = [
        "act", "workflow_dispatch",
        "-W", str(WORKFLOW),
        "--input", f"tag={tag}",
        "--input", "force=true",
        "--input", f"target={target_name}",
        "--job", "build",
        "-P", f"{target['os']}={image}",
        "-P", "ubuntu-latest=catthehacker/ubuntu:act-22.04",
        "--artifact-server-path", str(artifact_dir),
        "--bind",
        "--rm",
    ]
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(REPO)).returncode


def collect_debs(artifact_dir: Path, extract_to: Path) -> list[Path]:
    debs: list[Path] = []
    for z in sorted(artifact_dir.rglob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                if not name.endswith(".deb"):
                    continue
                dst = extract_to / Path(name).name
                with zf.open(name) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
                debs.append(dst)
    return debs


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--tag", default=None,
                    help="upstream libfabric tag (default: latest stable upstream tag)")
    ap.add_argument("--target", default=None,
                    help=f"matrix target: one of {sorted(TARGETS)} "
                         "(default: jammy-<host_arch>)")
    ap.add_argument("--keep", action="store_true",
                    help="keep act artifacts and extracted .debs on exit")
    args = ap.parse_args()

    for tool in ("act", "docker", "ar", "tar", "git"):
        if shutil.which(tool) is None:
            sys.exit(f"required tool not found in PATH: {tool}")

    tag = args.tag or latest_upstream_tag()
    target_name = args.target or f"jammy-{host_arch()}"
    if target_name not in TARGETS:
        sys.exit(f"unknown target {target_name!r}; choose from {sorted(TARGETS)}")
    target = TARGETS[target_name]
    image = IMAGES[target["codename"]]
    print(f"tag={tag} target={target_name}")

    artifact_dir = Path(tempfile.mkdtemp(prefix="act-test-"))
    extract_dir = Path(tempfile.mkdtemp(prefix="debs-"))

    try:
        rc = run_act(tag, target_name, target, image, artifact_dir)
        if rc != 0:
            print(f"\nact exited {rc}", file=sys.stderr)
            return rc

        debs = collect_debs(artifact_dir, extract_dir)
        by_pkg = {d.name.split("_", 1)[0]: d for d in debs}
        if "libfabric" not in by_pkg or "libfabric-dev" not in by_pkg:
            print("ERROR: expected both libfabric and libfabric-dev .debs", file=sys.stderr)
            for d in debs:
                print(f"  found: {d.name}", file=sys.stderr)
            return 1

        runtime = by_pkg["libfabric"]
        dev = by_pkg["libfabric-dev"]
        verify_script = REPO / "scripts" / "verify_deb.py"

        print()
        rc = subprocess.run([
            sys.executable, str(verify_script),
            "--runtime", str(runtime),
            "--dev", str(dev),
            "--tag", tag,
            "--codename", target["codename"],
            "--arch", target["arch"],
        ]).returncode

        if rc == 0:
            for leftover in ("pkg-runtime", "pkg-dev"):
                shutil.rmtree(REPO / leftover, ignore_errors=True)
        return rc
    finally:
        if args.keep:
            print(f"\nartifact dir: {artifact_dir}", file=sys.stderr)
            print(f"extracted:    {extract_dir}", file=sys.stderr)
        else:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            shutil.rmtree(extract_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
