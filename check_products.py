import sqlite3
conn = sqlite3.connect('database.db')
cur = conn.cursor()
rows = cur.execute('SELECT id, name, category, active, stock, image FROM party_products ORDER BY id DESC LIMIT 20').fetchall()
print('id | name | category | active | stock | image')
for r in rows:
    print(' | '.join(str(x) for x in r))
conn.close()
