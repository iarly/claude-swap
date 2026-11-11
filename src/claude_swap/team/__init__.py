"""Team accounts shared through Azure Key Vault, with presence in Table Storage.

A team lives in one Key Vault (the vault is the security boundary: Key Vault
has no per-prefix RBAC). The vault holds a manifest secret ``cswap-<team>``
naming the presence table, and one secret per account,
``cswap-<team>--<slug>``, carrying a setup-token or API key. Those never
rotate, so every member can hold a copy.

Only the Azure-specific module (``azure_backend``) imports the Azure SDK, and
only when a team command actually runs. Everything else talks to the
``SecretStore`` / ``PresenceStore`` protocols in ``stores``, which the tests
replace with in-memory fakes.
"""
