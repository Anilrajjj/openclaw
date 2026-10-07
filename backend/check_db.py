import sqlite3
conn = sqlite3.connect('promptiq.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur.fetchall()
print('Tables:', tables)
for t in tables:
    print(f'\n--- {t[0]} ---')
    cur.execute(f'SELECT * FROM "{t[0]}"')
    rows = cur.fetchall()
    if rows:
        col_names = [d[0] for d in cur.description]
        print('Columns:', col_names)
        for r in rows:
            vals = []
            for i, c in enumerate(col_names):
                v = str(r[i])[:100]
                vals.append(f'{c}={v}')
            print(' | '.join(vals))
    else:
        print('(empty)')
conn.close()
