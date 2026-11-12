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
| `cswap add-token TOKEN --email EMAIL --into TEAM [--slot N]` | Writes the account to the vault, then adds it here. `--email` is required: a token does not say which account it is. If the vault write fails, nothing is added. When this machine already holds a normal login of that email, a setup-token is not added as a second slot: the login is linked instead, and shares the account's usage with the team (see below). `--slot` picks the login when there are several. |
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

## Usage without a probe: the admin's login shares it

A setup-token cannot read the usage endpoint, so members see `usage unavailable`, or spend a small request on the account's own window with `usage.headerProbe`. The admin who generated the setup-token usually holds the account's normal login as well, which reads usage for free. When `add-token --into` finds that login here, it links it instead of adding a slot:

```
$ cswap add-token sk-ant-oat01-... --email shared1@example.com --into xpto
Published shared1@example.com to team xpto.
Linked to slot 2, your login of shared1@example.com: no second slot is added here, and its usage is shared with the team.
```

- **On the admin's machine** the dashboard and `cswap auto` write each new reading of a linked login to the team's table (partition `<team>~usage` of the presence table, so no new role or setting is needed). `cswap list` shows the slot as `…, usage for team xpto`, and the watch lists it under the team. Using that login counts in presence as being on the team account.
- **On members' machines** the dashboard, `cswap auto` and `cswap team sync` adopt those readings for the team's setup-token slot, the way `cswap import-usage` does, and hold the slot for `team.presenceStaleSeconds`: no local fetch and no probe runs meanwhile, and the reading drives auto-switching.
- **Freshness** follows the admin's own polling of that account (every few minutes, slower when it is idle), and stops while the admin runs neither the dashboard nor `cswap auto`. When the hold lapses, members fall back to `usage unavailable` or their own probe.
- **The link is the admin's own account.** `team sync`, `team remove` and `team leave` only unlink it, never delete it; a re-login with `cswap add` keeps the link. A normal login is never published, only its readings.
- cswap cannot check that the setup-token belongs to the same account as the login: it trusts `--email`.

All of this is best-effort: if the vault or the table cannot be reached, or the sign-in has expired, switching and auto-switching carry on as without teams, and the dashboard title says why. Only `cswap login az` ever opens a browser.

## Layout

- Manifest: secret `cswap-<team>`, JSON `{schema, team, storage: {tableEndpoint, table}, createdBy, createdAt}`, tags `cswap-kind=manifest`, `cswap-team`.
- Account: secret `cswap-<team>--<email slug>-<6 hex>`, JSON `{schema, email, kind: setup_token|api_key, token}`, tags `cswap-kind=account`, `cswap-team`, `cswap-email`. Removing an account soft-deletes the secret; adding the same email again recovers it.
- Presence: table `cswappresence` (or `--table`), `PartitionKey` = team, `RowKey` = `<user>|<machine>`, columns `user`, `machine`, `accountEmail`, `since`, `lastSeen`.
- Shared usage: same table, `PartitionKey` = `<team>~usage`, `RowKey` = the account's email, columns `email`, `usage` (the `list --json` usage object), `ageAtWrite` (seconds), `publisher`. A reader's age is `ageAtWrite` plus the time since the row's own `Timestamp`, so no two machines' clocks have to agree.
- Linked login: `"teamLink": "<team>"` on the admin's slot record in `sequence.json`.
- Local: `teams.json` in the backup directory (joined teams and their vault and table), `azure/auth_record.json` (who signed in; no secret), `azure/presence.json`, `azure/usage.json` (which reading of each linked login was last shared). Entra ID tokens live in the OS keyring under the name `claude-swap` (DPAPI on Windows, Keychain on macOS, libsecret on Linux; `cswap login az --allow-unencrypted-cache` stores them unencrypted where there is no keyring).

## Limits

- A setup-token and a normal access token both start with `sk-ant-oat01-`, and telling them apart needs a request to Anthropic, which `add-token` never makes. Publishing a normal access token works until it expires a few hours later.
- Setup-tokens show `usage unavailable` unless the admin's login is linked (above) or `usage.headerProbe` is on (see the README).
- Presence tracks the default login only. Sessions started with `cswap run` are not shown.
- Everyone holding a token of an account shares that account's 5-hour and weekly limits.
