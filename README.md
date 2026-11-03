# simp-vmgen

Generate reproducible QEMU lab environments from a small TOML project file.

`simp-vmgen` is a thin, installable CLI. A generated project contains only
`vmgen.toml`, a `Makefile` that forwards to `simp-vmgen`, and a generated
`README.md`; all build and runtime logic lives in the installed tool.

## Status

Under active development. See [TODO.md](TODO.md) for the current progress.

## Install

```sh
uv tool install --editable .
# or, for local development:
uv run simp-vmgen --help
```

## CLI overview

```sh
simp-vmgen new lab-ebpf --distro ubuntu --release 24.04 \
    --mem 4G --cpus 4 -v work:/work
simp-vmgen new lab-kernel -i
simp-vmgen -C lab-ebpf build
simp-vmgen -C lab-ebpf run
simp-vmgen -C lab-ebpf tty
simp-vmgen diagnose
```

Planned distro support: Ubuntu 24.04 and Alpine 3.22. Planned kernel rootfs
support starts with a static busybox initramfs.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
```
