"""claude-tts verify: pinned voice digests and distrusted CA roots, with planted positives."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from claude_code_tts import install, trust
from claude_code_tts.piper_pins import PIPER_BASE, PIPER_REVISION, PIPER_SHA256

FIXTURES = Path(__file__).parent / "fixtures"
G3 = (FIXTURES / "emsign-ecc-root-g3.pem").read_bytes()
C3 = (FIXTURES / "emsign-ecc-root-c3.pem").read_bytes()


class TestDistrustedRoots:
    def test_g3_is_found_by_fingerprint(self):
        assert [r.name for r in trust.distrusted_in(G3)] == ["emSign ECC Root CA - G3"]

    def test_an_unaffected_emsign_root_is_not_flagged(self):
        assert trust.distrusted_in(C3) == []

    def test_found_inside_a_bundle_among_others(self):
        assert len(trust.distrusted_in(C3 + b"\n" + G3 + b"\n" + C3)) == 1

    def test_a_renamed_label_does_not_hide_it(self):
        assert trust.distrusted_in(G3.replace(b"emSign ECC Root CA - G3", b"something else")) != []


class TestVoices:
    def _pins(self, **files: bytes) -> dict[str, str]:
        return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}

    def test_matching_file_is_ok_and_changed_file_is_affected(self, tmp_path):
        (tmp_path / "a.onnx").write_bytes(b"model a")
        (tmp_path / "b.onnx").write_bytes(b"model b, swapped")
        pins = self._pins(**{"a.onnx": b"model a", "b.onnx": b"model b"})
        by_what = {c.what: c for c in trust.check_voices(tmp_path, pins)}
        assert by_what["voice a.onnx"].status == "ok"
        swapped = by_what["voice b.onnx"]
        assert swapped.status == "affected"
        assert "claude-tts-install --voice b" in swapped.fix

    def test_a_voice_outside_the_catalog_is_skipped_not_passed(self, tmp_path):
        (tmp_path / "mine.onnx").write_bytes(b"x")
        assert [c.status for c in trust.check_voices(tmp_path, {})] == ["skip"]

    def test_no_voices_dir_is_a_skip(self, tmp_path):
        assert trust.check_voices(tmp_path / "absent", {})[0].status == "skip"


class TestStoresAndVenvs:
    def test_a_bundle_holding_g3_is_affected(self, tmp_path, monkeypatch):
        bundle = tmp_path / "cert.pem"
        bundle.write_bytes(C3 + b"\n" + G3)
        monkeypatch.setattr(trust, "_bundle_paths", lambda: [bundle])
        monkeypatch.setattr(trust, "detect_platform", lambda: "linux")
        assert [c.status for c in trust.check_stores()] == ["affected"]

    def test_a_cert_directory_is_read_file_by_file(self, tmp_path, monkeypatch):
        (tmp_path / "1a2b3c4d.0").write_bytes(G3)
        (tmp_path / "other.pem").write_bytes(C3)
        monkeypatch.setattr(trust, "_bundle_paths", lambda: [tmp_path])
        monkeypatch.setattr(trust, "detect_platform", lambda: "linux")
        assert [c.status for c in trust.check_stores()] == ["affected"]

    def _venv(self, root: Path, pem: bytes, version: str) -> Path:
        pkg = root / "lib" / "python3.12" / "site-packages" / "certifi"
        pkg.mkdir(parents=True)
        (pkg / "cacert.pem").write_bytes(pem)
        (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n')
        return root

    def test_venv_with_old_certifi_is_affected_with_an_upgrade_line(self, tmp_path):
        venv = self._venv(tmp_path / "mlx", C3 + G3, "2026.07.22")
        [c] = trust.check_venvs({"mlx": venv})
        assert c.status == "affected" and "2026.07.22" in c.detail
        assert c.fix == f"uv pip install --python {venv / 'bin' / 'python'} --upgrade certifi"

    def test_venv_with_fixed_certifi_is_ok(self, tmp_path):
        [c] = trust.check_venvs({"mlx": self._venv(tmp_path / "mlx", C3, "2026.10.19")})
        assert c.status == "ok"

    def test_exit_codes(self):
        ok, unknown, bad = trust.Check("ok", "a"), trust.Check("unknown", "b"), trust.Check("affected", "c")
        assert trust.exit_code([ok]) == 0
        assert trust.exit_code([ok, unknown]) == 2
        assert trust.exit_code([unknown, bad]) == 1


class TestPins:
    def test_every_catalog_voice_is_pinned_both_files(self):
        for name, *_rest in install.AVAILABLE_VOICES:
            assert re.fullmatch(r"[0-9a-f]{64}", PIPER_SHA256[f"{name}.onnx"]), name
            assert re.fullmatch(r"[0-9a-f]{64}", PIPER_SHA256[f"{name}.onnx.json"]), name

    def test_downloads_come_from_a_commit_not_a_tag(self):
        assert re.fullmatch(r"[0-9a-f]{40}", PIPER_REVISION)
        assert install.HF_VOICES_BASE == PIPER_BASE
        assert f"/resolve/{PIPER_REVISION}/" in install.VOICE_URL


class TestDownloadRefusesAChangedFile:
    def _fake_curl(self, monkeypatch, dest: Path, data: bytes) -> None:
        monkeypatch.setattr(install, "run_cmd", lambda cmd, **kw: dest.write_bytes(data))

    def test_mismatch_deletes_and_dies(self, tmp_path, monkeypatch):
        dest = tmp_path / "voice.onnx.json"
        self._fake_curl(monkeypatch, dest, b'{"swapped": true}')
        with pytest.raises(SystemExit):
            install.download_file("https://example.invalid/v.json", dest, sha256=hashlib.sha256(b"{}").hexdigest())
        assert not dest.exists()

    def test_match_keeps_the_file(self, tmp_path, monkeypatch):
        dest = tmp_path / "voice.onnx.json"
        self._fake_curl(monkeypatch, dest, b"{}")
        install.download_file("https://example.invalid/v.json", dest, sha256=hashlib.sha256(b"{}").hexdigest())
        assert dest.read_bytes() == b"{}"

    def test_download_voice_refuses_a_voice_with_no_pin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install, "VOICES_DIR", tmp_path)
        monkeypatch.setattr(install, "download_file", lambda *a, **kw: pytest.fail("downloaded an unpinned voice"))
        assert install.download_voice("en_US-nobody-medium", "en/en_US/nobody/medium") is False
