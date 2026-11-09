"""Shared exceptions for simp-vmgen."""

from __future__ import annotations


class SimpVmgenError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(SimpVmgenError):
    """Invalid or unreadable project configuration."""


class DistroError(SimpVmgenError):
    """Unknown distro or invalid distro metadata."""


class ProjectError(SimpVmgenError):
    """Project creation, discovery or regeneration failure."""


class CacheError(SimpVmgenError):
    """Image or artifact download/cache failure."""


class LockError(SimpVmgenError):
    """Invalid or unwritable vmgen.lock."""


class ComposeError(SimpVmgenError):
    """Invalid compose project, state or network allocation."""
