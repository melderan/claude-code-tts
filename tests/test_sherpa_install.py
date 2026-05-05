"""Tests for the curated sherpa picklist + `claude-tts sherpa install`.

Covers:
  - Catalog has the structural fields every consumer relies on
  - SHA256 helper computes the right digest for real bytes
  - `sherpa install` respects the confirm prompt (--yes / n / y)
  - Mismatched SHA256 aborts and cleans up the bad download
  - Already-installed model short-circuits without re-downloading
  - Tarball "bomb" defense rejects absolute paths and `..` components
"""

from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_code_tts import cli, sherpa_catalog


# --- Catalog structure ---


class TestCatalogStructure:
    """Every catalog entry must have the fields the rest of the code uses.
    A missing field is a silent regression that only shows up when an
    operator tries to install — exactly the kind of failure mode this
    test exists to prevent."""

    REQUIRED_FIELDS = (
        "id", "url", "sha256", "compressed_bytes", "extracted_mb",
        "layout", "license_weights", "license_notes",
        "voices", "voices_list", "sample_rate", "source_page",
        "language", "verified_date",
    )

    def test_catalog_is_nonempty(self):
        assert len(sherpa_catalog.CATALOG) >= 1

    def test_every_entry_has_required_fields(self):
        for model_id, entry in sherpa_catalog.CATALOG.items():
            missing = [f for f in self.REQUIRED_FIELDS if f not in entry]
            assert not missing, f"{model_id} missing fields: {missing}"

    def test_id_field_matches_dict_key(self):
        for model_id, entry in sherpa_catalog.CATALOG.items():
            assert entry["id"] == model_id, (
                f"Catalog dict key {model_id!r} doesn't match entry['id']={entry['id']!r}"
            )

    def test_sha256_is_64_hex_chars(self):
        for model_id, entry in sherpa_catalog.CATALOG.items():
            sha = entry["sha256"]
            assert len(sha) == 64, f"{model_id}: sha256 wrong length: {len(sha)}"
            assert all(c in "0123456789abcdef" for c in sha), \
                f"{model_id}: sha256 has non-hex chars"

    def test_url_uses_immutable_path(self):
        for model_id, entry in sherpa_catalog.CATALOG.items():
            url = entry["url"]
            # Reject mutable references in URLs we hardcode.
            assert ":latest" not in url, f"{model_id}: url has :latest"
            assert "/main/" not in url, f"{model_id}: url has /main/ branch"
            assert url.startswith("https://"), f"{model_id}: url not https"

    def test_layout_is_known(self):
        for model_id, entry in sherpa_catalog.CATALOG.items():
            assert entry["layout"] in ("vits", "kokoro", "matcha"), \
                f"{model_id}: unknown layout {entry['layout']}"

    def test_license_is_permissive(self):
        # Catalog policy: only MIT/Apache/BSD/CC0 weights licenses.
        approved = ("MIT", "Apache 2.0", "BSD-2-Clause", "BSD-3-Clause", "CC0")
        for model_id, entry in sherpa_catalog.CATALOG.items():
            lic = entry["license_weights"]
            assert any(a in lic for a in approved), \
                f"{model_id}: license_weights not in approved set: {lic!r}"


# --- SHA256 helper ---


class TestSha256File:
    def test_known_digest_for_known_bytes(self, tmp_path):
        # Empty file → known SHA256
        empty = tmp_path / "empty"
        empty.write_bytes(b"")
        assert cli._sha256_file(empty) == \
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_matches_hashlib(self, tmp_path):
        data = b"the quick brown fox" * 1000
        p = tmp_path / "data"
        p.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert cli._sha256_file(p) == expected

    def test_chunked_reads_match(self, tmp_path):
        # File larger than default chunk_size, verify chunked accumulation
        data = b"x" * (65536 * 3 + 17)
        p = tmp_path / "big"
        p.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert cli._sha256_file(p, chunk_size=65536) == expected


# --- Tarball bomb defense ---


class TestExtractTarbz2:
    def _make_archive(self, tmp_path, layout: dict[str, bytes]) -> Path:
        """Build a .tar.bz2 in tmp_path with given filename → bytes contents."""
        archive = tmp_path / "test.tar.bz2"
        with tarfile.open(archive, "w:bz2") as tar:
            for name, data in layout.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return archive

    def test_extract_normal_archive(self, tmp_path):
        archive = self._make_archive(tmp_path, {
            "model-x/model.onnx": b"\x00\x01",
            "model-x/tokens.txt": b"a b c",
        })
        dest = tmp_path / "out"
        result = cli._extract_tarbz2(archive, dest)
        assert result == dest / "model-x"
        assert (dest / "model-x" / "model.onnx").is_file()
        assert (dest / "model-x" / "tokens.txt").read_text() == "a b c"

    def test_rejects_absolute_path(self, tmp_path, capsys):
        archive = self._make_archive(tmp_path, {
            "/etc/passwd": b"hax",
        })
        dest = tmp_path / "out"
        result = cli._extract_tarbz2(archive, dest)
        assert result is None
        assert "suspicious" in capsys.readouterr().out.lower()

    def test_rejects_dotdot(self, tmp_path, capsys):
        archive = self._make_archive(tmp_path, {
            "../escape/file": b"hax",
        })
        dest = tmp_path / "out"
        result = cli._extract_tarbz2(archive, dest)
        assert result is None
        assert "suspicious" in capsys.readouterr().out.lower()

    def test_rejects_multiple_top_level_dirs(self, tmp_path, capsys):
        archive = self._make_archive(tmp_path, {
            "dir-a/file.txt": b"a",
            "dir-b/file.txt": b"b",
        })
        dest = tmp_path / "out"
        result = cli._extract_tarbz2(archive, dest)
        assert result is None
        assert "top-level" in capsys.readouterr().out.lower()


# --- `sherpa install` flow ---


@pytest.fixture
def fake_models_dir(tmp_path, monkeypatch):
    models = tmp_path / "sherpa-models"
    monkeypatch.setattr("claude_code_tts.config.SHERPA_MODELS_DIR", models)
    return models


def _make_kokoro_archive(tmp_path: Path, top_dir: str = "kokoro-en-v0_19") -> Path:
    """Build a minimal valid Kokoro-layout archive."""
    archive = tmp_path / f"{top_dir}.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        for fname, data in [
            (f"{top_dir}/model.onnx", b"\x00" * 100),
            (f"{top_dir}/tokens.txt", b"a\nb\nc\n"),
            (f"{top_dir}/voices.bin", b"\x00" * 50),
        ]:
            info = tarfile.TarInfo(name=fname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive


class TestSherpaInstall:
    def test_unknown_model_id_returns_nonzero(self, fake_models_dir):
        rc = cli._sherpa_install("not-a-real-model", assume_yes=True)
        assert rc == 2

    def test_user_says_no_aborts(self, fake_models_dir, monkeypatch):
        with patch("builtins.input", return_value="n"), \
             patch.object(cli, "_download_with_progress") as mock_dl:
            rc = cli._sherpa_install("kokoro-en-v0_19", assume_yes=False)
        assert rc == 0
        mock_dl.assert_not_called()

    def test_already_installed_short_circuits(self, fake_models_dir, monkeypatch):
        # Pre-create a complete kokoro layout
        target = fake_models_dir / "kokoro-en-v0_19"
        target.mkdir(parents=True)
        (target / "model.onnx").write_bytes(b"")
        (target / "tokens.txt").write_text("")
        (target / "voices.bin").write_bytes(b"")

        with patch.object(cli, "_download_with_progress") as mock_dl, \
             patch("builtins.input") as mock_input:
            rc = cli._sherpa_install("kokoro-en-v0_19", assume_yes=False)
        assert rc == 0
        mock_dl.assert_not_called()
        mock_input.assert_not_called()

    def test_sha256_mismatch_aborts_and_cleans_up(self, fake_models_dir, tmp_path):
        # Build a real archive but its SHA won't match the catalog's claimed SHA
        archive = _make_kokoro_archive(tmp_path)

        def fake_download(url, dest, expected_bytes):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read_bytes())
            return True

        with patch.object(cli, "_download_with_progress", side_effect=fake_download):
            rc = cli._sherpa_install("kokoro-en-v0_19", assume_yes=True)

        assert rc == 4  # SHA mismatch exit code
        # Bad download cleaned up
        bad = fake_models_dir / ".tmp" / "kokoro-en-v0_19.tar.bz2"
        assert not bad.exists(), "Bad download was not cleaned up after SHA mismatch"
        # Target dir was not created
        assert not (fake_models_dir / "kokoro-en-v0_19").exists()

    def test_successful_install_with_matching_sha(
        self, fake_models_dir, tmp_path, monkeypatch
    ):
        # Build a real archive and patch the catalog's SHA to match
        archive = _make_kokoro_archive(tmp_path)
        real_sha = cli._sha256_file(archive)

        # Make the catalog entry match this archive
        test_entry = dict(sherpa_catalog.CATALOG["kokoro-en-v0_19"])
        test_entry["sha256"] = real_sha
        test_entry["compressed_bytes"] = archive.stat().st_size
        monkeypatch.setitem(sherpa_catalog.CATALOG, "kokoro-en-v0_19", test_entry)

        def fake_download(url, dest, expected_bytes):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read_bytes())
            return True

        with patch.object(cli, "_download_with_progress", side_effect=fake_download):
            rc = cli._sherpa_install("kokoro-en-v0_19", assume_yes=True)

        assert rc == 0
        # Layout files arrived in the right place
        target = fake_models_dir / "kokoro-en-v0_19"
        assert target.is_dir()
        assert (target / "model.onnx").is_file()
        assert (target / "tokens.txt").is_file()
        assert (target / "voices.bin").is_file()
        # Tmp archive cleaned up
        assert not (fake_models_dir / ".tmp" / "kokoro-en-v0_19.tar.bz2").exists()
        # Layout detected as kokoro
        assert cli._detect_sherpa_layout(target) == "kokoro"

    def test_download_failure_returns_nonzero(self, fake_models_dir):
        with patch.object(cli, "_download_with_progress", return_value=False):
            rc = cli._sherpa_install("kokoro-en-v0_19", assume_yes=True)
        assert rc == 3
