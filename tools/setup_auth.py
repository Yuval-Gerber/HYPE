"""Set up (or change) the Hype owner credentials — run this interactively.

Sets the owner PASSWORD and the secret NICKNAME (3rd factor for changing the
payout wallet, §6.3). Input is hidden (getpass); nothing is echoed, logged, or
stored in plaintext — only salted scrypt hashes go into the macOS Keychain.

  python tools/setup_auth.py

If credentials already exist, you must enter the current password to change them.
"""

from __future__ import annotations

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hype.security.auth import AuthManager  # noqa: E402
from hype.security import touchid  # noqa: E402


def main() -> int:
    auth = AuthManager()
    print("\n=== Hype owner credential setup ===\n")

    avail, reason = touchid.is_available()
    print(f"Touch ID / password available on this Mac: {'yes' if avail else 'no'}"
          + (f"  ({reason})" if not avail else ""))

    if auth.is_password_set():
        print("\nCredentials already exist. Enter your CURRENT password to change them.")
        current = getpass.getpass("Current password: ")
        if not auth.verify_password(current):
            print("❌ Incorrect password. Aborting — nothing changed.")
            return 1

    # New password
    while True:
        pw1 = getpass.getpass("\nNew password (min 6 chars): ")
        if len(pw1) < 6:
            print("  too short, try again.")
            continue
        pw2 = getpass.getpass("Confirm new password: ")
        if pw1 != pw2:
            print("  passwords don't match, try again.")
            continue
        break

    # Secret nickname (3rd factor). No default — it must be owner-chosen.
    while True:
        nick = getpass.getpass("\nSecret nickname (3rd factor): ").strip()
        if nick:
            break
        print("  nickname can't be empty, try again.")

    auth.set_password(pw1)
    auth.set_nickname(nick)
    print("\n✅ Credentials stored in the Keychain (as salted hashes — never plaintext).")

    # Verify round-trip without echoing anything.
    ok = auth.verify_password(pw1) and auth.verify_nickname(nick)
    print(f"   self-check: {'passed' if ok else 'FAILED'}")

    # Optional Touch ID test
    if avail:
        ans = input("\nTest Touch ID now? [y/N]: ").strip().lower()
        if ans == "y":
            success = auth.touch_id("Hype — confirm Touch ID works")
            print(f"   Touch ID: {'SUCCESS' if success else 'failed/declined'}")

    print("\nDone. The dashboard (P5) will use these for login, lock, and the\n"
          "3-factor payout-wallet change.\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
