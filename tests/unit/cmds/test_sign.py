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

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509 import CertificateBuilder, Name, NameAttribute
from cryptography.x509.oid import NameOID
from ota_image_libs._crypto.x509_utils import X5cX509CertChain
from ota_image_libs.common import Sha256Digest
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.index_jwt.utils import (
    decode_index_jwt_with_verification,
    get_index_jwt_sign_cert_chain,
)

from ota_image_builder.cmds.sign import (
    _add_compat_to_image,
    _generate_dummy_metadata_jwt,
    load_cert_chains,
    sign_cmd,
    sign_image,
)


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

    def test_sign_image_not_finalized_exits(self, tmp_path: Path, mocker):
        """Test that signing non-finalized image exits."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()

        mock_cert_chain = mocker.MagicMock()
        key_pem = _generate_test_cert_and_key()[1]

        mock_helper = mocker.MagicMock()
        mock_helper.image_index.image_finalized = False
        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper", return_value=mock_helper
        )

        with pytest.raises(SystemExit):
            sign_image(
                image_root,
                sign_cert_chain=mock_cert_chain,
                sign_key=key_pem,
                force_sign=False,
            )

    def test_sign_image_success(self, tmp_path: Path, mocker):
        """Test successful image signing."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()

        mock_cert_chain = mocker.MagicMock()
        key_pem = _generate_test_cert_and_key()[1]

        mock_helper = mocker.MagicMock()
        mock_helper.image_index.image_finalized = True
        mock_helper.sync_index.return_value = (None, b"test_descriptor")
        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper", return_value=mock_helper
        )
        mocker.patch(
            "ota_image_builder.cmds.sign.compose_index_jwt",
            return_value="test.jwt.content",
        )

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

    def test_nonexistent_sign_cert_exits(self, tmp_path: Path, mocker):
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

        mocker.patch(
            "ota_image_builder.cmds.sign.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            sign_cmd(args)

    def test_nonexistent_ca_cert_exits(self, tmp_path: Path, mocker):
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

        mocker.patch(
            "ota_image_builder.cmds.sign.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            sign_cmd(args)

    def test_load_cert_chain_failure_exits(self, tmp_path: Path, mocker):
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

        mocker.patch(
            "ota_image_builder.cmds.sign.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            sign_cmd(args)


def _generate_encrypted_test_cert_and_key(
    passphrase: bytes,
) -> tuple[bytes, bytes]:
    """Helper that generates a self-signed cert and a passphrase-encrypted EC key."""
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
        Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(passphrase)
    )
    return cert_pem, key_pem


def _generate_test_ca_and_ee_chain() -> tuple[bytes, bytes, bytes]:
    """Generate a self-signed CA + a CA-issued EE cert + the EE private key (PEM, unencrypted)."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_subject = Name(
        [
            NameAttribute(NameOID.COUNTRY_NAME, "JP"),
            NameAttribute(NameOID.ORGANIZATION_NAME, "Test CA"),
            NameAttribute(NameOID.COMMON_NAME, "Test Root CA"),
        ]
    )
    not_before = datetime.datetime.now(datetime.timezone.utc)
    not_after = not_before + datetime.timedelta(days=365)
    ca_cert = (
        CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(ca_key, hashes.SHA256())
    )

    ee_key = ec.generate_private_key(ec.SECP256R1())
    ee_subject = Name(
        [
            NameAttribute(NameOID.COUNTRY_NAME, "JP"),
            NameAttribute(NameOID.ORGANIZATION_NAME, "Test Org"),
            NameAttribute(NameOID.COMMON_NAME, "Test EE"),
        ]
    )
    ee_cert = (
        CertificateBuilder()
        .subject_name(ee_subject)
        .issuer_name(ca_subject)
        .public_key(ee_key.public_key())
        .serial_number(2)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(ca_key, hashes.SHA256())
    )

    return (
        ca_cert.public_bytes(Encoding.PEM),
        ee_cert.public_bytes(Encoding.PEM),
        ee_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()),
    )


class TestEncryptedPrivateKey:
    """Cryptography compatibility: passphrase-encrypted PKCS8 EC key handling.

    Reading the key itself only returns bytes, but the password is passed
    through to `cryptography.hazmat.primitives.serialization.load_pem_private_key`
    inside `_generate_dummy_metadata_jwt` and `compose_index_jwt`. These tests
    pin that behavior so a `cryptography` upgrade can't silently regress it.
    """

    PASSPHRASE = b"test-passphrase-1234"

    def test_dummy_metadata_jwt_with_correct_passphrase(self):
        cert_pem, key_pem = _generate_encrypted_test_cert_and_key(self.PASSPHRASE)

        result = _generate_dummy_metadata_jwt(
            sign_cert_bytes=cert_pem,
            sign_key=key_pem,
            sign_key_passwd=self.PASSPHRASE,
        )

        assert len(result.split(".")) == 3

    def test_dummy_metadata_jwt_with_wrong_passphrase_raises(self):
        cert_pem, key_pem = _generate_encrypted_test_cert_and_key(self.PASSPHRASE)

        with pytest.raises(ValueError):
            _generate_dummy_metadata_jwt(
                sign_cert_bytes=cert_pem,
                sign_key=key_pem,
                sign_key_passwd=b"wrong-passphrase",
            )

    def test_dummy_metadata_jwt_with_missing_passphrase_raises(self):
        cert_pem, key_pem = _generate_encrypted_test_cert_and_key(self.PASSPHRASE)

        with pytest.raises(TypeError):
            _generate_dummy_metadata_jwt(
                sign_cert_bytes=cert_pem,
                sign_key=key_pem,
                sign_key_passwd=None,
            )

    def test_add_compat_to_image_with_encrypted_key(self, tmp_path: Path):
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        cert_pem, key_pem = _generate_encrypted_test_cert_and_key(self.PASSPHRASE)

        _add_compat_to_image(
            image_root,
            cert_bytes=cert_pem,
            sign_key=key_pem,
            sign_key_passwd=self.PASSPHRASE,
        )

        assert (image_root / "metadata.jwt").exists()
        assert (image_root / "certificate.pem").exists()
        assert len((image_root / "metadata.jwt").read_text().split(".")) == 3


class TestSignImageRealJWTRoundTrip:
    """End-to-end signing test that exercises the real cryptography path
    (no mocking of `compose_index_jwt`).

    `ImageIndexHelper` is mocked so we don't need a full OTA image on disk,
    but the produced `index.jwt` is decoded and verified using the real
    cryptography-backed JWS verifier. This catches regressions in:
      - PEM EC private key loading
      - DER serialization of x509 certs into the x5c header
      - ES256 sign / verify round-trip
    across cryptography version upgrades.
    """

    @staticmethod
    def _patch_index_helper(mocker, descriptor: ImageIndex.Descriptor):
        mock_helper = mocker.MagicMock()
        mock_helper.image_index.image_finalized = True
        mock_helper.sync_index.return_value = (None, descriptor)
        return mock_helper

    def test_real_jwt_round_trip_unencrypted_key(self, tmp_path: Path, mocker):
        from cryptography.x509 import load_pem_x509_certificate

        _, ee_pem, ee_key_pem = _generate_test_ca_and_ee_chain()

        cert_chain = X5cX509CertChain()
        cert_chain.add_ee(load_pem_x509_certificate(ee_pem))

        image_root = tmp_path / "ota_image"
        image_root.mkdir()

        descriptor = ImageIndex.Descriptor(
            digest=Sha256Digest("a" * 64),
            size=42,
        )

        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=self._patch_index_helper(mocker, descriptor),
        )
        sign_image(
            image_root,
            sign_cert_chain=cert_chain,
            sign_key=ee_key_pem,
            force_sign=False,
        )

        jwt_str = (image_root / "index.jwt").read_text()
        extracted_chain = get_index_jwt_sign_cert_chain(jwt_str)
        assert extracted_chain.ee.subject == cert_chain.ee.subject

        claims = decode_index_jwt_with_verification(jwt_str, extracted_chain)
        assert claims.image_index.digest == descriptor.digest
        assert claims.image_index.size == descriptor.size

    def test_real_jwt_round_trip_encrypted_key(self, tmp_path: Path, mocker):
        passphrase = b"another-strong-passphrase"

        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_subject = Name([NameAttribute(NameOID.COMMON_NAME, "Test Root CA")])
        not_before = datetime.datetime.now(datetime.timezone.utc)
        not_after = not_before + datetime.timedelta(days=365)
        (
            CertificateBuilder()
            .subject_name(ca_subject)
            .issuer_name(ca_subject)
            .public_key(ca_key.public_key())
            .serial_number(1)
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .sign(ca_key, hashes.SHA256())
        )
        ee_key = ec.generate_private_key(ec.SECP256R1())
        ee_subject = Name([NameAttribute(NameOID.COMMON_NAME, "Test EE")])
        ee_cert = (
            CertificateBuilder()
            .subject_name(ee_subject)
            .issuer_name(ca_subject)
            .public_key(ee_key.public_key())
            .serial_number(2)
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .sign(ca_key, hashes.SHA256())
        )
        ee_key_pem = ee_key.private_bytes(
            Encoding.PEM,
            PrivateFormat.PKCS8,
            BestAvailableEncryption(passphrase),
        )

        cert_chain = X5cX509CertChain()
        cert_chain.add_ee(ee_cert)

        image_root = tmp_path / "ota_image"
        image_root.mkdir()

        descriptor = ImageIndex.Descriptor(
            digest=Sha256Digest("b" * 64),
            size=128,
        )

        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=self._patch_index_helper(mocker, descriptor),
        )
        sign_image(
            image_root,
            sign_cert_chain=cert_chain,
            sign_key=ee_key_pem,
            sign_key_passwd=passphrase,
            force_sign=False,
        )

        jwt_str = (image_root / "index.jwt").read_text()
        extracted_chain = get_index_jwt_sign_cert_chain(jwt_str)
        claims = decode_index_jwt_with_verification(jwt_str, extracted_chain)
        assert claims.image_index.digest == descriptor.digest
        assert claims.image_index.size == descriptor.size
