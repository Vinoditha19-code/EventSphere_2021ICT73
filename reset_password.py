from getpass import getpass  # Hide the password while typing.
from pwdlib import PasswordHash  # Create a secure password hash.
from database import transaction  # Connect to your configured database.

# Choose the account whose password you want to change.
email = input("Account email: ").strip().lower()

# Enter the new password twice to avoid typing mistakes.
password = getpass("New password (at least 8 characters): ")
confirm = getpass("Enter the new password again: ")

if len(password) < 8:
    print("Use at least 8 characters.")
elif password != confirm:
    print("Passwords do not match. Run this script again.")
else:
    # Update only the selected account.
    password_hash = PasswordHash.recommended().hash(password)

    with transaction() as db:
        result = db.execute(
            "UPDATE users SET password_hash = ? WHERE email = ?",
            (password_hash, email)
        )

    if result.rowcount:
        print("Password changed successfully.")
    else:
        print("Account not found. Check the email.")