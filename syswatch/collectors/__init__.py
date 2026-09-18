"""Collectors read system information from /proc, /sys and the OS.

Every public function degrades gracefully: when a source is missing,
unreadable or malformed it returns None / an empty structure instead of
raising, so callers can render partial results.
"""
