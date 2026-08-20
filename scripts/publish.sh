#!/usr/bin/env bash
#
# Publish the prepared CRUCIBLE release using a token from .env.
#
# The token is read from .env and handed to git through GIT_ASKPASS, so it
# never appears in the command line (visible in `ps`), never gets written into
# .git/config by a remote URL, and is not echoed. `set -x` is deliberately not
# used anywhere in this script.
#
#   cp .env.example .env && $EDITOR .env
#   ./scripts/publish.sh              # push branch + tag, then create the release
#   ./scripts/publish.sh --dry-run    # check access and show what would happen
#
set -euo pipefail

cd "$(dirname "$0")/.."

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# ---------------------------------------------------------------- load .env
if [[ ! -f .env ]]; then
  echo "no .env found. Create one:" >&2
  echo "    cp .env.example .env && \$EDITOR .env" >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a; source ./.env; set +a

: "${GITHUB_TOKEN:?GITHUB_TOKEN is empty in .env}"
GITHUB_REPO="${GITHUB_REPO:-forUAi/CRUCIBLE}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"

if [[ ${#GITHUB_TOKEN} -lt 20 ]]; then
  echo "GITHUB_TOKEN looks too short to be a real token." >&2
  exit 1
fi

# Guard against the one mistake that would be unrecoverable: committing it.
if git check-ignore -q .env; then :; else
  echo ".env is NOT gitignored. Refusing to continue." >&2
  exit 1
fi
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo ".env is TRACKED by git. Refusing to continue; run: git rm --cached .env" >&2
  exit 1
fi

# ------------------------------------------------------- token via askpass
# A temp helper that prints the token. Git calls it for the password prompt,
# so the secret never reaches argv or .git/config.
ASKPASS="$(mktemp)"
chmod 700 "$ASKPASS"
cat > "$ASKPASS" <<'HELPER'
#!/usr/bin/env bash
case "$1" in
  *Username*) echo "x-access-token" ;;
  *)          echo "${GITHUB_TOKEN}" ;;
esac
HELPER
cleanup() { rm -f "$ASKPASS"; }
trap cleanup EXIT

export GIT_ASKPASS="$ASKPASS"
export GIT_TERMINAL_PROMPT=0

REMOTE_URL="https://github.com/${GITHUB_REPO}.git"

echo "repository : ${GITHUB_REPO}"
echo "branch     : ${GITHUB_BRANCH}"
echo "local HEAD : $(git rev-parse --short HEAD)"
echo "tag v0.1.0 : $(git rev-list -n1 v0.1.0 2>/dev/null || echo '<missing>')"
echo

# ------------------------------------------------------------ access check
echo "checking write access..."
if ! git ls-remote "$REMOTE_URL" >/dev/null 2>&1; then
  echo "  cannot read ${GITHUB_REPO}: check the token and the repository name." >&2
  exit 1
fi

# A no-op push to an unused ref is the cheapest honest write test.
if git push --dry-run "$REMOTE_URL" "HEAD:refs/heads/${GITHUB_BRANCH}" >/dev/null 2>&1; then
  echo "  write access confirmed"
else
  echo "  WRITE DENIED. The token authenticates, but its account has no push" >&2
  echo "  permission on ${GITHUB_REPO}." >&2
  echo "  Use a token from an account that owns or collaborates on that repo." >&2
  exit 1
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo
  echo "dry run: would push ${GITHUB_BRANCH} and tag v0.1.0, then create the release."
  git push --dry-run "$REMOTE_URL" "HEAD:refs/heads/${GITHUB_BRANCH}" 2>&1 | sed 's/^/    /'
  exit 0
fi

# ------------------------------------------------------------------- push
# No --force anywhere. A rejection here means the remote moved; resolve it by
# merging, never by overwriting.
echo
echo "pushing ${GITHUB_BRANCH}..."
git push "$REMOTE_URL" "HEAD:refs/heads/${GITHUB_BRANCH}"

echo "pushing tag v0.1.0..."
git push "$REMOTE_URL" v0.1.0

# ---------------------------------------------------------------- release
ASSETS_DIR="${ASSETS_DIR:-/tmp/release-assets}"
EXPECTED_SHA="db248379ee3910c8c959b51d076cbe59711b5a75ec59fc3a79318b42ae6bb6ae"

if command -v gh >/dev/null 2>&1; then
  echo
  echo "creating the GitHub release..."
  export GH_TOKEN="$GITHUB_TOKEN"

  ART="${ASSETS_DIR}/crucible-0.1.0.tar.gz"
  UPLOAD=()
  if [[ -f "$ART" ]]; then
    ACTUAL="$(shasum -a 256 "$ART" | cut -d' ' -f1)"
    if [[ "$ACTUAL" == "$EXPECTED_SHA" ]]; then
      UPLOAD+=("$ART")
      for extra in crucible-0.1.0.tar.gz.sha256 \
                   crucible-v0.1.0-evidence.tar.gz SHA256SUMS; do
        [[ -f "${ASSETS_DIR}/${extra}" ]] && UPLOAD+=("${ASSETS_DIR}/${extra}")
      done
    else
      echo "  artifact hash mismatch -- refusing to upload it under v0.1.0." >&2
      echo "    expected ${EXPECTED_SHA}" >&2
      echo "    actual   ${ACTUAL}" >&2
    fi
  else
    echo "  no artifact at ${ART}; creating the release without assets."
  fi

  gh release create v0.1.0 "${UPLOAD[@]}" \
     --repo "$GITHUB_REPO" \
     --title "CRUCIBLE v0.1.0" \
     --notes-file docs/RELEASE_NOTES_v0.1.0.md \
     --verify-tag
else
  echo
  echo "gh not installed; branch and tag are pushed. Create the release at:"
  echo "  https://github.com/${GITHUB_REPO}/releases/new?tag=v0.1.0"
fi

echo
echo "done. Verify:"
echo "  https://github.com/${GITHUB_REPO}"
echo "  https://github.com/${GITHUB_REPO}/releases/tag/v0.1.0"
