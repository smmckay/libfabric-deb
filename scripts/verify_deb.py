#!/usr/bin/env python3
"""Verify libfabric backport .deb files have correct contents and metadata.

Validates the runtime, dev, and dbgsym packages produced by
.github/workflows/build.yml:
  - control fields (Package, Version, Architecture, Replaces, Depends pin)
  - file partitioning (no headers in runtime, no /usr/bin in dev, no .la in either)
  - split debuginfo: runtime ELFs carry .gnu_debuglink and no .debug_* sections;
    matching .debug companions live in the dbgsym package under /usr/lib/debug/.

Usage:
    verify_deb.py \\
        --runtime libfabric_2.5.1-1~jammy1_arm64.deb \\
        --dev libfabric-dev_2.5.1-1~jammy1_arm64.deb \\
        --dbgsym libfabric-dbgsym_2.5.1-1~jammy1_arm64.deb \\
        --tag v2.5.1 \\
        --codename jammy \\
        --arch arm64

Requires `ar` and `tar` (no dpkg-deb dependency, so it runs on macOS too).
"""
from __future__ import annotations

import argparse
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
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


@contextmanager
def open_deb(deb: Path):
    """Yield (control_fields, file_list, read_member) for a .deb.

    read_member(name) returns the raw bytes of an entry in the data tarball.
    """
    work = Path(tempfile.mkdtemp(prefix="deb-inspect-"))
    try:
        control_tar, data_tar = split_deb(deb, work)
        fields = read_control(control_tar)
        files = list_tar(data_tar)

        def read_member(name: str) -> bytes:
            return subprocess.check_output(
                ["tar", "-xOf", str(data_tar), f"./{name}"]
            )

        yield fields, files, read_member
    finally:
        shutil.rmtree(work, ignore_errors=True)


def elf_section_names(data: bytes) -> list[str]:
    """Return the section names of an ELF blob, or [] if not a valid ELF."""
    if len(data) < 0x40 or data[:4] != b"\x7fELF":
        return []
    cls = data[4]
    endian = "<" if data[5] == 1 else ">"
    if cls == 2:  # ELF64
        e_shoff = struct.unpack_from(endian + "Q", data, 0x28)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
            endian + "HHH", data, 0x3a)
        sh_off_field, sh_size_field, off_fmt = 0x18, 0x20, endian + "Q"
    elif cls == 1:  # ELF32
        e_shoff = struct.unpack_from(endian + "I", data, 0x20)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
            endian + "HHH", data, 0x2e)
        sh_off_field, sh_size_field, off_fmt = 0x10, 0x14, endian + "I"
    else:
        return []

    str_hdr = e_shoff + e_shstrndx * e_shentsize
    sh_off = struct.unpack_from(off_fmt, data, str_hdr + sh_off_field)[0]
    sh_size = struct.unpack_from(off_fmt, data, str_hdr + sh_size_field)[0]
    strtab = data[sh_off:sh_off + sh_size]

    names = []
    for i in range(e_shnum):
        hdr = e_shoff + i * e_shentsize
        name_off = struct.unpack_from(endian + "I", data, hdr)[0]
        end = strtab.find(b"\x00", name_off)
        names.append(strtab[name_off:end].decode("utf-8", "replace"))
    return names


def check(cond: bool, msg: str, errs: list[str]) -> None:
    if not cond:
        errs.append(msg)


def verify_runtime(deb: Path, version: str, codename: str, arch: str) -> list[str]:
    errs: list[str] = []
    expected_version = f"{version}-1~{codename}1"

    with open_deb(deb) as (fields, files, read_member):
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
        has_debug = any(p.startswith("usr/lib/debug/") for p in files)

        check(has_so_real,    "missing real libfabric.so.X.Y.Z", errs)
        check(has_so_soname,  "missing SONAME libfabric.so.N", errs)
        check(has_fi_info,    "missing /usr/bin/fi_info", errs)
        check(has_man1,       "missing man1 pages", errs)
        check(not has_unversioned, "should not contain unversioned libfabric.so", errs)
        check(not has_pc,     "should not contain pkgconfig/.pc", errs)
        check(not has_header, "should not contain /usr/include headers", errs)
        check(not has_la,     "should not contain .la libtool archives", errs)
        check(not has_debug,  "should not contain /usr/lib/debug (belongs in dbgsym)", errs)

        # ELF checks: split debuginfo must leave .gnu_debuglink and remove .debug_*.
        elf_targets = [p for p in files
                       if p == "usr/bin/fi_info"
                       or re.fullmatch(r"usr/lib/libfabric\.so\.\d+\.\d+\.\d+", p)]
        for path in elf_targets:
            sections = elf_section_names(read_member(path))
            if not sections:
                errs.append(f"{path}: not a valid ELF")
                continue
            if ".gnu_debuglink" not in sections:
                errs.append(f"{path}: missing .gnu_debuglink (debuginfo not split)")
            stale = [s for s in sections if s.startswith(".debug_")]
            if stale:
                errs.append(f"{path}: still carries debug sections {stale}")
    return errs


def verify_dev(deb: Path, version: str, codename: str, arch: str) -> list[str]:
    errs: list[str] = []
    expected_version = f"{version}-1~{codename}1"
    expected_dep = f"libfabric (= {expected_version})"

    with open_deb(deb) as (fields, files, _read):
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


def verify_dbgsym(deb: Path, version: str, codename: str, arch: str) -> list[str]:
    errs: list[str] = []
    expected_version = f"{version}-1~{codename}1"
    expected_dep = f"libfabric (= {expected_version})"

    with open_deb(deb) as (fields, files, read_member):
        check(fields.get("Package") == "libfabric-dbgsym",
              f"Package={fields.get('Package')!r}, want 'libfabric-dbgsym'", errs)
        check(fields.get("Version") == expected_version,
              f"Version={fields.get('Version')!r}, want {expected_version!r}", errs)
        check(fields.get("Architecture") == arch,
              f"Architecture={fields.get('Architecture')!r}, want {arch!r}", errs)
        check(fields.get("Depends") == expected_dep,
              f"Depends={fields.get('Depends')!r}, want {expected_dep!r}", errs)
        check(fields.get("Section") == "debug",
              f"Section={fields.get('Section')!r}, want 'debug'", errs)

        has_fi_info_dbg = "usr/lib/debug/usr/bin/fi_info.debug" in files
        has_so_dbg = any(re.fullmatch(
            r"usr/lib/debug/usr/lib/libfabric\.so\.\d+\.\d+\.\d+\.debug", p)
                         for p in files)
        check(has_fi_info_dbg, "missing usr/lib/debug/usr/bin/fi_info.debug", errs)
        check(has_so_dbg,
              "missing usr/lib/debug/usr/lib/libfabric.so.X.Y.Z.debug", errs)

        # Anything not under /usr/lib/debug/ (or an intermediate dir on the way
        # there) is a payload that doesn't belong in a dbgsym package.
        allowed_dirs = {"usr", "usr/lib"}
        stray = [p for p in files
                 if p not in allowed_dirs and not p.startswith("usr/lib/debug")]
        check(not stray, f"unexpected non-debug payload: {stray[:3]}", errs)

        # Each .debug companion should be a valid ELF carrying .debug_info.
        for path in files:
            if not path.endswith(".debug"):
                continue
            sections = elf_section_names(read_member(path))
            if not sections:
                errs.append(f"{path}: not a valid ELF")
                continue
            if ".debug_info" not in sections:
                errs.append(f"{path}: missing .debug_info section")
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
    ap.add_argument("--dbgsym", required=True, type=Path,
                    help="path to libfabric-dbgsym .deb")
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
    for label, p in (("runtime", args.runtime),
                     ("dev", args.dev),
                     ("dbgsym", args.dbgsym)):
        if not p.is_file():
            sys.exit(f"--{label} not a file: {p}")

    version = args.tag.lstrip("v")
    errs: list[str] = []
    errs += [f"runtime ({args.runtime.name}): {e}"
             for e in verify_runtime(args.runtime, version, args.codename, args.arch)]
    errs += [f"dev     ({args.dev.name}): {e}"
             for e in verify_dev(args.dev, version, args.codename, args.arch)]
    errs += [f"dbgsym  ({args.dbgsym.name}): {e}"
             for e in verify_dbgsym(args.dbgsym, version, args.codename, args.arch)]

    if errs:
        print("FAIL", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"OK  {args.runtime.name}")
    print(f"OK  {args.dev.name}")
    print(f"OK  {args.dbgsym.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
