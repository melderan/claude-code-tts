#!/bin/bash
# Clean-machine proof: tests/docker/run-clean-install-test.sh [TAG] [REPO_URL]
#                      WHEEL=dist/x.whl tests/docker/run-clean-install-test.sh   (install the local wheel instead)
# Builds an Ubuntu image with a plain user, copies in the local uv binary, and runs
# clean_install_e2e.sh against the given tag. Proxy variables are passed through for sandboxes.
set -euo pipefail
cd "$(dirname "$0")/../.."
TAG="${1:-main}"; REPO="${2:-https://github.com/melderan/claude-code-tts}"
UV_BIN="$(command -v uv)" || { echo "uv not found on the host" >&2; exit 1; }
docker build -q -t claude-tts-clean-install -f tests/docker/Dockerfile.clean-install . >/dev/null
# Inside a Docker Sandbox the proxy terminates TLS with its own CA; hand the container the same
# trust bundle the sandbox uses so uv, pip and curl accept it. A plain host has no proxy and skips this.
CA_ARGS=()
if [ -n "${HTTPS_PROXY:-${https_proxy:-}}" ] && [ -f /etc/ssl/certs/ca-certificates.crt ]; then
  CA_ARGS=(-v /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro
           -e SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt -e REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt)
fi
WHEEL_ARGS=()
if [ -n "${WHEEL:-}" ]; then
  # Stage the wheel under $HOME: some daemons (Docker Sandboxes) cannot bind-mount files from a
  # host-mounted tree, and do not see the caller's /tmp either. The home directory works.
  mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}"; STAGE="$(mktemp -d "${XDG_CACHE_HOME:-$HOME/.cache}/claude-tts-e2e.XXXXXX")"; cp "$WHEEL" "$STAGE/"; chmod 755 "$STAGE"; trap 'rm -rf "$STAGE"' EXIT   # container user is a different uid
  WHEEL_ARGS=(-v "$STAGE:/wheel:ro" -e WHEEL="/wheel/$(basename "$WHEEL")")
fi
docker run --rm "${CA_ARGS[@]}" "${WHEEL_ARGS[@]}" \
  -v "$UV_BIN:/usr/local/bin/uv:ro" \
  -e TAG="$TAG" -e REPO="$REPO" \
  ${HTTP_PROXY:+-e HTTP_PROXY=$HTTP_PROXY} ${HTTPS_PROXY:+-e HTTPS_PROXY=$HTTPS_PROXY} ${NO_PROXY:+-e NO_PROXY=$NO_PROXY} \
  ${http_proxy:+-e http_proxy=$http_proxy} ${https_proxy:+-e https_proxy=$https_proxy} ${no_proxy:+-e no_proxy=$no_proxy} \
  claude-tts-clean-install
