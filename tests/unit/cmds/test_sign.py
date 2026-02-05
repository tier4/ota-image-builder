# Copyright 2025 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for cmds/sign.py module."""

from __future__ import annotations

import datetime
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509 import CertificateBuilder, Name, NameAttribute
from cryptography.x509.oid import NameOID

from ota_image_builder.cmds.sign import (
    _add_compat_to_image,
    _generate_dummy_metadata_jwt,
    _load_private_key,
    load_cert_chains,
    sign_cmd,
    sign_image,
)


class TestLoadPrivateKey:
    """Tests for _load_private_key function."""

    SAMPLE_PEM_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgevZzL1gdAFr88hb2
OF/2NxApJCzGCEDdfSp6VQO30hyhRANCAAQRWz+jn65BtOMvdyHKcvjBeBSDZH2r
1RTwjmYSi9R/zpBnuQ4EiMnCqfMPWiZqB4QdbAd0E7oH50VpuZ1P087G
-----END PRIVATE KEY-----"""

    def test_load_from_pem_string(self):
        """Test loading private key from PEM string directly."""
        result = _load_private_key(self.SAMPLE_PEM_KEY)

        assert result == self.SAMPLE_PEM_KEY.encode()

    def test_load_from_file(self, tmp_path: Path):
        """Test loading private key from file path."""
        key_file = tmp_path / "private.pem"
        key_file.write_text(self.SAMPLE_PEM_KEY)

        result = _load_private_key(str(key_file))

        assert result == self.SAMPLE_PEM_KEY.encode()

    def test_empty_input_exits(self):
        """Test that empty input causes SystemExit."""
        with pytest.raises(SystemExit):
            _load_private_key("")

    def test_none_input_exits(self):
        """Test that None input causes SystemExit."""
        with pytest.raises(SystemExit):
            _load_private_key(None)

    def test_nonexistent_file_exits(self, tmp_path: Path):
        """Test that non-existent file path causes SystemExit."""
        nonexistent = tmp_path / "nonexistent.pem"

        with pytest.raises(SystemExit):
            _load_private_key(str(nonexistent))

    def test_pem_string_starts_with_begin(self):
        """Test that strings starting with -----BEGIN are treated as PEM."""
        pem_like = (
            "-----BEGIN RSA PRIVATE KEY-----\ndata\n-----END RSA PRIVATE KEY-----"
        )

        result = _load_private_key(pem_like)

        assert result == pem_like.encode()


def _generate_test_cert_and_key() -> tuple[bytes, bytes]:
    """Helper to generate a test certificate and private key."""
    private_key = ec.generate_private_key(ec.SECP256R1())

    subject = issuer = Name(
        [
            NameAttribute(NameOID.COUNTRY_NAME, "JP"),
            NameAttribute(NameOID.ORGANIZATION_NAME, "Test Org"),
            NameAttribute(NameOID.COMMON_NAME, "Test Cert"),
        ]
    )

    cert = (
        CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(1000)
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .sign(private_key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(Encoding.PEM)
    key_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )

    return cert_pem, key_pem


class TestLoadCertChains:
    """Tests for load_cert_chains function."""

    def test_load_single_cert(self, tmp_path: Path):
        """Test loading a single end-entity certificate."""
        cert_pem, _ = _generate_test_cert_and_key()
        cert_file = tmp_path / "cert.pem"
        cert_file.write_bytes(cert_pem)

        result = load_cert_chains(cert_file, [])

        assert result.ee is not None
        assert len(result.interms) == 0

    def test_load_with_root_ca_raises_error(self, tmp_path: Path):
        """Test that loading root CA as intermediate cert raises ValueError.

        Self-signed certificates are treated as root CAs and cannot be added
        as intermediate certificates.
        """
        ee_cert_pem, _ = _generate_test_cert_and_key()
        ca_cert_pem, _ = _generate_test_cert_and_key()

        ee_file = tmp_path / "ee.pem"
        ca_file = tmp_path / "ca.pem"
        ee_file.write_bytes(ee_cert_pem)
        ca_file.write_bytes(ca_cert_pem)

        with pytest.raises(ValueError, match="Reject adding root CA into cert chain"):
            load_cert_chains(ee_file, [ca_file])


class TestGenerateDummyMetadataJwt:
    """Tests for _generate_dummy_metadata_jwt function."""

    def test_generates_jwt_format(self, tmp_path: Path):
        """Test that the output is in JWT format (3 parts separated by dots)."""
        cert_pem, key_pem = _generate_test_cert_and_key()

        result = _generate_dummy_metadata_jwt(
            sign_cert_bytes=cert_pem,
            sign_key=key_pem,
        )

        parts = result.split(".")
        assert len(parts) == 3  # header.payload.signature

    def test_jwt_parts_are_base64(self, tmp_path: Path):
        """Test that JWT parts are base64 encoded."""
        import base64

        cert_pem, key_pem = _generate_test_cert_and_key()

        result = _generate_dummy_metadata_jwt(
            sign_cert_bytes=cert_pem,
            sign_key=key_pem,
        )

        parts = result.split(".")
        # Should be able to decode all parts (with padding)
        for part in parts:
            # Add padding if needed
            padded = part + "=" * (4 - len(part) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            assert decoded is not None


class TestSignImage:
    """Tests for sign_image function."""

    def test_sign_image_not_finalized_exits(self, tmp_path: Path):
        """Test that signing non-finalized image exits."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()

        mock_cert_chain = MagicMock()
        key_pem = _generate_test_cert_and_key()[1]

        with patch("ota_image_builder.cmds.sign.ImageIndexHelper") as mock_helper_class:
            mock_helper = MagicMock()
            mock_helper.image_index.image_finalized = False
            mock_helper_class.return_value = mock_helper

            with pytest.raises(SystemExit):
                sign_image(
                    image_root,
                    sign_cert_chain=mock_cert_chain,
                    sign_key=key_pem,
                    force_sign=False,
                )

    def test_sign_image_success(self, tmp_path: Path):
        """Test successful image signing."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()

        mock_cert_chain = MagicMock()
        key_pem = _generate_test_cert_and_key()[1]

        with patch("ota_image_builder.cmds.sign.ImageIndexHelper") as mock_helper_class:
            mock_helper = MagicMock()
            mock_helper.image_index.image_finalized = True
            mock_helper.sync_index.return_value = (None, b"test_descriptor")
            mock_helper_class.return_value = mock_helper

            with patch(
                "ota_image_builder.cmds.sign.compose_index_jwt",
                return_value="test.jwt.content",
            ):
                sign_image(
                    image_root,
                    sign_cert_chain=mock_cert_chain,
                    sign_key=key_pem,
                    force_sign=False,
                )

                # Check that index.jwt was written
                jwt_file = image_root / "index.jwt"
                assert jwt_file.exists()
                assert jwt_file.read_text() == "test.jwt.content"


class TestAddCompatToImage:
    """Tests for _add_compat_to_image function."""

    def test_adds_compat_files(self, tmp_path: Path):
        """Test that legacy compat files are added to image."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()

        cert_pem, key_pem = _generate_test_cert_and_key()

        _add_compat_to_image(
            image_root,
            cert_bytes=cert_pem,
            sign_key=key_pem,
        )

        # Check files were created
        metadata_jwt = image_root / "metadata.jwt"
        certificate = image_root / "certificate.pem"

        assert metadata_jwt.exists()
        assert certificate.exists()
        assert certificate.read_bytes() == cert_pem
        # metadata.jwt should be JWT format
        assert len(metadata_jwt.read_text().split(".")) == 3


class TestLoadPrivateKeyExtended:
    """Extended tests for _load_private_key function."""

    def test_read_exception_exits(self, tmp_path: Path):
        """Test that unreadable file causes SystemExit."""
        # Create a directory instead of a file
        key_path = tmp_path / "key_dir"
        key_path.mkdir()

        with pytest.raises(SystemExit):
            _load_private_key(str(key_path))


class TestSignCmd:
    """Tests for sign_cmd function."""

    def test_invalid_ota_image_exits(self, tmp_path: Path):
        """Test that invalid OTA image directory causes SystemExit."""
        image_root = tmp_path / "invalid_image"
        image_root.mkdir()
        cert_file = tmp_path / "cert.pem"
        key_file = tmp_path / "key.pem"

        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_file),
            ca_cert=[],
            sign_key=str(key_file),
            sign_key_passwd=None,
            legacy_compat=False,
        )

        with pytest.raises(SystemExit):
            sign_cmd(args)

    def test_nonexistent_sign_cert_exits(self, tmp_path: Path):
        """Test that non-existent sign cert causes SystemExit."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        cert_file = tmp_path / "nonexistent.pem"
        key_file = tmp_path / "key.pem"

        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_file),
            ca_cert=[],
            sign_key=str(key_file),
            sign_key_passwd=None,
            legacy_compat=False,
        )

        with patch(
            "ota_image_builder.cmds.sign.check_if_valid_ota_image",
            return_value=True,
        ):
            with pytest.raises(SystemExit):
                sign_cmd(args)

    def test_nonexistent_ca_cert_exits(self, tmp_path: Path):
        """Test that non-existent CA cert causes SystemExit."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        cert_pem, _ = _generate_test_cert_and_key()
        cert_file = tmp_path / "cert.pem"
        cert_file.write_bytes(cert_pem)
        ca_cert_file = tmp_path / "nonexistent_ca.pem"
        key_file = tmp_path / "key.pem"

        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_file),
            ca_cert=[str(ca_cert_file)],
            sign_key=str(key_file),
            sign_key_passwd=None,
            legacy_compat=False,
        )

        with patch(
            "ota_image_builder.cmds.sign.check_if_valid_ota_image",
            return_value=True,
        ):
            with pytest.raises(SystemExit):
                sign_cmd(args)

    def test_load_cert_chain_failure_exits(self, tmp_path: Path):
        """Test that cert chain loading failure causes SystemExit."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        # Create invalid cert file
        cert_file = tmp_path / "invalid.pem"
        cert_file.write_text("not a valid cert")
        key_file = tmp_path / "key.pem"
        key_file.write_text("not a valid key")

        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_file),
            ca_cert=[],
            sign_key=str(key_file),
            sign_key_passwd=None,
            legacy_compat=False,
        )

        with patch(
            "ota_image_builder.cmds.sign.check_if_valid_ota_image",
            return_value=True,
        ):
            with pytest.raises(SystemExit):
                sign_cmd(args)
