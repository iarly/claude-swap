# Releasing cswap-teams

This fork publishes to PyPI as **`cswap-teams`** (the `claude-swap` name belongs to the upstream project). `.github/workflows/publish.yml` builds, tests and uploads it with PyPI trusted publishing, so no API token is stored anywhere.

## One-time setup

1. **PyPI account.** Create one at <https://pypi.org/account/register/> and turn on two-factor authentication. Do the same on <https://test.pypi.org> for dry runs; it is a separate site with separate accounts.
2. **Pending publisher on PyPI.** At <https://pypi.org/manage/account/publishing/>, under "Add a new pending publisher" (the project doesn't exist yet; the first upload creates it):

   | Field | Value |
   |---|---|
   | PyPI Project Name | `cswap-teams` |
   | Owner | `iarly` |
   | Repository name | `claude-swap` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

3. **The same on TestPyPI** at <https://test.pypi.org/manage/account/publishing/>, with environment name `testpypi`.
4. **GitHub environments.** In the repository, Settings → Environments, create `pypi` and `testpypi`. On `pypi`, add yourself under "Required reviewers", so every upload to PyPI waits for your approval, and limit "Deployment branches and tags" to tags `v*`.
5. **Actions enabled** on the fork (Settings → Actions → General). GitHub disables workflows on new forks until you allow them.

## Each release

1. Merge the work into `main`, with CI green.
2. Set the version in `pyproject.toml` (`version = "1.2.0b2"`; PEP 440, pre-releases as `bN`/`rcN`), then refresh the lock and commit:

   ```bash
   uv lock
   git commit -am "chore(release): 1.2.0b2"
   git push
   ```

3. Optional dry run: Actions → "Publish to PyPI" → Run workflow (on `main`). It tests, builds and uploads to TestPyPI. Try it with:

   ```bash
   uv tool install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "cswap-teams[azure]==1.2.0b2"
   ```

4. Create the release, with a tag that matches the version exactly plus a leading `v`:

   ```bash
   gh release create v1.2.0b2 --target main --generate-notes --prerelease   # drop --prerelease for a final version
   ```

   The workflow checks the tag against `pyproject.toml`, runs the tests, builds, and waits for your approval on the `pypi` environment before uploading.
5. Check <https://pypi.org/project/cswap-teams/> and `uv tool upgrade cswap-teams` on a machine.

A version can be uploaded only once: PyPI refuses to replace a file, even after deleting it. To fix a broken release, publish the next version (or `.post1`).

## Pulling from upstream

```bash
git fetch upstream
git merge upstream/main
```

Keep `name = "cswap-teams"`, this fork's URLs and its own version in `pyproject.toml` when resolving conflicts, and keep `src/claude_swap/distribution.py`: the update check and upgrade hints read the package name from it.
