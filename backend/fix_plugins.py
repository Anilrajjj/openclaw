import sqlite3
import json

def fix_plugins():
    conn = sqlite3.connect('promptiq.db')
    cursor = conn.cursor()
    cursor.execute('SELECT instance_id, config_json FROM instance_configs')
    rows = cursor.fetchall()

    for instance_id, config_str in rows:
        try:
            config = json.loads(config_str)
            if 'plugins' not in config:
                config['plugins'] = {}
            if 'entries' not in config['plugins']:
                config['plugins']['entries'] = {}
            
            # Disable problematic plugins
            config['plugins']['entries']['file-transfer'] = {'enabled': False}
            config['plugins']['entries']['memory-core'] = {'enabled': False}
            
            new_config_str = json.dumps(config)
            cursor.execute('UPDATE instance_configs SET config_json = ? WHERE instance_id = ?', (new_config_str, instance_id))
            print(f"Updated instance {instance_id}")
        except Exception as e:
            print(f"Failed to update instance {instance_id}: {e}")

    conn.commit()
    conn.close()
    print("Database update complete.")

if __name__ == "__main__":
    fix_plugins()
