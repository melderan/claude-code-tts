"""Does this machine trust what it should not, and are the downloaded voices the real files?

`claude-tts verify` runs these checks locally; nothing is sent anywhere. They exist because of the
eMudhra (emSign) incident of October 2026: eight intermediate CA keys were recoverable from their
public certificates, so for about 17 months anyone could have signed a valid certificate for any
host, Hugging Face included. Browsers blocked the intermediates; a CA bundle read by OpenSSL (curl
on Linux, Homebrew's Python, certifi inside the engine venvs) blocks nothing until the root that
chains them, emSign ECC Root CA - G3, is gone from it.

Three questions, one Check each:
  voices   each installed Piper file hashed against piper_pins.py; a mismatch is a file that is
           not the one Hugging Face published at the pinned commit
  stores   the CA bundles this tool's downloads trust, searched for a distrusted root by its
           SHA-256 fingerprint (never by name)
  venvs    certifi inside the sherpa and mlx venvs, which huggingface_hub and requests trust

Exit 0 when nothing is affected, 1 when something is, 2 when a check could not run.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_code_tts.audio import detect_platform


@dataclass(frozen=True)
class DistrustedRoot:
    name: str
    sha256: str  # of the DER certificate
    why: str
    certifi_without: str  # the first certifi release that no longer carries it


# Fingerprints read from certifi 2026.7.22 and from Node v24.0.0's node_root_certs.h (two builds
# of Mozilla's list) and equal in both. certifi 2026.10.18, the release after 2026.7.22, is the
# first without this root (wheel checked against PyPI's sha256, then by fingerprint); it kept
# emSign G1, C1 and C3, which did not chain the compromised intermediates.
DISTRUSTED_ROOTS = (
    DistrustedRoot(
        name="emSign ECC Root CA - G3",
        sha256="86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
        why="eMudhra's eight G3 intermediates had trivially weak keys (reported 2026-10-17)",
        certifi_without="2026.10.18",
    ),
)

STATUS_ORDER = ("affected", "unknown", "note", "ok", "skip")

_PEM = re.compile(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)
_MAC_KEYCHAINS = (
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    "/Library/Keychains/System.keychain",
)


@dataclass(frozen=True)
class Check:
    status: str  # affected | unknown | note | ok | skip
    what: str
    detail: str = ""
    fix: str = ""


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def distrusted_in(pem_bytes: bytes) -> list[DistrustedRoot]:
    """The distrusted roots among the PEM certificates in pem_bytes."""
    fps = set()
    for block in _PEM.findall(pem_bytes):
        try:
            fps.add(hashlib.sha256(ssl.PEM_cert_to_DER_cert(block.decode("ascii"))).hexdigest())
        except (ValueError, UnicodeDecodeError):
            continue
    return [r for r in DISTRUSTED_ROOTS if r.sha256 in fps]


def check_voices(voices_dir: Path, pins: dict[str, str]) -> list[Check]:
    if not voices_dir.is_dir():
        return [Check("skip", "voices", f"no {voices_dir}")]
    out = []
    for path in sorted(p for p in voices_dir.iterdir() if p.name.endswith((".onnx", ".onnx.json"))):
        want = pins.get(path.name)
        if want is None:
            out.append(Check("skip", f"voice {path.name}", "not from the curated list; no pin to check against"))
            continue
        try:
            got = file_sha256(path)
        except OSError as e:
            out.append(Check("unknown", f"voice {path.name}", f"unreadable: {e}"))
            continue
        if got == want:
            out.append(Check("ok", f"voice {path.name}", "matches its pinned SHA-256"))
        else:
            voice = path.name.removesuffix(".json").removesuffix(".onnx")
            out.append(Check(
                "affected", f"voice {path.name}", f"SHA-256 {got} is not the pinned {want}",
                fix=f"delete {path} and run: claude-tts-install --voice {voice}",
            ))
    return out or [Check("skip", "voices", f"no voice files in {voices_dir}")]


def _bundle_paths() -> list[Path]:
    """CA bundle files and directories OpenSSL-based clients on this machine read."""
    vp = ssl.get_default_verify_paths()
    names = [os.environ.get("SSL_CERT_FILE"), os.environ.get("CURL_CA_BUNDLE"), os.environ.get("REQUESTS_CA_BUNDLE"),
             vp.cafile, vp.openssl_cafile, os.environ.get("SSL_CERT_DIR"), vp.capath, vp.openssl_capath]
    if shutil.which("brew"):
        names.append("/opt/homebrew/etc/ca-certificates/cert.pem")
        names.append("/usr/local/etc/ca-certificates/cert.pem")
    seen: list[Path] = []
    for n in names:
        if not n:
            continue
        p = Path(n).resolve()
        if p.exists() and p not in seen:
            seen.append(p)
    return seen


def _read_store(path: Path) -> bytes:
    if path.is_file():
        return path.read_bytes()
    chunks = []
    for f in sorted(path.iterdir()):
        if f.is_file() and (f.suffix in (".pem", ".crt") or re.fullmatch(r"[0-9a-f]{8}\.\d+", f.name)):
            try:
                chunks.append(f.read_bytes())
            except OSError:
                continue
    return b"\n".join(chunks)


def check_stores() -> list[Check]:
    out = []
    upgrade = "brew upgrade ca-certificates" if shutil.which("brew") else "update the ca-certificates package (apt, dnf)"
    for path in _bundle_paths():
        try:
            hits = distrusted_in(_read_store(path))
        except OSError as e:
            out.append(Check("unknown", f"store {path}", f"unreadable: {e}"))
            continue
        if hits:
            out.extend(Check("affected", f"store {path}", f"still trusts {r.name}: {r.why}", fix=upgrade) for r in hits)
        else:
            out.append(Check("ok", f"store {path}", "no distrusted root"))
    if detect_platform() == "macos" and shutil.which("security"):
        for kc in _MAC_KEYCHAINS:
            try:
                pem = subprocess.run(["security", "find-certificate", "-a", "-p", kc],
                                     capture_output=True, timeout=30).stdout
            except (OSError, subprocess.TimeoutExpired) as e:
                out.append(Check("unknown", f"keychain {kc}", str(e)))
                continue
            hits = distrusted_in(pem)
            out.extend(Check(
                "note", f"keychain {kc}", f"holds {r.name}; Apple announced a block of its intermediates on 2026-10-19",
                fix="keep macOS current (Software Update)",
            ) for r in hits)
            if not hits:
                out.append(Check("ok", f"keychain {kc}", "no distrusted root"))
    return out or [Check("unknown", "stores", "found no CA bundle to read")]


def check_venvs(venvs: dict[str, Path]) -> list[Check]:
    out = []
    for label, venv in venvs.items():
        if not venv.is_dir():
            out.append(Check("skip", f"venv {label}", f"not installed ({venv})"))
            continue
        bundles = sorted(venv.glob("lib/python*/site-packages/certifi/cacert.pem"))
        if not bundles:
            out.append(Check("ok", f"venv {label}", "no certifi bundle"))
            continue
        for b in bundles:
            init = b.parent / "__init__.py"
            m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text()) if init.exists() else None
            ver = m.group(1) if m else "unknown version"
            hits = distrusted_in(b.read_bytes())
            if hits:
                # A floor, not a bare --upgrade: an index that holds back new releases would
                # otherwise resolve to a certifi that still carries the root and report success.
                out.extend(Check(
                    "affected", f"venv {label}", f"certifi {ver} still trusts {r.name}",
                    fix=f"uv pip install --python {venv / 'bin' / 'python'} --upgrade 'certifi>={r.certifi_without}'",
                ) for r in hits)
            else:
                out.append(Check("ok", f"venv {label}", f"certifi {ver}, no distrusted root"))
    return out


def exit_code(checks: list[Check]) -> int:
    if any(c.status == "affected" for c in checks):
        return 1
    if any(c.status == "unknown" for c in checks):
        return 2
    return 0
