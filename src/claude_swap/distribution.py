"""The name this build is published under on PyPI.

This fork ships as ``cswap-teams`` (``claude-swap`` on PyPI belongs to the
original project). Only what refers to the *published package* uses this
name: the installed version, the update check, upgrade commands and install
hints. Everything that names stored data (the backup directory, the keychain
service, the logger, the Entra ID token cache) stays ``claude-swap``, so an
install of either package reads the same accounts.
"""

DIST_NAME = "cswap-teams"
