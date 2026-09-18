"""Exception types used across syswatch."""


class SyswatchError(Exception):
    """Base class for expected syswatch failures."""


class ConfigError(SyswatchError):
    """Raised when the configuration file is missing or invalid."""


class PlatformNotSupportedError(SyswatchError):
    """Raised when running on a platform other than Linux."""
