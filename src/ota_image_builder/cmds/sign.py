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

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.x509 import load_pem_x509_certificate
from ota_image_libs._crypto.x509_utils import X509CertChain
from ota_image_libs.v1.consts import INDEX_JWT_FNAME
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.index_jwt.utils import compose_index_jwt

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)


def load_cert_chains(ee_fpath: Path, ca_fpaths: list[Path]) -> X509CertChain:
    _res = X509CertChain()
    _res.add_ee(load_pem_x509_certificate(ee_fpath.read_bytes()))
    if ca_fpaths:
        _res.add_interms(
            *[load_pem_x509_certificate(_ca_f.read_bytes()) for _ca_f in ca_fpaths]
        )
    return _res


def sign_image(
    image_root: Path,
    *,
    sign_cert_chain: X509CertChain,
    sign_key: bytes,
    force_sign: bool,
    sign_key_passwd: bytes | None = None,
) -> None:
    """Sign the image, and write the output index.jwt into the OTA image root."""
    _index_helper = ImageIndexHelper(image_root)
    if not _index_helper.image_index.image_finalized:
        exit_with_err_msg(
            "ERR: image is not yet finalized, thus cannot be signed, abort!"
        )
    _index_helper.image_index.finalize_signing_image(force_sign=force_sign)
    _, _index_descriptor = _index_helper.sync_index()

    _index_jwt = compose_index_jwt(
        _index_descriptor,
        sign_cert_chain=sign_cert_chain,
        sign_key=sign_key,
        sign_key_passwd=sign_key_passwd,
    )
    _index_jwt_f = image_root / INDEX_JWT_FNAME
    _index_jwt_f.write_text(_index_jwt)


def sign_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    init_cmd_arg_parser = sub_arg_parser.add_parser(
        name="sign",
        help=(_help_txt := "Sign an OTA image with sign cert chain and sign key"),
        description=_help_txt,
        parents=parent_parser,
    )
    init_cmd_arg_parser.add_argument(
        "--sign-cert",
        help="OTA image signing cert in X509 PEM format.",
        required=True,
    )
    init_cmd_arg_parser.add_argument(
        "--sign-key",
        help="OTA image signing key in PEM format.",
        required=True,
    )
    init_cmd_arg_parser.add_argument(
        "--sign-key-passwd",
        default=None,
        help="If private key is protected by passphrase.",
    )
    init_cmd_arg_parser.add_argument(
        "--ca-cert",
        action="append",
        help="Intermediate CA certs in X509 PEM format for cert chain, can be specified multiple times.",
    )
    init_cmd_arg_parser.add_argument(
        "--force-sign",
        help="If specified, fore signing an already signed OTA image.",
        action="store_true",
    )
    init_cmd_arg_parser.add_argument(
        "image_root",
        help="The folder to hold a new empty OTA image. It should be an empty folder.",
    )
    init_cmd_arg_parser.set_defaults(handler=sign_cmd)


def sign_cmd(args: Namespace) -> None:
    logger.debug(f"calling {sign_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} doesn't hold a valid OTA image.")

    sign_cert = Path(args.sign_cert)
    sign_key = Path(args.sign_key)
    ca_certs = [Path(_ca_cert) for _ca_cert in args.ca_cert]

    if not sign_cert.is_file():
        exit_with_err_msg(f"{sign_cert=} not found.")
    if not sign_key.is_file():
        exit_with_err_msg(f"{sign_key=} not found.")
    for _ca_cert in ca_certs:
        if not _ca_cert.is_file():
            exit_with_err_msg(f"CA cert {_ca_cert} is specified, but not found.")

    try:
        loaded_cert_chain = load_cert_chains(sign_cert, ca_certs)
    except Exception as e:
        logger.debug(f"failed to load sign cert chain: {e}", exc_info=e)
        exit_with_err_msg(f"failed to load sign cert chain {sign_cert} and {ca_certs}")

    logger.info(f"Will sign OTA image at {image_root} ...")

    _key_pass = args.sign_key_passwd.encode() if args.sign_key_passwd else None
    try:
        sign_image(
            image_root,
            force_sign=args.force_sign,
            sign_cert_chain=loaded_cert_chain,
            sign_key=sign_key.read_bytes(),
            sign_key_passwd=_key_pass,
        )
    except Exception as e:
        logger.debug(f"failed to sign the image: {e}", exc_info=e)
        exit_with_err_msg(f"failed to sign the image: {e}")

    sign_cert = loaded_cert_chain.ee
    print(
        "OTA Image is signed successfully!\n"
        f"{sign_cert.subject=}\n{sign_cert.not_valid_before_utc=}\n{sign_cert.not_valid_after_utc=}"
    )
