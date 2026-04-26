#!/usr/bin/env python3
"""Run the libfabric build workflow locally with `act` and verify the .debs.

Slims the build matrix to a single target, runs `act --job build`, then
inspects the two produced .debs (libfabric, libfabric-dev) for correct
partitioning and control-field metadata.

Usage:
    scripts/test_build.py                       # default: tag v2.5.1, jammy-arm64
    scripts/test_build.py --tag v2.5.0
    scripts/test_build.py --target noble-amd64
    scripts/test_build.py --keep                # keep extracted .debs

Requires: act, docker, ar, tar (no dpkg-deb dependency).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

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

# The multi-line `target:` block inside the build job's matrix.
TARGET_BLOCK_RE = re.compile(
    r"^        target:\n(?:          - \{[^}]+\}\n)+",
    re.MULTILINE,
)


def slim_matrix(text: str, t: dict) -> str:
    repl = (
        "        target:\n"
        f"          - {{ os: {t['os']}, codename: {t['codename']}, arch: {t['arch']} }}\n"
    )
    new, n = TARGET_BLOCK_RE.subn(repl, text, count=1)
    if n != 1:
        sys.exit(f"failed to patch matrix.target in {WORKFLOW} (matched {n} times)")
    return new


def run_act(tag: str, target: dict, image: str, artifact_dir: Path) -> int:
    cmd = [
        "act", "workflow_dispatch",
        "-W", str(WORKFLOW),
        "--input", f"tag={tag}",
        "--input", "force=true",
        "--job", "build",
        "-P", f"{target['os']}={image}",
        "-P", "ubuntu-latest=catthehacker/ubuntu:act-22.04",
        "--artifact-server-path", str(artifact_dir),
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


def split_deb(deb: Path, into: Path) -> tuple[Path, Path]:
    """Extract control.tar.* and data.tar.* from a .deb (ar archive)."""
    into.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ar", "x", str(deb.resolve())], cwd=str(into), check=True)
    members = list(into.iterdir())
    control = next(p for p in members if p.name.startswith("control.tar"))
    data = next(p for p in members if p.name.startswith("data.tar"))
    return control, data


def list_tar(path: Path) -> list[str]:
    out = subprocess.check_output(["tar", "-tf", str(path)], text=True)
    # Normalize: drop leading "./", strip trailing "/" on dirs, ignore blanks.
    paths = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("./"):
            s = s[2:]
        paths.append(s.rstrip("/"))
    return paths


def read_control(control_tar: Path) -> dict[str, str]:
    out = subprocess.check_output(
        ["tar", "-xOf", str(control_tar), "./control"], text=True
    )
    fields: dict[str, str] = {}
    for line in out.splitlines():
        if not line or line.startswith((" ", "\t")):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields


def check(cond: bool, msg: str, errs: list[str]) -> None:
    if not cond:
        errs.append(msg)


def verify_runtime(deb: Path, version: str, codename: str, arch: str) -> list[str]:
    work = Path(tempfile.mkdtemp(prefix="deb-rt-"))
    try:
        control_tar, data_tar = split_deb(deb, work)
        fields = read_control(control_tar)
        files = list_tar(data_tar)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    errs: list[str] = []
    expected_version = f"{version}-1~{codename}1"

    check(fields.get("Package") == "libfabric",
          f"Package={fields.get('Package')!r}, want 'libfabric'", errs)
    check(fields.get("Version") == expected_version,
          f"Version={fields.get('Version')!r}, want {expected_version!r}", errs)
    check(fields.get("Architecture") == arch,
          f"Architecture={fields.get('Architecture')!r}, want {arch!r}", errs)
    check("libfabric1" in fields.get("Replaces", ""),
          "Replaces missing 'libfabric1'", errs)
    check("libfabric-dev" not in fields.get("Replaces", ""),
          "Replaces should not include 'libfabric-dev' (dev package owns it)", errs)

    has_so_real = any(re.fullmatch(r"usr/lib/libfabric\.so\.\d+\.\d+\.\d+", p) for p in files)
    has_so_soname = any(re.fullmatch(r"usr/lib/libfabric\.so\.\d+", p) for p in files)
    has_unversioned = "usr/lib/libfabric.so" in files
    has_pc = any(p.endswith("/pkgconfig/libfabric.pc") for p in files)
    has_header = any(p.startswith("usr/include/") for p in files)
    has_la = any(p.endswith(".la") for p in files)
    has_fi_info = "usr/bin/fi_info" in files
    has_man1 = any(p.startswith("usr/share/man/man1/") for p in files)

    check(has_so_real,    "missing real libfabric.so.X.Y.Z", errs)
    check(has_so_soname,  "missing SONAME libfabric.so.N", errs)
    check(has_fi_info,    "missing /usr/bin/fi_info", errs)
    check(has_man1,       "missing man1 pages", errs)
    check(not has_unversioned, "should not contain unversioned libfabric.so", errs)
    check(not has_pc,     "should not contain pkgconfig/.pc", errs)
    check(not has_header, "should not contain /usr/include headers", errs)
    check(not has_la,     "should not contain .la libtool archives", errs)
    return errs


def verify_dev(deb: Path, version: str, codename: str, arch: str) -> list[str]:
    work = Path(tempfile.mkdtemp(prefix="deb-dev-"))
    try:
        control_tar, data_tar = split_deb(deb, work)
        fields = read_control(control_tar)
        files = list_tar(data_tar)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    errs: list[str] = []
    expected_version = f"{version}-1~{codename}1"
    expected_dep = f"libfabric (= {expected_version})"

    check(fields.get("Package") == "libfabric-dev",
          f"Package={fields.get('Package')!r}, want 'libfabric-dev'", errs)
    check(fields.get("Version") == expected_version,
          f"Version={fields.get('Version')!r}, want {expected_version!r}", errs)
    check(fields.get("Architecture") == arch,
          f"Architecture={fields.get('Architecture')!r}, want {arch!r}", errs)
    check(fields.get("Depends") == expected_dep,
          f"Depends={fields.get('Depends')!r}, want {expected_dep!r}", errs)

    has_unversioned = "usr/lib/libfabric.so" in files
    has_pc = any(p.endswith("/pkgconfig/libfabric.pc") for p in files)
    has_headers = any(p.startswith("usr/include/rdma/") for p in files)
    has_so_real = any(re.fullmatch(r"usr/lib/libfabric\.so\.\d+\.\d+\.\d+", p) for p in files)
    has_bin = any(p.startswith("usr/bin/") for p in files)
    has_la = any(p.endswith(".la") for p in files)

    check(has_unversioned, "missing unversioned libfabric.so symlink", errs)
    check(has_pc,          "missing pkgconfig/libfabric.pc", errs)
    check(has_headers,     "missing usr/include/rdma headers", errs)
    check(not has_so_real, "should not contain real libfabric.so.X.Y.Z", errs)
    check(not has_bin,     "should not contain /usr/bin/* utilities", errs)
    check(not has_la,      "should not contain .la libtool archives", errs)
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--tag", default="v2.5.1", help="upstream libfabric tag (default: %(default)s)")
    ap.add_argument("--target", default="jammy-arm64", choices=sorted(TARGETS),
                    help="matrix target to test (default: %(default)s)")
    ap.add_argument("--keep", action="store_true",
                    help="keep act artifacts and extracted .debs on exit")
    args = ap.parse_args()

    for tool in ("act", "docker", "ar", "tar"):
        if shutil.which(tool) is None:
            sys.exit(f"required tool not found in PATH: {tool}")

    target = TARGETS[args.target]
    image = IMAGES[target["codename"]]
    version = args.tag.lstrip("v")

    original_workflow = WORKFLOW.read_text()
    artifact_dir = Path(tempfile.mkdtemp(prefix="act-test-"))
    extract_dir = Path(tempfile.mkdtemp(prefix="debs-"))

    try:
        WORKFLOW.write_text(slim_matrix(original_workflow, target))
        rc = run_act(args.tag, target, image, artifact_dir)
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

        errs: list[str] = []
        errs += [f"runtime: {e}" for e in verify_runtime(runtime, version, target["codename"], target["arch"])]
        errs += [f"dev:     {e}" for e in verify_dev(dev, version, target["codename"], target["arch"])]

        print()
        if errs:
            print("FAIL", file=sys.stderr)
            for e in errs:
                print(f"  - {e}", file=sys.stderr)
            return 1
        print(f"OK  {runtime.name}")
        print(f"OK  {dev.name}")
        return 0
    finally:
        WORKFLOW.write_text(original_workflow)
        if args.keep:
            print(f"\nartifact dir: {artifact_dir}", file=sys.stderr)
            print(f"extracted:    {extract_dir}", file=sys.stderr)
        else:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            shutil.rmtree(extract_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
