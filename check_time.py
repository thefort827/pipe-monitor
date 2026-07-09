import sqlite3
conn = sqlite3.connect(':memory:')
print(conn.execute("SELECT datetime('now')").fetchone()[0])
conn.close()
