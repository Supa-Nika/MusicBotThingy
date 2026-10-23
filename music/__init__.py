"""Music module. Discord.py loads this package as an extension via setup()/teardown()."""
from .cog import setup, teardown

__all__ = ["setup", "teardown"]
