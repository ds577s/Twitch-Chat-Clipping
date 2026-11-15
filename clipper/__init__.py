"""Chat Spike Clipper: find clip-worthy Twitch moments from chat activity."""

__version__ = "0.1.0"


class ClipperError(Exception):
    """An error with a message meant for the user (no traceback needed)."""
