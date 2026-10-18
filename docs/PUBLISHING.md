# Publishing JIZO to PyPI

Everything in this file is a **one-time** setup, after which publishing is a
GitHub release and nothing more. No API token is ever stored in this
repository.

**Why not just `pip install jizo`?** That name is already taken on PyPI by an
unrelated 1 KB placeholder package (owner `kaliv0`, published February 2025).
PyPI names are permanent — once taken, a name cannot be re-registered by
anyone else. So the distribution is `jizo-resilience` and the import stays
`jizo`.

---

## One-time setup

### 1. Get a PyPI account

1. Go to <https://pypi.org/account/register/> and create the account
   (`kuuraskii` or whatever address you want public — it shows on the
   package page).
2. Verify the email. PyPI will not let you publish until you do.

### 2. Turn on two-factor authentication

PyPI **requires** 2FA before a project can be published.

1. Go to <https://pypi.org/manage/account/two-factor/>.
2. Scan the QR code with an authenticator app (Google Authenticator, Authy,
   1Password — any TOTP app).
3. Enter the 6-digit code to confirm.

You only need this for the PyPI website itself. The automated publishing below
does **not** use your account password or an API token.

### 3. Register JIZO as a trusted publisher

This is what creates the `jizo-resilience` project and lets GitHub publish to
it without a secret.

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Click **Add a new pending publisher**.
3. Fill in:

   | Field | Value |
   |---|---|
   | PyPI project name | `jizo-resilience` |
   | Owner | `kuuraskii` |
   | Repository name | `Jizo` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

4. Click **Add pending publisher**.

### 4. Create the matching GitHub environment

1. Go to <https://github.com/kuuraskii/Jizo/settings/environments>.
2. Click **New environment**, name it exactly `pypi`, and save.

The name must match step 3 character for character, or PyPI rejects the token.

---

## Publishing a release

From PowerShell, in `C:\my work\Jizo`:

```powershell
git pull
git tag v0.1.0
git push origin v0.1.0
```

Or, simpler — use the GitHub website: **Releases → Draft a new release → tag
`v0.1.0` → Publish release.** The workflow triggers on a published release.

Watch it run at <https://github.com/kuuraskii/Jizo/actions>. Three jobs:

```
test     → runs all 559 tests against Postgres; if red, nothing is published
build    → produces jizo_resilience-0.1.0-py3-none-any.whl and the .tar.gz
publish  → mints an OIDC token and uploads to PyPI
```

When it finishes, verify:

```powershell
pip install jizo-resilience
python -c "import jizo; print(jizo.__version__)"
```

---

## Publishing a later version

Bump `__version__` in `jizo/__init__.py`, commit, then release with the next
tag (`v0.1.1`, `v0.2.0`, …). Nothing else changes — `pyproject.toml` reads the
version from there, and a test fails if the two ever disagree.

---

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `403` or "trusted publisher not found" | Environment name mismatch | Check steps 3 and 4 match exactly, including capitalisation |
| `invalid` project name error | Project already exists from a manual upload | Use **trusted publishing** only; never upload by hand as well |
| Build job fails on the wheel check | Distribution renamed without updating it | The check in `publish.yml` expects `jizo_resilience-*.whl` |
| `from jizo import ...` fails after installing | The unrelated `jizo` stub is also installed | `pip uninstall jizo`, then reinstall `jizo-resilience` |

---

## Rollback

Files cannot be deleted from PyPI once published — that is deliberate, so
people's builds keep working. To back out a bad release:

1. `yank` the version on the PyPI project page (installers stop getting it,
   existing installs keep working).
2. Fix on `main`, then publish a new version.