"""Exception types. Every one carries a message safe to show to the model."""


class SlurmMcpError(Exception):
    """Base class for errors reported back to the caller."""


class ConfigError(SlurmMcpError):
    """The configuration file is missing or invalid."""


class PathError(SlurmMcpError):
    """A remote path was rejected by validation."""


class RemoteError(SlurmMcpError):
    """The remote command or the SSH connection failed."""


class RemoteFileNotFound(RemoteError):
    """A file that was asked for does not exist on the cluster."""
