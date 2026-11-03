"""Built-in rootfs plugins."""

from .base import RootfsSpec, available_rootfs, load_rootfs, load_rootfs_from_path

__all__ = [
    "RootfsSpec",
    "available_rootfs",
    "load_rootfs",
    "load_rootfs_from_path",
]
