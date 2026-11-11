# Team accounts through Azure Key Vault

A team shares setup-tokens and API keys through an Azure Key Vault, with access controlled by Entra ID, and sees who is on which account through an Azure Table Storage table. Only `add-token` accounts can be shared: a normal `/login` rotates its refresh token, so two copies of it log each other out. A setup-token (`claude setup-token`) or an API key never rotates, so every member can hold a copy.

## What to create in Azure

1. **One Key Vault per team**, with the Azure RBAC permission model (`az keyvault create --enable-rbac-authorization`). The vault is the security boundary: Key Vault has no per-prefix permissions, and listing secrets, which members need, is a vault-wide right. Anyone who can read the vault can read every account in it.
2. **A Storage account with the Table service** for presence. Several teams can share one; each team gets its own partition.
3. **Role assignments**, best given to an Entra ID group:

| Who | Role | Scope |
|-----|------|-------|
| Admins (add and remove accounts, register the team) | Key Vault Secrets Officer | the vault |
| Members | Key Vault Secrets User | the vault |
| Everyone | Storage Table Data Contributor | the storage account (or the table) |

cswap never grants roles.

## Commands

```bash
# everyone, once per machine (needs: uv tool install "claude-swap[azure]")
cswap login az --subscription-id <id> --keyvault <vault-name>

# the admin
cswap team register xpto --storage https://<account>.table.core.windows.net
claude setup-token                                       # prints sk-ant-oat01-...
cswap add-token sk-ant-oat01-... --email shared1@example.com --into xpto

# members
cswap team join xpto
cswap team status
cswap watch
```

| Command | Does |
|---------|------|
| `cswap login az --subscription-id ID \| --tenant-id ID [--keyvault NAME] [--device-code]` | Signs in to Entra ID. Without `--tenant-id`, the subscription's tenant is looked up first. With `--keyvault`, saves it as the default vault and checks that you can list it. |
| `cswap logout az` | Forgets the sign-in. |
| `cswap keyvault [NAME\|URL]` | Shows or changes the default vault. |
| `cswap team register TEAM --storage URL [--table NAME] [--keyvault V] [--force]` | Creates the team: its manifest secret and the presence table. |
| `cswap add-token TOKEN --email EMAIL --into TEAM` | Writes the account to the vault, then adds it here. `--email` is required: a token does not say which account it is. If the vault write fails, nothing is added. |
| `cswap team join TEAM [--keyvault V]` | Joins and pulls the team's accounts. |
| `cswap team sync [TEAM] [--json]` | Pulls changes: new accounts, replaced tokens, removed accounts. |
| `cswap team status [TEAM] [--json]` | Who is on which account, from which machine, last seen when. |
| `cswap team list [--json]` | Teams joined on this machine. |
| `cswap team remove TEAM EMAIL` | Deletes an account from the vault (admin), then syncs. |
| `cswap team leave TEAM` | Removes the team's accounts from this machine. The vault keeps them. |

Team accounts show as `[team xpto]` in `cswap list`, and their `--json` rows carry `"team"`. `cswap export` skips them, so a member who leaves keeps no copy in a backup file. An email can be either a personal account or a team account on one machine, never both.

## What happens on its own

- **Sync.** `join`, `sync` and the dashboard (every `team.syncIntervalSeconds`, default 300) read the vault. Only secrets whose last-updated time changed are read in full. A replaced token is written to its slot; if that slot is the active account, it is re-activated so Claude Code stops using the old token. An account removed from the vault is removed here; if it was active, cswap switches to another account first.
- **Dead tokens.** In the dashboard, a team account whose token reads as expired or refused triggers a sync at once (at most once a minute). That is how a token an admin replaced reaches the team without anyone running a command.
- **Presence.** Every switch onto or off a team account writes this machine's row: the team account in use, or empty when on a personal account (a personal account's email is never published). The dashboard and `cswap auto` refresh the row every `team.heartbeatSeconds` (default 120). A row not refreshed for `team.presenceStaleSeconds` (default 900) shows as away.
- **Auto-switch.** When it moves proactively, `cswap auto` prefers, among the accounts it would land on anyway, the one with fewest people present. When escaping a limit, room left decides and the head count only breaks ties.

All of this is best-effort: if the vault or the table cannot be reached, or the sign-in has expired, switching and auto-switching carry on as without teams, and the dashboard title says why. Only `cswap login az` ever opens a browser.

## Layout

- Manifest: secret `cswap-<team>`, JSON `{schema, team, storage: {tableEndpoint, table}, createdBy, createdAt}`, tags `cswap-kind=manifest`, `cswap-team`.
- Account: secret `cswap-<team>--<email slug>-<6 hex>`, JSON `{schema, email, kind: setup_token|api_key, token}`, tags `cswap-kind=account`, `cswap-team`, `cswap-email`. Removing an account soft-deletes the secret; adding the same email again recovers it.
- Presence: table `cswappresence` (or `--table`), `PartitionKey` = team, `RowKey` = `<user>|<machine>`, columns `user`, `machine`, `accountEmail`, `since`, `lastSeen`.
- Local: `teams.json` in the backup directory (joined teams and their vault and table), `azure/auth_record.json` (who signed in; no secret), `azure/presence.json`. Entra ID tokens live in the OS keyring under the name `claude-swap` (DPAPI on Windows, Keychain on macOS, libsecret on Linux; `cswap login az --allow-unencrypted-cache` stores them unencrypted where there is no keyring).

## Limits

- A setup-token and a normal access token both start with `sk-ant-oat01-`, and telling them apart needs a request to Anthropic, which `add-token` never makes. Publishing a normal access token works until it expires a few hours later.
- Setup-tokens show `usage unavailable` unless `usage.headerProbe` is on (see the README).
- Presence tracks the default login only. Sessions started with `cswap run` are not shown.
- Everyone holding a token of an account shares that account's 5-hour and weekly limits.
