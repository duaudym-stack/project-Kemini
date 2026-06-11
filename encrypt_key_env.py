"""Encrypt workspace key.env into key.env.enc."""
from __future__ import annotations

import argparse

from .secret_loader import ENCRYPTED_ENV, PLAINTEXT_ENV, encrypt_key_env, plaintext_key_names, rekey_encrypted_env


def main() -> int:
    parser = argparse.ArgumentParser(description="Encrypt key.env with Windows DPAPI.")
    parser.add_argument(
        "--remove-plaintext",
        action="store_true",
        help="Delete key.env after key.env.enc is created successfully.",
    )
    parser.add_argument(
        "--rekey-machine",
        action="store_true",
        help="Re-encrypt existing key.env.enc so any process on this Windows machine can decrypt it.",
    )
    args = parser.parse_args()

    if args.rekey_machine:
        if not rekey_encrypted_env(local_machine=True):
            print(f"No encrypted key file found at {ENCRYPTED_ENV}.")
            return 1
        print(f"Re-encrypted {ENCRYPTED_ENV} for this Windows machine.")
        return 0

    names = plaintext_key_names()
    if not names:
        print(f"No keys found in {PLAINTEXT_ENV}. Save key.env first.")
        return 1

    if not encrypt_key_env():
        print(f"Could not encrypt {PLAINTEXT_ENV}.")
        return 1

    if args.remove_plaintext:
        PLAINTEXT_ENV.unlink()

    print(f"Encrypted {len(names)} key(s) into {ENCRYPTED_ENV}.")
    print("Key names: " + ", ".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
