"""Default locations of kernel-provided virtual filesystems.

Collectors accept explicit roots as parameters (with these defaults) so tests
can point them at synthetic /proc or /sys trees.
"""

from pathlib import Path

PROC_ROOT = Path("/proc")
SYSFS_ROOT = Path("/sys")
ETC_ROOT = Path("/etc")
