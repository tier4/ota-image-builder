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
"""Support for signing an OTA image with the AWS KMS Sign API.

Two CLI subcommands are provided for the support:

1. ``gen-raw-jwt``: finalizes the image signing state, rewrites ``index.json`` and emits
   the unsigned JWT signing input (``header.payload``) together with a ready-to-fill
   AWS KMS ``Sign`` request template.
2. ``sign-with-aws-kms``: takes that unsigned signing input plus the AWS KMS ``Sign``
   response and assembles the complete, signed ``index.jwt``.

Reference: https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ota_image_libs._crypto.aws_kms import (
    AWSKMSSignAlgorithm,
    compose_jwt_from_aws_kms_sign_response,
)
from ota_image_libs.v1.consts import INDEX_JWT_FNAME
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.index_jwt.utils import (
    compose_unsigned_index_jwt_for_aws_kms_sign,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg
from ota_image_builder.cmds.sign import load_cert_chains

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction

    from ota_image_libs._crypto.x509_utils import X5cX509CertChain


logger = logging.getLogger(__name__)

# We always compose the request with MessageType=DIGEST: the signing input embeds the
# full x5c cert chain in the JWT header and will typically exceed AWS KMS's 4096-byte
# cap for MessageType=RAW. Signing the pre-computed digest yields an equally valid
# signature regardless of input size.
KMS_MESSAGE_TYPE_DIGEST = "DIGEST"

# Map an AWS KMS ECDSA signing algorithm to the hashlib constructor used to pre-hash
# the signing input for MessageType=DIGEST.
_AWS_ALG_HASHLIB_MAPPING = {
    AWSKMSSignAlgorithm.ECDSA_SHA_256: hashlib.sha256,
    AWSKMSSignAlgorithm.ECDSA_SHA_384: hashlib.sha384,
    AWSKMSSignAlgorithm.ECDSA_SHA_512: hashlib.sha512,
}

# Validate both by field name and by alias so the models accept snake_case names (when
# constructed in Python) and the PascalCase wire aliases (when parsed from JSON), while
# serialization with by_alias=True emits the wire form.
_WIRE_MODEL_CONFIG = ConfigDict(validate_by_name=True, validate_by_alias=True)


#
# ------ message schemas ------ #
# Wire field names follow the AWS KMS Sign API.
# See https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html
#


class AWSKMSSignRequestTemplate(BaseModel):
    """The composable subset of an AWS KMS ``Sign`` request.

    The caller fills in the remaining required fields (e.g. ``KeyId``) before invoking
    the KMS ``Sign`` API. See ``API_Sign_RequestSyntax``.
    """

    model_config = _WIRE_MODEL_CONFIG

    message: str = Field(alias="Message")
    """Base64-encoded SHA-* digest of the JWT signing input (for MessageType=DIGEST)."""
    message_type: str = Field(default=KMS_MESSAGE_TYPE_DIGEST, alias="MessageType")
    signing_algorithm: AWSKMSSignAlgorithm = Field(alias="SigningAlgorithm")


class GenRawJWTOutput(BaseModel):
    """The ``gen-raw-jwt`` output object printed to stdout."""

    model_config = _WIRE_MODEL_CONFIG

    aws_kms_sign_request: AWSKMSSignRequestTemplate = Field(alias="AWSKMSSignRequest")
    jwt_payload_unsigned: str = Field(alias="JWTPayloadUnsigned")
    schema_ver: Literal[1] = Field(default=1, alias="SchemaVer")


class AWSKMSSignResponse(BaseModel):
    """The relevant subset of an AWS KMS ``Sign`` response.

    Extra fields (e.g. ``KeyId``) returned by the API are ignored. See
    ``API_Sign_ResponseSyntax``.
    """

    # NOTE: extra="ignore" so a verbatim KMS response (with KeyId, etc.) validates.
    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, extra="ignore"
    )

    signature: str = Field(alias="Signature")
    """Base64-encoded DER ECDSA signature, as in the KMS REST response."""
    signing_algorithm: AWSKMSSignAlgorithm = Field(alias="SigningAlgorithm")


class SignWithAWSKMSInput(BaseModel):
    """The ``sign-with-aws-kms`` input object."""

    model_config = _WIRE_MODEL_CONFIG

    jwt_payload_unsigned: str = Field(alias="JWTPayloadUnsigned")
    aws_kms_sign_response: AWSKMSSignResponse = Field(alias="AWSKMSSignResponse")


#
# ------ gen-raw-jwt ------ #
#


def gen_raw_jwt(
    image_root: Path,
    *,
    sign_cert_chain: X5cX509CertChain,
    force_sign: bool,
) -> GenRawJWTOutput:
    """Finalize the signing state and compose the unsigned JWT + KMS Sign request.

    NOTE: this mutates the OTA image. It sets the ``signed_at`` annotation and rewrites
        ``index.json`` so the descriptor embedded into the returned JWT matches the
        on-disk ``index.json``. The returned ``JWTPayloadUnsigned`` must be paired with
        the KMS ``Sign`` response of the *same* ``gen-raw-jwt`` run.

    Returns:
        The gen-raw-jwt output with the ``AWSKMSSignRequest`` template, the
        ``JWTPayloadUnsigned`` signing input and ``SchemaVer``.
    """
    _index_helper = ImageIndexHelper(image_root)
    if not _index_helper.image_index.image_finalized:
        exit_with_err_msg(
            "ERR: image is not yet finalized, "
            "run the `finalize` command before `gen-raw-jwt`, abort!"
        )
    _index_helper.image_index.finalize_signing_image(force_sign=force_sign)
    _, _index_descriptor = _index_helper.sync_index()

    aws_alg, unsigned_jwt = compose_unsigned_index_jwt_for_aws_kms_sign(
        _index_descriptor, sign_cert_chain=sign_cert_chain
    )

    try:
        _hashlib_impl = _AWS_ALG_HASHLIB_MAPPING[aws_alg]
    except KeyError:
        exit_with_err_msg(f"unsupported AWS KMS signing algorithm: {aws_alg}")

    _digest = _hashlib_impl(unsigned_jwt.encode("ascii")).digest()
    _message_b64 = base64.b64encode(_digest).decode("ascii")

    # NOTE: construct via the wire aliases (validate_by_alias=True). This also keeps
    #       static type checkers happy, since the synthesized __init__ for an aliased
    #       model is keyed on the aliases.
    return GenRawJWTOutput(
        AWSKMSSignRequest=AWSKMSSignRequestTemplate(
            Message=_message_b64,
            SigningAlgorithm=aws_alg,
        ),
        JWTPayloadUnsigned=unsigned_jwt,
    )


def gen_raw_jwt_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    _parser = sub_arg_parser.add_parser(
        name="gen-raw-jwt",
        help=(
            _help_txt := "Generate the unsigned JWT and an AWS KMS Sign request "
            "template for signing an OTA image with AWS KMS"
        ),
        description=(
            f"{_help_txt}. The OTA image MUST be finalized first (run `finalize`). "
            "This finalizes the image signing state and rewrites index.json, then "
            "prints a JSON object with `AWSKMSSignRequest`, `JWTPayloadUnsigned` and "
            "`SchemaVer` to stdout."
        ),
        parents=parent_parser,
    )
    _parser.add_argument(
        "--sign-cert",
        help="OTA image signing cert in X509 PEM format.",
        required=True,
    )
    _parser.add_argument(
        "--ca-cert",
        action="append",
        help="Intermediate CA certs in X509 PEM format for cert chain, "
        "can be specified multiple times.",
    )
    _parser.add_argument(
        "--force-sign",
        help="If specified, force re-generating for an already signed OTA image.",
        action="store_true",
    )
    _parser.add_argument(
        "image_root",
        help="The folder that holds the finalized OTA image to be signed.",
    )
    _parser.set_defaults(handler=gen_raw_jwt_cmd)


def gen_raw_jwt_cmd(args: Namespace) -> None:
    logger.debug(f"calling {gen_raw_jwt_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} doesn't hold a valid OTA image.")

    sign_cert_f = Path(args.sign_cert)
    ca_certs_fs = [Path(_ca_cert) for _ca_cert in (args.ca_cert or [])]

    if not sign_cert_f.is_file():
        exit_with_err_msg(f"{sign_cert_f=} not found.")
    for _ca_cert in ca_certs_fs:
        if not _ca_cert.is_file():
            exit_with_err_msg(f"CA cert {_ca_cert} is specified, but not found.")

    try:
        loaded_cert_chain = load_cert_chains(sign_cert_f, ca_certs_fs)
    except Exception as e:
        logger.debug(f"failed to load sign cert chain: {e}", exc_info=e)
        exit_with_err_msg(
            f"failed to load sign cert chain {sign_cert_f} and {ca_certs_fs}"
        )

    logger.info(
        f"Generating raw JWT and AWS KMS Sign request template for {image_root} ..."
    )
    try:
        result = gen_raw_jwt(
            image_root,
            sign_cert_chain=loaded_cert_chain,
            force_sign=args.force_sign,
        )
    except Exception as e:
        logger.debug(f"failed to generate raw JWT: {e}", exc_info=e)
        exit_with_err_msg(f"failed to generate raw JWT: {e}")

    # NOTE: only the JSON result goes to stdout (logs go to stderr) so the output stays
    #       clean and machine-parseable.
    print(result.model_dump_json(by_alias=True, indent=2))


#
# ------ sign-with-aws-kms ------ #
#


def _load_input_json(_in: str | None) -> dict[str, Any]:
    """Resolve the ``input`` arg into a parsed JSON object.

    Accepts inline JSON (a string starting with ``{``), a file path, or ``-`` to read
    from stdin.
    """
    if not _in:
        exit_with_err_msg("empty input, abort!")

    if _in.lstrip().startswith("{"):
        _raw = _in
    elif _in == "-":
        try:
            _raw = sys.stdin.read()
        except Exception as e:
            exit_with_err_msg(f"failed to read input from stdin: {e!r}")
    else:
        try:
            _raw = Path(_in).read_text()
        except FileNotFoundError:
            exit_with_err_msg("the specified input file doesn't exist!")
        except Exception as e:
            exit_with_err_msg(f"failed to read input file: {e!r}")

    try:
        _loaded = json.loads(_raw)
    except Exception as e:
        exit_with_err_msg(f"input is not valid JSON: {e!r}")

    if not isinstance(_loaded, dict):
        exit_with_err_msg("input must be a JSON object.")
    return _loaded


def _parse_sign_input(raw: dict[str, Any]) -> SignWithAWSKMSInput:
    """Validate the raw input object against the ``SignWithAWSKMSInput`` schema."""
    try:
        return SignWithAWSKMSInput.model_validate(raw)
    except ValidationError as e:
        logger.debug(f"invalid sign-with-aws-kms input: {e}", exc_info=e)
        exit_with_err_msg(f"invalid input: {e}")


def compose_signed_jwt_from_kms_response(input_obj: SignWithAWSKMSInput) -> str:
    """Assemble the complete signed index.jwt from a gen-raw-jwt output + KMS response.

    Returns:
        The complete ``header.payload.signature`` JWT.
    """
    kms_sign_resp = input_obj.aws_kms_sign_response

    # NOTE: the AWS KMS REST response encodes `Signature` as base64. (A boto3 caller
    #       gets raw bytes and must base64-encode them before placing into this JSON.)
    try:
        der_sig = base64.b64decode(kms_sign_resp.signature)
    except Exception as e:
        exit_with_err_msg(f"failed to base64-decode the KMS Signature: {e!r}")

    try:
        return compose_jwt_from_aws_kms_sign_response(
            input_obj.jwt_payload_unsigned,
            kms_sign_resp=der_sig,
            kms_sign_algorithm=kms_sign_resp.signing_algorithm,
        )
    except Exception as e:
        logger.debug(f"failed to compose signed JWT: {e}", exc_info=e)
        exit_with_err_msg(f"failed to compose signed JWT: {e}")


def sign_with_aws_kms_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    _parser = sub_arg_parser.add_parser(
        name="sign-with-aws-kms",
        help=(
            _help_txt := "Assemble a signed index.jwt from a gen-raw-jwt output "
            "and an AWS KMS Sign response"
        ),
        description=(
            f"{_help_txt}. Prints the complete signed JWT to stdout, and optionally "
            "writes it into <image-root>/index.jwt."
        ),
        parents=parent_parser,
    )
    _parser.add_argument(
        "input",
        help="A JSON object with `JWTPayloadUnsigned` (from gen-raw-jwt) and "
        "`AWSKMSSignResponse` (the AWS KMS Sign API response). "
        "This arg takes either inline JSON, a file path, or `-` to read from stdin.",
    )
    _parser.add_argument(
        "--image-root",
        default=None,
        help="If specified, also write the resulting JWT into <image-root>/index.jwt.",
    )
    _parser.set_defaults(handler=sign_with_aws_kms_cmd)


def sign_with_aws_kms_cmd(args: Namespace) -> None:
    logger.debug(f"calling {sign_with_aws_kms_cmd.__name__} with {args}")
    raw_input = _load_input_json(args.input)
    input_obj = _parse_sign_input(raw_input)
    signed_jwt = compose_signed_jwt_from_kms_response(input_obj)

    if args.image_root is not None:
        image_root = Path(args.image_root)
        if not check_if_valid_ota_image(image_root):
            exit_with_err_msg(f"{image_root} doesn't hold a valid OTA image.")
        index_jwt_f = image_root / INDEX_JWT_FNAME
        index_jwt_f.write_text(signed_jwt)
        logger.info(f"Signed index.jwt written to {index_jwt_f}")

    # NOTE: only the JWT goes to stdout (logs go to stderr) so it stays pipeable.
    print(signed_jwt)
