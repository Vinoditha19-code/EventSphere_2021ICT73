import sqlite3
from getpass import getpass
from pwdlib import PasswordHash

db = sqlite3.connect("eventsphere.db")
hasher = PasswordHash.recommended()

for role in("admin","staff","attendee"):
    email = role + "@demo.test"
    password = getpass(f"Enter a password fro {email}: ")
    password_hash = hasher.hash(password)

    db.execute(
        "INSERT INTO users(email, password_hash,role) VALUES (?,?,?)",
        (email,password_hash,role)
    )

db.commit()
db.close()
print("Three demo accounts created!!!")