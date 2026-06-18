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
"""Unit tests for cmds/aws_kms_sign.py module."""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)
from cryptography.x509 import (
    CertificateBuilder,
    Name,
    NameAttribute,
    load_pem_x509_certificate,
)
from cryptography.x509.oid import NameOID
from ota_image_libs._crypto.aws_kms import AWSKMSSignAlgorithm
from ota_image_libs._crypto.x509_utils import X5cX509CertChain
from ota_image_libs.common import Sha256Digest
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.index_jwt.utils import (
    decode_index_jwt_with_verification,
    get_index_jwt_sign_cert_chain,
)
from pydantic import ValidationError

from ota_image_builder.cmds.aws_kms_sign import (
    AWSKMSSignResponse,
    SignWithAWSKMSInput,
    compose_signed_jwt_from_kms_response,
    sign_with_aws_kms_finish_cmd,
    sign_with_aws_kms_finish_cmd_args,
    sign_with_aws_kms_prepare,
    sign_with_aws_kms_prepare_cmd,
    sign_with_aws_kms_prepare_cmd_args,
)

KMS_MESSAGE_TYPE_DIGEST = "DIGEST"

# ------ test helpers ------ #


def _load_ec_private_key(key_pem: bytes) -> ec.EllipticCurvePrivateKey:
    key = load_pem_private_key(key_pem, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return key


def _generate_test_ca_and_ee_chain() -> tuple[bytes, bytes, bytes]:
    """Generate a self-signed CA + a CA-issued EE cert + the EE private key (PEM)."""
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


def _make_ee_chain() -> X5cX509CertChain:
    _, ee_pem, _ = _generate_test_ca_and_ee_chain()
    chain = X5cX509CertChain()
    chain.add_ee(load_pem_x509_certificate(ee_pem))
    return chain


def _patch_index_helper(mocker, descriptor: ImageIndex.Descriptor):
    mock_helper = mocker.MagicMock()
    mock_helper.image_index.image_finalized = True
    mock_helper.sync_index.return_value = (None, descriptor)
    return mock_helper


def _simulate_kms_sign_response(
    signing_input: str, ee_key_pem: bytes, *, signing_algorithm: str = "ECDSA_SHA_256"
) -> dict[str, str]:
    """Sign the signing input with the EE private key, mimicking an AWS KMS response.

    Returns a JSON-shaped object as returned by the AWS KMS Sign REST API, where the
    DER signature is base64 encoded.
    """
    key = _load_ec_private_key(ee_key_pem)
    der_sig = key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    return {
        "KeyId": "arn:aws:kms:region:acct:key/dummy",  # extra field, must be ignored
        "Signature": base64.b64encode(der_sig).decode("ascii"),
        "SigningAlgorithm": signing_algorithm,
    }


def _make_real_unsigned_jwt(
    mocker,
    descriptor: ImageIndex.Descriptor | None = None,
) -> tuple[str, bytes]:
    """Produce a real unsigned JWT (ES256 header) plus the EE private key matching the
    cert embedded in it, by running sign_with_aws_kms_prepare with a mocked
    ImageIndexHelper (no on-disk OTA image needed).
    """
    descriptor = descriptor or ImageIndex.Descriptor(
        digest=Sha256Digest("d" * 64), size=512
    )
    _, ee_pem, ee_key_pem = _generate_test_ca_and_ee_chain()
    chain = X5cX509CertChain()
    chain.add_ee(load_pem_x509_certificate(ee_pem))
    mocker.patch(
        "ota_image_builder.cmds.sign.ImageIndexHelper",
        return_value=_patch_index_helper(mocker, descriptor),
    )
    raw = sign_with_aws_kms_prepare(
        Path("/unused"), sign_cert_chain=chain, force_sign=False
    )
    return raw.jwt_payload_unsigned, ee_key_pem


# ------ sign_with_aws_kms_prepare ------ #


class TestSignWithAWSKMSPrepare:
    """Tests for the sign_with_aws_kms_prepare core function."""

    def test_not_finalized_exits(self, tmp_path: Path, mocker):
        """sign_with_aws_kms_prepare must reject an image that has not been finalized."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()

        mock_helper = mocker.MagicMock()
        mock_helper.image_index.image_finalized = False
        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper", return_value=mock_helper
        )

        with pytest.raises(SystemExit):
            sign_with_aws_kms_prepare(
                image_root, sign_cert_chain=_make_ee_chain(), force_sign=False
            )
        # must not advance to mutating the signing state
        mock_helper.image_index.finalize_signing_image.assert_not_called()

    def test_output_schema(self, tmp_path: Path, mocker):
        """sign_with_aws_kms_prepare returns a SignWithAWSKMSPrepareOutput with the documented content."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        descriptor = ImageIndex.Descriptor(digest=Sha256Digest("a" * 64), size=42)

        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=_patch_index_helper(mocker, descriptor),
        )
        result = sign_with_aws_kms_prepare(
            image_root, sign_cert_chain=_make_ee_chain(), force_sign=False
        )

        assert result.schema_ver == 1
        assert result.aws_kms_sign_request.message_type == KMS_MESSAGE_TYPE_DIGEST
        # signing_algorithm is validated/typed as the AWSKMSSignAlgorithm enum.
        assert (
            result.aws_kms_sign_request.signing_algorithm
            is AWSKMSSignAlgorithm.ECDSA_SHA_256
        )

        # JWTPayloadUnsigned is the `header.payload` (two base64url segments).
        unsigned = result.jwt_payload_unsigned
        assert unsigned.count(".") == 1

        # Message must be base64(SHA-256(signing_input)) for MessageType=DIGEST.
        expected_digest = hashlib.sha256(unsigned.encode("ascii")).digest()
        assert base64.b64decode(result.aws_kms_sign_request.message) == expected_digest

    def test_wire_serialization_uses_aliases(self, tmp_path: Path, mocker):
        """The default serialization uses the PascalCase aliases (serialize_by_alias)."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        descriptor = ImageIndex.Descriptor(digest=Sha256Digest("c" * 64), size=7)

        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=_patch_index_helper(mocker, descriptor),
        )
        result = sign_with_aws_kms_prepare(
            image_root, sign_cert_chain=_make_ee_chain(), force_sign=False
        )

        wire = json.loads(result.model_dump_json())
        assert set(wire) == {
            "AWSKMSSignRequestTemplate",
            "JWTPayloadUnsigned",
            "SchemaVer",
        }
        assert wire["SchemaVer"] == 1
        assert set(wire["AWSKMSSignRequestTemplate"]) == {
            "Message",
            "MessageType",
            "SigningAlgorithm",
        }
        # StrEnum serializes to its plain string value.
        assert wire["AWSKMSSignRequestTemplate"]["SigningAlgorithm"] == "ECDSA_SHA_256"

    def test_unsupported_aws_algorithm_exits(self, tmp_path: Path, mocker):
        """The defensive guard exits if the lib returns an unmappable algorithm."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        descriptor = ImageIndex.Descriptor(digest=Sha256Digest("a" * 64), size=42)

        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=_patch_index_helper(mocker, descriptor),
        )
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign."
            "compose_unsigned_index_jwt_for_aws_kms_sign",
            return_value=("BOGUS_ALG", "header.payload"),
        )
        with pytest.raises(SystemExit):
            sign_with_aws_kms_prepare(
                image_root, sign_cert_chain=_make_ee_chain(), force_sign=False
            )


class TestSignWithAWSKMSPrepareCmd:
    """Tests for the sign_with_aws_kms_prepare_cmd handler (arg validation)."""

    def test_invalid_ota_image_exits(self, tmp_path: Path):
        image_root = tmp_path / "invalid"
        image_root.mkdir()
        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(tmp_path / "cert.pem"),
            ca_cert=None,
            force_sign=False,
        )
        with pytest.raises(SystemExit):
            sign_with_aws_kms_prepare_cmd(args)

    def test_nonexistent_sign_cert_exits(self, tmp_path: Path, mocker):
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(tmp_path / "nope.pem"),
            ca_cert=None,
            force_sign=False,
        )
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            sign_with_aws_kms_prepare_cmd(args)

    def test_nonexistent_ca_cert_exits(self, tmp_path: Path, mocker):
        _, ee_pem, _ = _generate_test_ca_and_ee_chain()
        cert_f = tmp_path / "ee.pem"
        cert_f.write_bytes(ee_pem)
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_f),
            ca_cert=[str(tmp_path / "missing_ca.pem")],
            force_sign=False,
        )
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            sign_with_aws_kms_prepare_cmd(args)

    def test_existing_ca_cert_is_iterated(self, tmp_path: Path, mocker):
        """An existing --ca-cert passes the file check (loop iterates the truthy path)."""
        ca_pem, ee_pem, _ = _generate_test_ca_and_ee_chain()
        cert_f = tmp_path / "ee.pem"
        cert_f.write_bytes(ee_pem)
        ca_f = tmp_path / "ca.pem"
        ca_f.write_bytes(ca_pem)
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_f),
            ca_cert=[str(ca_f)],
            force_sign=False,
        )
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.check_if_valid_ota_image",
            return_value=True,
        )
        # The CA file exists (loop iterates past the is_file check), but a
        # self-signed root is rejected by load_cert_chains -> SystemExit.
        with pytest.raises(SystemExit):
            sign_with_aws_kms_prepare_cmd(args)

    def test_cert_chain_load_failure_exits(self, tmp_path: Path, mocker):
        cert_f = tmp_path / "bad.pem"
        cert_f.write_text("not a valid cert")
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_f),
            ca_cert=None,
            force_sign=False,
        )
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            sign_with_aws_kms_prepare_cmd(args)

    def test_success_prints_json(self, tmp_path: Path, capsys, mocker):
        _, ee_pem, _ = _generate_test_ca_and_ee_chain()
        cert_f = tmp_path / "ee.pem"
        cert_f.write_bytes(ee_pem)
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        descriptor = ImageIndex.Descriptor(digest=Sha256Digest("a" * 64), size=42)
        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_f),
            ca_cert=None,
            force_sign=False,
        )
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.check_if_valid_ota_image",
            return_value=True,
        )
        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=_patch_index_helper(mocker, descriptor),
        )
        sign_with_aws_kms_prepare_cmd(args)

        # stdout must be the well-formed JSON output object only.
        parsed = json.loads(capsys.readouterr().out)
        assert set(parsed) == {
            "AWSKMSSignRequestTemplate",
            "JWTPayloadUnsigned",
            "SchemaVer",
        }
        assert parsed["SchemaVer"] == 1

    def test_runtime_error_during_finalize_exits(self, tmp_path: Path, mocker):
        """A non-exit error from the core routine is reported and exits non-zero."""
        _, ee_pem, _ = _generate_test_ca_and_ee_chain()
        cert_f = tmp_path / "ee.pem"
        cert_f.write_bytes(ee_pem)
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        args = Namespace(
            image_root=str(image_root),
            sign_cert=str(cert_f),
            ca_cert=None,
            force_sign=False,
        )
        helper = mocker.MagicMock()
        helper.image_index.image_finalized = True
        # e.g. already-signed image without --force-sign raises ValueError.
        helper.image_index.finalize_signing_image.side_effect = ValueError(
            "already signed"
        )
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.check_if_valid_ota_image",
            return_value=True,
        )
        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=helper,
        )
        with pytest.raises(SystemExit):
            sign_with_aws_kms_prepare_cmd(args)


# ------ SignWithAWSKMSInput schema validation ------ #


class TestSignWithAWSKMSInputValidation:
    """Schema validation of the sign-with-aws-kms-finish input (via model_validate_json).

    The command parses + validates the raw JSON text with
    ``SignWithAWSKMSInput.model_validate_json``; these tests pin that behaviour
    (alias + extra-field handling on success, ValidationError on bad input).
    """

    @staticmethod
    def _payload(**response_fields: str) -> str:
        return json.dumps(
            {
                "JWTPayloadUnsigned": "aaa.bbb",
                "AWSKMSSignResponse": response_fields,
            }
        )

    def test_parses_wire_aliases_and_ignores_extra(self):
        model = SignWithAWSKMSInput.model_validate_json(
            json.dumps(
                {
                    "JWTPayloadUnsigned": "aaa.bbb",
                    "AWSKMSSignResponse": {
                        "KeyId": "arn:aws:kms:dummy",  # extra -> ignored
                        "Signature": "c2ln",
                        "SigningAlgorithm": "ECDSA_SHA_256",
                    },
                }
            )
        )
        assert model.jwt_payload_unsigned == "aaa.bbb"
        assert model.aws_kms_sign_response.signature == "c2ln"
        assert (
            model.aws_kms_sign_response.signing_algorithm
            is AWSKMSSignAlgorithm.ECDSA_SHA_256
        )

    def test_missing_jwt_payload_raises(self):
        with pytest.raises(ValidationError):
            SignWithAWSKMSInput.model_validate_json(
                json.dumps(
                    {
                        "AWSKMSSignResponse": {
                            "Signature": "c2ln",
                            "SigningAlgorithm": "ECDSA_SHA_256",
                        }
                    }
                )
            )

    def test_missing_kms_response_raises(self):
        with pytest.raises(ValidationError):
            SignWithAWSKMSInput.model_validate_json(
                json.dumps({"JWTPayloadUnsigned": "aaa.bbb"})
            )

    def test_missing_signature_raises(self):
        with pytest.raises(ValidationError):
            SignWithAWSKMSInput.model_validate_json(
                self._payload(SigningAlgorithm="ECDSA_SHA_256")
            )

    def test_missing_signing_algorithm_raises(self):
        with pytest.raises(ValidationError):
            SignWithAWSKMSInput.model_validate_json(self._payload(Signature="c2ln"))

    def test_unsupported_algorithm_raises(self):
        """An unsupported SigningAlgorithm is rejected by the enum field."""
        with pytest.raises(ValidationError):
            SignWithAWSKMSInput.model_validate_json(
                self._payload(
                    Signature="c2ln", SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256"
                )
            )

    def test_malformed_json_raises(self):
        with pytest.raises(ValidationError):
            SignWithAWSKMSInput.model_validate_json("{not json}")

    def test_non_object_json_raises(self):
        with pytest.raises(ValidationError):
            SignWithAWSKMSInput.model_validate_json("[1, 2, 3]")


# ------ compose_signed_jwt_from_kms_response ------ #


class TestComposeSignedJwtFromKmsResponse:
    """Tests for compose_signed_jwt_from_kms_response error handling."""

    def test_bad_base64_signature_exits(self, mocker):
        """A Signature that is not valid base64 is rejected (base64 validate=True)."""
        # A real ES256 signing input so the algorithm guard passes and we reach the
        # base64-decode step.
        signing_input, _ = _make_real_unsigned_jwt(mocker)
        model = SignWithAWSKMSInput(
            JWTPayloadUnsigned=signing_input,
            AWSKMSSignResponse=AWSKMSSignResponse(
                Signature="!!!not-base64!!!",
                SigningAlgorithm=AWSKMSSignAlgorithm.ECDSA_SHA_256,
            ),
        )
        with pytest.raises(SystemExit):
            compose_signed_jwt_from_kms_response(model)

    def test_garbage_der_signature_exits(self, mocker):
        """A validly-base64 but non-DER signature fails inside the compose step."""
        signing_input, _ = _make_real_unsigned_jwt(mocker)
        model = SignWithAWSKMSInput(
            JWTPayloadUnsigned=signing_input,
            AWSKMSSignResponse=AWSKMSSignResponse(
                Signature=base64.b64encode(b"not-a-der-signature").decode(),
                SigningAlgorithm=AWSKMSSignAlgorithm.ECDSA_SHA_256,
            ),
        )
        with pytest.raises(SystemExit):
            compose_signed_jwt_from_kms_response(model)

    def test_algorithm_mismatch_exits(self, capsys, mocker):
        """The response SigningAlgorithm must match the JWT header's algorithm.

        The JWT header is always ES256 (-> ECDSA_SHA_256). A response that advertises a
        different ECDSA algorithm must be rejected up front, even when the DER signature
        itself is a perfectly valid P-256 signature. Otherwise the DER->raw conversion
        would silently size the signature for the wrong curve and emit a structurally
        valid but cryptographically invalid index.jwt.
        """
        signing_input, ee_key_pem = _make_real_unsigned_jwt(mocker)
        # Valid P-256 signature, but mislabeled as ECDSA_SHA_512.
        kms_resp = _simulate_kms_sign_response(
            signing_input, ee_key_pem, signing_algorithm="ECDSA_SHA_512"
        )
        model = SignWithAWSKMSInput.model_validate_json(
            json.dumps(
                {"JWTPayloadUnsigned": signing_input, "AWSKMSSignResponse": kms_resp}
            )
        )
        with pytest.raises(SystemExit):
            compose_signed_jwt_from_kms_response(model)
        # Pinned to the algorithm-mismatch guard (not a later generic failure).
        assert "doesn't match" in capsys.readouterr().out

    def test_signature_from_wrong_key_fails_self_verification(self, capsys, mocker):
        """A structurally valid signature that does not verify against the signing cert
        embedded in the JWT must be rejected, so a broken index.jwt is never returned.
        """
        signing_input, _ = _make_real_unsigned_jwt(mocker)
        # Sign with a DIFFERENT key than the EE cert embedded in the signing input;
        # the algorithm still matches, so this only trips the self-verification guard.
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        wrong_key_pem = wrong_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
        kms_resp = _simulate_kms_sign_response(signing_input, wrong_key_pem)
        model = SignWithAWSKMSInput.model_validate_json(
            json.dumps(
                {"JWTPayloadUnsigned": signing_input, "AWSKMSSignResponse": kms_resp}
            )
        )
        with pytest.raises(SystemExit):
            compose_signed_jwt_from_kms_response(model)
        assert "self-verification" in capsys.readouterr().out

    def test_mispaired_payload_fails_self_verification(self, capsys, mocker):
        """A signature correctly produced for a DIFFERENT signing input (e.g. a stale
        sign-with-aws-kms-prepare output) must fail self-verification rather than be
        assembled into a mismatched index.jwt.
        """
        signing_input, ee_key_pem = _make_real_unsigned_jwt(
            mocker, ImageIndex.Descriptor(digest=Sha256Digest("a" * 64), size=1)
        )
        other_input, _ = _make_real_unsigned_jwt(
            mocker, ImageIndex.Descriptor(digest=Sha256Digest("b" * 64), size=2)
        )
        # Signature is over `other_input` but paired with `signing_input`.
        kms_resp = _simulate_kms_sign_response(other_input, ee_key_pem)
        model = SignWithAWSKMSInput.model_validate_json(
            json.dumps(
                {"JWTPayloadUnsigned": signing_input, "AWSKMSSignResponse": kms_resp}
            )
        )
        with pytest.raises(SystemExit):
            compose_signed_jwt_from_kms_response(model)
        assert "self-verification" in capsys.readouterr().out


# ------ sign_with_aws_kms_finish_cmd ------ #


class TestSignWithAwsKmsCmd:
    """Tests for the sign_with_aws_kms_finish_cmd handler."""

    def test_invalid_image_root_exits(self, tmp_path: Path, mocker):
        bad_root = tmp_path / "not_an_image"
        bad_root.mkdir()
        valid_input = {
            "JWTPayloadUnsigned": "aaa.bbb",
            "AWSKMSSignResponse": {
                "Signature": base64.b64encode(b"sig").decode(),
                "SigningAlgorithm": "ECDSA_SHA_256",
            },
        }
        args = Namespace(input=json.dumps(valid_input), image_root=str(bad_root))
        # Patch compose so we only exercise the (post-compose) image-root check.
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.compose_signed_jwt_from_kms_response",
            return_value="a.b.c",
        )
        with pytest.raises(SystemExit):
            sign_with_aws_kms_finish_cmd(args)

    def test_invalid_input_exits(self):
        """Well-formed JSON that fails schema validation exits cleanly (no traceback)."""
        # Missing AWSKMSSignResponse -> ValidationError -> exit_with_err_msg.
        args = Namespace(input='{"JWTPayloadUnsigned": "a.b"}', image_root=None)
        with pytest.raises(SystemExit):
            sign_with_aws_kms_finish_cmd(args)

    def test_self_verification_failure_does_not_write_index_jwt(
        self, tmp_path: Path, mocker
    ):
        """A JWT that fails self-verification must abort BEFORE index.jwt is written."""
        signing_input, _ = _make_real_unsigned_jwt(mocker)
        # Wrong signing key -> valid signature that won't verify against the embedded cert.
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        wrong_key_pem = wrong_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
        kms_resp = _simulate_kms_sign_response(signing_input, wrong_key_pem)

        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        input_obj = {
            "JWTPayloadUnsigned": signing_input,
            "AWSKMSSignResponse": kms_resp,
        }
        args = Namespace(input=json.dumps(input_obj), image_root=str(image_root))
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            sign_with_aws_kms_finish_cmd(args)

        # The broken index.jwt must NOT have been written into the OTA image.
        assert not (image_root / "index.jwt").exists()

    def test_prints_jwt_without_image_root(self, tmp_path: Path, capsys, mocker):
        """Without --image-root, the JWT is only printed (no index.jwt written)."""
        _, ee_pem, ee_key_pem = _generate_test_ca_and_ee_chain()
        chain = X5cX509CertChain()
        chain.add_ee(load_pem_x509_certificate(ee_pem))

        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        descriptor = ImageIndex.Descriptor(digest=Sha256Digest("d" * 64), size=512)

        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=_patch_index_helper(mocker, descriptor),
        )
        raw = sign_with_aws_kms_prepare(
            image_root, sign_cert_chain=chain, force_sign=False
        )

        signing_input = raw.jwt_payload_unsigned
        kms_resp = _simulate_kms_sign_response(signing_input, ee_key_pem)
        input_obj = {
            "JWTPayloadUnsigned": signing_input,
            "AWSKMSSignResponse": kms_resp,
        }

        args = Namespace(input=json.dumps(input_obj), image_root=None)
        sign_with_aws_kms_finish_cmd(args)

        signed_jwt = capsys.readouterr().out.strip()
        assert signed_jwt.count(".") == 2
        # nothing should be written to the image.
        assert not (image_root / "index.jwt").exists()

        extracted_chain = get_index_jwt_sign_cert_chain(signed_jwt)
        claims = decode_index_jwt_with_verification(signed_jwt, extracted_chain)
        assert claims.image_index.size == descriptor.size


# ------ arg-parser registration ------ #


class TestCmdArgsRegistration:
    """Verify the subcommands register their args and handlers correctly."""

    def test_sign_with_aws_kms_prepare_cmd_args(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        sign_with_aws_kms_prepare_cmd_args(sub)

        args = parser.parse_args(
            [
                "sign-with-aws-kms-prepare",
                "--sign-cert",
                "ee.pem",
                "--ca-cert",
                "ca1.pem",
                "--ca-cert",
                "ca2.pem",
                "--force-sign",
                "/img",
            ]
        )
        assert args.handler is sign_with_aws_kms_prepare_cmd
        assert args.sign_cert == "ee.pem"
        assert args.ca_cert == ["ca1.pem", "ca2.pem"]
        assert args.force_sign is True
        assert args.image_root == "/img"

    def test_sign_with_aws_kms_prepare_cmd_args_requires_sign_cert(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        sign_with_aws_kms_prepare_cmd_args(sub)
        # --sign-cert is required -> argparse exits.
        with pytest.raises(SystemExit):
            parser.parse_args(["sign-with-aws-kms-prepare", "/img"])

    def test_sign_with_aws_kms_finish_cmd_args(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        sign_with_aws_kms_finish_cmd_args(sub)

        args = parser.parse_args(
            ["sign-with-aws-kms-finish", "--image-root", "/img", "{}"]
        )
        assert args.handler is sign_with_aws_kms_finish_cmd
        assert args.input == "{}"
        assert args.image_root == "/img"

    def test_sign_with_aws_kms_finish_cmd_args_image_root_defaults_none(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        sign_with_aws_kms_finish_cmd_args(sub)

        args = parser.parse_args(["sign-with-aws-kms-finish", "{}"])
        assert args.image_root is None


# ------ full round trip ------ #


class TestRoundTrip:
    """End-to-end: sign-with-aws-kms-prepare -> simulate KMS -> sign-with-aws-kms-finish -> verify.

    Exercises the real cryptography path (no mocking of the compose helpers). The KMS
    Sign call is simulated locally with the EE private key, producing a DER ECDSA
    signature exactly as KMS would return (base64 encoded in the response JSON).
    """

    def test_full_round_trip(self, tmp_path: Path, mocker):
        _, ee_pem, ee_key_pem = _generate_test_ca_and_ee_chain()
        chain = X5cX509CertChain()
        chain.add_ee(load_pem_x509_certificate(ee_pem))

        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        descriptor = ImageIndex.Descriptor(digest=Sha256Digest("d" * 64), size=512)

        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=_patch_index_helper(mocker, descriptor),
        )
        raw = sign_with_aws_kms_prepare(
            image_root, sign_cert_chain=chain, force_sign=False
        )

        signing_input = raw.jwt_payload_unsigned
        kms_resp = _simulate_kms_sign_response(signing_input, ee_key_pem)

        input_model = SignWithAWSKMSInput.model_validate_json(
            json.dumps(
                {
                    "JWTPayloadUnsigned": signing_input,
                    "AWSKMSSignResponse": kms_resp,
                }
            )
        )
        signed_jwt = compose_signed_jwt_from_kms_response(input_model)

        assert signed_jwt.count(".") == 2
        assert signed_jwt.startswith(signing_input + ".")

        extracted_chain = get_index_jwt_sign_cert_chain(signed_jwt)
        assert extracted_chain.ee.subject == chain.ee.subject

        claims = decode_index_jwt_with_verification(signed_jwt, extracted_chain)
        assert claims.image_index.digest == descriptor.digest
        assert claims.image_index.size == descriptor.size

    def test_full_round_trip_writes_index_jwt(self, tmp_path: Path, mocker):
        """sign_with_aws_kms_finish_cmd writes index.jwt when --image-root is given."""
        _, ee_pem, ee_key_pem = _generate_test_ca_and_ee_chain()
        chain = X5cX509CertChain()
        chain.add_ee(load_pem_x509_certificate(ee_pem))

        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        descriptor = ImageIndex.Descriptor(digest=Sha256Digest("e" * 64), size=1024)

        mocker.patch(
            "ota_image_builder.cmds.sign.ImageIndexHelper",
            return_value=_patch_index_helper(mocker, descriptor),
        )
        raw = sign_with_aws_kms_prepare(
            image_root, sign_cert_chain=chain, force_sign=False
        )

        signing_input = raw.jwt_payload_unsigned
        kms_resp = _simulate_kms_sign_response(signing_input, ee_key_pem)
        input_obj = {
            "JWTPayloadUnsigned": signing_input,
            "AWSKMSSignResponse": kms_resp,
        }

        args = Namespace(input=json.dumps(input_obj), image_root=str(image_root))
        mocker.patch(
            "ota_image_builder.cmds.aws_kms_sign.check_if_valid_ota_image",
            return_value=True,
        )
        sign_with_aws_kms_finish_cmd(args)

        index_jwt_f = image_root / "index.jwt"
        assert index_jwt_f.is_file()

        written = index_jwt_f.read_text()
        extracted_chain = get_index_jwt_sign_cert_chain(written)
        claims = decode_index_jwt_with_verification(written, extracted_chain)
        assert claims.image_index.digest == descriptor.digest
        assert claims.image_index.size == descriptor.size
