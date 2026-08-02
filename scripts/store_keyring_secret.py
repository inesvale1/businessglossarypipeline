from __future__ import annotations

import argparse
import getpass


def main() -> None:
    parser = argparse.ArgumentParser(description="Store a secret (API key, proxy password, ...) securely in the OS keyring.")
    parser.add_argument("--service", required=True, help="Keyring service name, e.g.: catalogo-semantico-azure-openai")
    parser.add_argument("--username", required=True, help="Keyring username for that service")
    parser.add_argument("--show-check", action="store_true", help="Confirm that the secret can be read back")
    args = parser.parse_args()

    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("This script requires keyring. Install it with: pip install keyring") from exc

    secret = getpass.getpass("Secret: ")
    confirmation = getpass.getpass("Repeat secret: ")
    if secret != confirmation:
        raise ValueError("Secrets do not match. Nothing was stored.")

    keyring.set_password(args.service, args.username, secret)
    print(f"Secret stored in keyring for service '{args.service}' and username '{args.username}'.")

    if args.show_check:
        stored = keyring.get_password(args.service, args.username)
        print("Read-back check:", "OK" if stored else "NOT FOUND")


if __name__ == "__main__":
    main()
