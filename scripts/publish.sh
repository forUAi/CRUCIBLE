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

# Disable every credential helper for these invocations. On macOS git uses
# osxkeychain by default, and a stored github.com credential is consulted
# BEFORE GIT_ASKPASS -- so the token in .env was ignored and the push was
# attempted as whichever account the keychain happened to hold. The error
# ("denied to <other-account>") names that account, not the token's.
GIT_NOCRED=(-c credential.helper= -c credential."https://github.com".helper=)

REMOTE_URL="https://github.com/${GITHUB_REPO}.git"

echo "repository : ${GITHUB_REPO}"
echo "branch     : ${GITHUB_BRANCH}"
echo "local HEAD : $(git rev-parse --short HEAD)"
echo "tag v0.1.0 : $(git rev-list -n1 v0.1.0 2>/dev/null || echo '<missing>')"
echo

# ------------------------------------------------------------ access check
echo "checking write access..."
if ! git "${GIT_NOCRED[@]}" ls-remote "$REMOTE_URL" >/dev/null 2>&1; then
  echo "  cannot read ${GITHUB_REPO}: check the token and the repository name." >&2
  exit 1
fi

# A dry-run push is the cheapest honest write test. Its output is KEPT: the
# first version of this script sent it to /dev/null and reported every failure
# as "write denied", which misdiagnosed a credential-helper collision as a
# permissions problem. A check that discards the evidence it is checking is
# worse than no check.
if ! PUSH_ERR="$(git "${GIT_NOCRED[@]}" push --dry-run "$REMOTE_URL" \
                     "HEAD:refs/heads/${GITHUB_BRANCH}" 2>&1)"; then
  echo "  push refused. git said:" >&2
  echo "$PUSH_ERR" | sed 's/^/    /' >&2
  case "$PUSH_ERR" in
    *"denied to"*)
      echo >&2
      echo "  If the account named above is not the one that owns your token," >&2
      echo "  a credential helper overrode it. This script disables them; a" >&2
      echo "  manual push needs: git -c credential.helper= push ..." >&2 ;;
    *"non-fast-forward"*|*"fetch first"*)
      echo >&2
      echo "  The remote moved. Merge it -- never force-push:" >&2
      echo "    git fetch origin && git merge origin/${GITHUB_BRANCH}" >&2 ;;
  esac
  exit 1
fi
echo "  write access confirmed"

if [[ $DRY_RUN -eq 1 ]]; then
  echo
  echo "dry run: would push ${GITHUB_BRANCH} and tag v0.1.0, then create the release."
  echo "$PUSH_ERR" | sed 's/^/    /'
  exit 0
fi

# ------------------------------------------------------------------- push
# No --force anywhere. A rejection here means the remote moved; resolve it by
# merging, never by overwriting.
echo
echo "pushing ${GITHUB_BRANCH}..."
git "${GIT_NOCRED[@]}" push "$REMOTE_URL" "HEAD:refs/heads/${GITHUB_BRANCH}"

echo "pushing tag v0.1.0..."
git "${GIT_NOCRED[@]}" push "$REMOTE_URL" v0.1.0

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
