"""Multi-account switcher for Claude Code."""

from importlib.metadata import version

from claude_swap.distribution import DIST_NAME

__version__ = version(DIST_NAME)

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
