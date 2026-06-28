"""ai guest list — engine for switching between Codex/Claude accounts on usage limits.

The engine is intentionally stdlib-only so it can be vendored as a single directory and run
without a virtualenv. The menubar app (``app/``) is a thin UI layer on top of this package.
"""

__version__ = "0.1.0"
APP_NAME = "ai guest list"

# Tools this engine knows how to manage.
TOOLS = ("codex", "claude")
