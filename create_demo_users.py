"""Create three demo accounts in the currently selected database without overwriting accounts."""
import os  # Optional DEMO_PASSWORD setting for deployment setup.  # Reads settings such as the database path and secret key from environment variables.
from getpass import getpass  # Read a password without showing it in the terminal.  # Read a password without printing it on the screen.
from pwdlib import PasswordHash  # Hash the demo password before saving it.  # Hashes passwords and checks a password against its saved hash.
from database import create_tables,transaction  # Use SQLite or PostgreSQL through the same connection settings.  # Uses the shared database path, connections, transactions and table setup.


# Create hashed demo accounts without overwriting existing users.
def create_demo_users():
    # Use this manually when setting up the new production database.
    password=os.getenv('DEMO_PASSWORD') or getpass('Choose a demo password (at least 8 characters): ')
    if len(password)<8:
        raise ValueError('The demo password must contain at least 8 characters')
    create_tables()
    hasher=PasswordHash.recommended()
    # Keep these changes together in one transaction.
    with transaction() as db:
        for role in ('admin','staff','attendee'):
            email=role+'@demo.test'
            result=db.execute('INSERT INTO users(email,password_hash,role) VALUES (?,?,?) ON CONFLICT(email) DO NOTHING',
                              (email,hasher.hash(password),role))
            print(email+(': created' if result.rowcount else ': already exists; password unchanged'))
    print('Use the password you chose for the newly created accounts. Do not commit it to GitHub.')


if __name__=='__main__':
    create_demo_users()
