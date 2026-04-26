# libfabric-deb

Backported `.deb` packages of [libfabric](https://github.com/ofiwg/libfabric) for
Ubuntu 22.04 (jammy) and 24.04 (noble), built from upstream stable tags newer
than the version Ubuntu ships in its archive.

Each upstream stable release (`vX.Y.Z`) is built for **jammy** and **noble**, on
**amd64** and **arm64**, and published two ways:

- as assets on a [GitHub Release](../../releases) for that tag, and
- as packages in an apt repository hosted on GitHub Pages.

## Use as an apt repository

```sh
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://smmckay.github.io/libfabric-deb/pubkey.asc \
  | sudo tee /etc/apt/keyrings/libfabric-deb.asc >/dev/null

echo "deb [signed-by=/etc/apt/keyrings/libfabric-deb.asc] https://smmckay.github.io/libfabric-deb $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/libfabric-deb.list

sudo apt-get update
sudo apt-get install libfabric libfabric-dev
```

Supported `$(lsb_release -cs)` values: `jammy`, `noble`.

The packages declare `Provides`/`Replaces`/`Conflicts` against the Ubuntu
`libfabric1`, `libfabric-bin`, and `libfabric-dev` packages, so installing from
this repo cleanly supersedes the archive versions.

## Packages

- **`libfabric`** — runtime shared library, provider plugins, and CLI tools
  (`fi_info`, `fi_pingpong`, etc.). Depends on `librdmacm1`, `libibverbs1`,
  `libnuma1`.
- **`libfabric-dev`** — headers, pkg-config file, and the unversioned `.so`
  symlink. Depends on the matching `libfabric`.

Versions are stamped as `<upstream>-1~<codename>1` (e.g. `2.5.1-1~jammy1`) so
the same upstream release is distinguishable per Ubuntu codename.

## Direct `.deb` downloads

If you don't want the apt repo, grab the `.deb`s directly from a
[release](../../releases):

```sh
curl -sSLO https://github.com/smmckay/libfabric-deb/releases/download/v2.5.1/libfabric_2.5.1-1~jammy1_arm64.deb
sudo dpkg -i libfabric_*.deb
```

## How it's built

`.github/workflows/build.yml` runs hourly (and on demand). It:

1. Lists upstream stable tags from `ofiwg/libfabric`.
2. Filters out tags older than what Ubuntu already ships for each codename
   (via `apt-cache madison`) and tags that already have a release here.
3. For each remaining `(tag, codename, arch)`, builds libfabric from source on
   the matching Ubuntu runner, splits the install tree into runtime and `-dev`
   payloads, and produces two `.deb`s with `dpkg-deb --build`.
4. Publishes the `.deb`s as assets on a GitHub Release named after the tag.

`.github/workflows/publish-apt.yml` then assembles those release assets into a
signed apt repository under `apt-repo/` using `reprepro`, and deploys it to
GitHub Pages. The reprepro distribution config lives in
[`apt-repo/conf/distributions`](apt-repo/conf/distributions).

## This is not an official libfabric distribution

These packages are an unofficial backport for personal/lab use. Upstream
libfabric is at <https://github.com/ofiwg/libfabric>; report libfabric bugs
there. Report packaging issues here.
