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

1. ``sign-with-aws-kms-prepare``: finalizes the image signing state, rewrites ``index.json`` and emits
   the unsigned JWT signing input (``header.payload``) together with a ready-to-fill
   AWS KMS ``Sign`` request template.
2. ``sign-with-aws-kms-finish``: takes that unsigned signing input plus the AWS KMS ``Sign``
   response and assembles the complete, signed ``index.jwt``.

Reference: https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ota_image_libs._crypto.aws_kms import (
    AWSKMSSignAlgorithm,
    compose_jwt_from_aws_kms_sign_response,
    get_aws_sign_alg,
)
from ota_image_libs.v1.consts import INDEX_JWT_FNAME
from ota_image_libs.v1.index_jwt.utils import (
    compose_unsigned_index_jwt_for_aws_kms_sign,
    decode_index_jwt_with_verification,
    get_index_jwt_sign_cert_chain,
    get_unverified_jwt_headers,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg
from ota_image_builder.cmds.sign import (
    load_cert_chain_from_args,
    seal_image_before_sign,
)

from ._utils import MODEL_WITH_ALIAS, resolve_cli_input_arg

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction

    from ota_image_libs._crypto.x509_utils import X5cX509CertChain


logger = logging.getLogger(__name__)

# Map an AWS KMS ECDSA signing algorithm to the hashlib constructor used to pre-hash
# the signing input for MessageType=DIGEST.
_AWS_ALG_HASHLIB_MAPPING = {
    AWSKMSSignAlgorithm.ECDSA_SHA_256: hashlib.sha256,
    AWSKMSSignAlgorithm.ECDSA_SHA_384: hashlib.sha384,
    AWSKMSSignAlgorithm.ECDSA_SHA_512: hashlib.sha512,
}


#
# ------ message schemas ------ #
#
# Wire field names follow the AWS KMS Sign API.
# See https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html
#


class AWSKMSSignRequestTemplate(BaseModel):
    """The composable subset of an AWS KMS ``Sign`` request.

    The caller fills in the remaining required fields (e.g. ``KeyId``) before invoking
    the KMS ``Sign`` API. See ``API_Sign_RequestSyntax``.
    """

    model_config = MODEL_WITH_ALIAS

    message: str = Field(alias="Message")
    """Base64-encoded SHA-* digest of the JWT signing input (for MessageType=DIGEST)."""
    message_type: Literal["DIGEST"] = Field(default="DIGEST", alias="MessageType")
    signing_algorithm: AWSKMSSignAlgorithm = Field(alias="SigningAlgorithm")


class SignWithAWSKMSPrepareOutput(BaseModel):
    """The ``sign-with-aws-kms-prepare`` output object printed to stdout."""

    model_config = MODEL_WITH_ALIAS

    aws_kms_sign_request: AWSKMSSignRequestTemplate = Field(
        alias="AWSKMSSignRequestTemplate"
    )
    jwt_payload_unsigned: str = Field(alias="JWTPayloadUnsigned")
    """The Base64_URL-encoded JWT(`header.payload`)."""
    schema_ver: Literal[1] = Field(default=1, alias="SchemaVer")


class AWSKMSSignResponse(BaseModel):
    """The relevant subset of an AWS KMS ``Sign`` response.

    Extra fields (e.g. ``KeyId``) returned by the API are ignored. See
    ``API_Sign_ResponseSyntax``.
    """

    # NOTE: extra="ignore" to ignore unrelated fields in the KMS sign resp.
    model_config = ConfigDict(MODEL_WITH_ALIAS, extra="ignore")

    signature: str = Field(alias="Signature")
    """Base64-encoded DER ECDSA signature, as in the KMS REST response."""
    signing_algorithm: AWSKMSSignAlgorithm = Field(alias="SigningAlgorithm")


class SignWithAWSKMSInput(BaseModel):
    """The ``sign-with-aws-kms-finish`` input object."""

    model_config = MODEL_WITH_ALIAS

    jwt_payload_unsigned: str = Field(alias="JWTPayloadUnsigned")
    """The Base64_URL-encoded JWT(`header.payload`)."""
    aws_kms_sign_response: AWSKMSSignResponse = Field(alias="AWSKMSSignResponse")
    schema_ver: Literal[1] = Field(default=1, alias="SchemaVer")


#
# ------ sign-with-aws-kms-prepare ------ #
#


def sign_with_aws_kms_prepare(
    image_root: Path,
    *,
    sign_cert_chain: X5cX509CertChain,
    force_sign: bool,
) -> SignWithAWSKMSPrepareOutput:
    """Finalize the signing state and compose the unsigned JWT + KMS Sign request.

    NOTE: this mutates the OTA image. It sets the ``signed_at`` annotation and rewrites
        ``index.json`` so the descriptor embedded into the returned JWT matches the
        on-disk ``index.json``. The returned ``JWTPayloadUnsigned`` must be paired with
        the KMS ``Sign`` response of the *same* ``sign-with-aws-kms-prepare`` run.

    Returns:
        The sign-with-aws-kms-prepare output with the ``AWSKMSSignRequestTemplate``,
        the ``JWTPayloadUnsigned`` signing input and ``SchemaVer``.
    """
    _index_descriptor = seal_image_before_sign(image_root, force_sign=force_sign)
    aws_alg, unsigned_jwt = compose_unsigned_index_jwt_for_aws_kms_sign(
        _index_descriptor, sign_cert_chain=sign_cert_chain
    )

    try:
        _hashlib_impl = _AWS_ALG_HASHLIB_MAPPING[aws_alg]
    except KeyError:
        exit_with_err_msg(f"unsupported AWS KMS signing algorithm: {aws_alg}")

    _message_digest_b64 = base64.b64encode(
        _hashlib_impl(unsigned_jwt.encode("ascii")).digest()
    ).decode("ascii")

    return SignWithAWSKMSPrepareOutput(
        AWSKMSSignRequestTemplate=AWSKMSSignRequestTemplate(
            Message=_message_digest_b64,
            SigningAlgorithm=aws_alg,
        ),
        JWTPayloadUnsigned=unsigned_jwt,
    )


def sign_with_aws_kms_prepare_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    _help_txt = (
        "Prepare for signing OTA image with AWS KMS signing: set ``signed_at`` for "
        "the index.json, and then emit the unsigned JWT and a corresponding "
        "AWS KMS Sign request template for caller to request signing."
    )
    _parser = sub_arg_parser.add_parser(
        name="sign-with-aws-kms-prepare",
        help=_help_txt,
        description=(
            f"{_help_txt} The OTA image MUST be finalized first (run `finalize`). "
            "This finalizes the image signing state and rewrites index.json, then "
            "prints a JSON object with `AWSKMSSignRequestTemplate`, "
            "`JWTPayloadUnsigned` and `SchemaVer` to stdout."
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
    _parser.set_defaults(handler=sign_with_aws_kms_prepare_cmd)


def sign_with_aws_kms_prepare_cmd(args: Namespace) -> None:
    logger.debug(f"calling {sign_with_aws_kms_prepare_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} doesn't hold a valid OTA image.")

    loaded_cert_chain = load_cert_chain_from_args(args)

    logger.info(
        f"Generating raw JWT and AWS KMS Sign request template for {image_root} ..."
    )
    try:
        result = sign_with_aws_kms_prepare(
            image_root,
            sign_cert_chain=loaded_cert_chain,
            force_sign=args.force_sign,
        )
    except Exception as e:
        logger.debug(f"failed to generate raw JWT: {e}", exc_info=e)
        exit_with_err_msg(f"failed to generate raw JWT: {e}")
    print(result.model_dump_json(indent=2))


#
# ------ sign-with-aws-kms-finish ------ #
#


def _check_aws_kms_sign_resp_against_jwt(
    signed_jwt: str, sign_resp: AWSKMSSignResponse
) -> None:
    """Ensure that the KMS sign response is for this JWT.

    The following checks are performed:
    1. check if the JWT algorithm matches the KMS signing algorithm.
    2. check if the JWT can pass self-verification(signature against embedded sign cert).
    """
    _headers = get_unverified_jwt_headers(signed_jwt)
    _alg = _headers["alg"]

    if (_kms_alg := get_aws_sign_alg(_alg)) != sign_resp.signing_algorithm:
        exit_with_err_msg(
            f"JWT's algorithm({_alg}) doesn't match "
            f"the KMS sign response's algorithm({_kms_alg})!!! "
            "Are you sure this sign_resp is actually for this OTA image???"
        )

    try:
        _embedded_chain = get_index_jwt_sign_cert_chain(signed_jwt)
        decode_index_jwt_with_verification(signed_jwt, _embedded_chain)
    except Exception as e:
        logger.exception(f"assembled index.jwt failed self-verification: {e}")
        exit_with_err_msg(
            "the assembled index.jwt failed signature self-verification; the AWS KMS "
            "Sign response does not match the signing input or the signing cert "
            f"(check the KeyId / signing cert pairing): {e}"
        )


def compose_signed_jwt_from_kms_response(input_obj: SignWithAWSKMSInput) -> str:
    """Assemble the complete signed index.jwt from a sign-with-aws-kms-prepare output + KMS response.

    Returns:
        The complete ``header.payload.signature`` JWT.
    """
    kms_sign_resp = input_obj.aws_kms_sign_response
    unsigned_jwt = input_obj.jwt_payload_unsigned

    # NOTE: the AWS KMS REST response encodes `Signature` as base64-encoded.
    try:
        der_sig = base64.b64decode(kms_sign_resp.signature, validate=True)
    except Exception as e:
        exit_with_err_msg(f"failed to base64-decode the KMS Signature: {e!r}")

    try:
        signed_jwt = compose_jwt_from_aws_kms_sign_response(
            unsigned_jwt,
            kms_sign_resp=der_sig,
            kms_sign_algorithm=kms_sign_resp.signing_algorithm,
        )
    except Exception as e:
        logger.debug(f"failed to compose signed JWT: {e}", exc_info=e)
        exit_with_err_msg(f"failed to compose signed JWT: {e}")

    _check_aws_kms_sign_resp_against_jwt(signed_jwt, kms_sign_resp)
    return signed_jwt


def sign_with_aws_kms_finish_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    _help_txt = (
        "Assemble a signed index.jwt from a sign-with-aws-kms-prepare output "
        "and an AWS KMS Sign response"
    )
    _parser = sub_arg_parser.add_parser(
        name="sign-with-aws-kms-finish",
        help=_help_txt,
        description=(
            f"{_help_txt}. Prints the complete signed JWT to stdout, and optionally "
            "writes it into <image-root>/index.jwt."
        ),
        parents=parent_parser,
    )
    _parser.add_argument(
        "input",
        help="A JSON object with `JWTPayloadUnsigned` (from sign-with-aws-kms-prepare) and "
        "`AWSKMSSignResponse` (the AWS KMS Sign API response). "
        "This arg takes either inline JSON, a file path, or `-` to read from stdin.",
    )
    _parser.add_argument(
        "--image-root",
        default=None,
        help="If specified, also write the resulting JWT into <image-root>/index.jwt.",
    )
    _parser.set_defaults(handler=sign_with_aws_kms_finish_cmd)


def sign_with_aws_kms_finish_cmd(args: Namespace) -> None:
    logger.debug(f"calling {sign_with_aws_kms_finish_cmd.__name__} with {args}")
    raw_input_json = resolve_cli_input_arg(args.input, inline_prefix="{", label="input")

    try:
        input_obj = SignWithAWSKMSInput.model_validate_json(raw_input_json)
    except ValidationError as e:
        logger.debug(f"invalid sign-with-aws-kms-finish input: {e}", exc_info=e)
        exit_with_err_msg(f"invalid input: {e}")

    signed_jwt = compose_signed_jwt_from_kms_response(input_obj)
    if args.image_root is not None:
        image_root = Path(args.image_root)
        if not check_if_valid_ota_image(image_root):
            exit_with_err_msg(f"{image_root} doesn't hold a valid OTA image.")
        logger.info(f"write the signed index.jwt to the {args.image_root} ...")

        index_jwt_f = image_root / INDEX_JWT_FNAME
        index_jwt_f.write_text(signed_jwt)
        logger.info(f"Signed index.jwt written to {index_jwt_f}")

    # NOTE: only the JWT goes to stdout (logs go to stderr) so it stays pipeable.
    print(signed_jwt)
