#!/usr/bin/env python3
"""Verify libfabric backport .deb files have correct contents and metadata.

Validates the runtime and dev packages produced by .github/workflows/build.yml:
  - control fields (Package, Version, Architecture, Replaces, Depends pin)
  - file partitioning (no headers in runtime, no /usr/bin in dev, no .la in either)

Usage:
    verify_deb.py \\
        --runtime libfabric_2.5.1-1~jammy1_arm64.deb \\
        --dev libfabric-dev_2.5.1-1~jammy1_arm64.deb \\
        --tag v2.5.1 \\
        --codename jammy \\
        --arch arm64

Requires `ar` and `tar` (no dpkg-deb dependency, so it runs on macOS too).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def split_deb(deb: Path, into: Path) -> tuple[Path, Path]:
    """Extract control.tar.* and data.tar.* from a .deb (an ar archive)."""
    into.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ar", "x", str(deb.resolve())], cwd=str(into), check=True)
    members = list(into.iterdir())
    control = next(p for p in members if p.name.startswith("control.tar"))
    data = next(p for p in members if p.name.startswith("data.tar"))
    return control, data


def list_tar(path: Path) -> list[str]:
    out = subprocess.check_output(["tar", "-tf", str(path)], text=True)
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


def inspect(deb: Path) -> tuple[dict[str, str], list[str]]:
    work = Path(tempfile.mkdtemp(prefix="deb-inspect-"))
    try:
        control_tar, data_tar = split_deb(deb, work)
        return read_control(control_tar), list_tar(data_tar)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check(cond: bool, msg: str, errs: list[str]) -> None:
    if not cond:
        errs.append(msg)


def verify_runtime(deb: Path, version: str, codename: str, arch: str) -> list[str]:
    fields, files = inspect(deb)
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
    fields, files = inspect(deb)
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
    ap.add_argument("--runtime", required=True, type=Path,
                    help="path to libfabric runtime .deb")
    ap.add_argument("--dev", required=True, type=Path,
                    help="path to libfabric-dev .deb")
    ap.add_argument("--tag", required=True,
                    help="upstream libfabric tag (e.g. v2.5.1)")
    ap.add_argument("--codename", required=True,
                    help="Ubuntu codename (jammy or noble)")
    ap.add_argument("--arch", required=True,
                    help="package architecture (amd64 or arm64)")
    args = ap.parse_args()

    for tool in ("ar", "tar"):
        if shutil.which(tool) is None:
            sys.exit(f"required tool not found in PATH: {tool}")
    for label, p in (("runtime", args.runtime), ("dev", args.dev)):
        if not p.is_file():
            sys.exit(f"--{label} not a file: {p}")

    version = args.tag.lstrip("v")
    errs: list[str] = []
    errs += [f"runtime ({args.runtime.name}): {e}"
             for e in verify_runtime(args.runtime, version, args.codename, args.arch)]
    errs += [f"dev     ({args.dev.name}): {e}"
             for e in verify_dev(args.dev, version, args.codename, args.arch)]

    if errs:
        print("FAIL", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"OK  {args.runtime.name}")
    print(f"OK  {args.dev.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
