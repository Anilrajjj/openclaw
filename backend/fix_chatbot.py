import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json, copy
os.environ["OPENCLAW_ROOT"] = r"C:\Users\Anil Kumar\OneDrive\Documents\Openclaw_SAAS"

import database, models, crud, schemas
from services import config_builder, launcher

db = next(database.get_db())

instance = db.query(models.Instance).filter(models.Instance.name == 'Chatbot').first()
print(f'Instance: {instance.name}, status={instance.status}, port={instance.port}')

db_config = db.query(models.InstanceConfig).filter(models.InstanceConfig.instance_id == instance.id).first()
config = copy.deepcopy(db_config.config_json)

# Fix primary model
config['agents']['defaults']['model']['primary'] = 'custom-api-openrouter-ai/openai/gpt-3.5-turbo'

# Minimal system prompt
config['agents']['defaults']['systemPrompt'] = 'You are a helpful and concise assistant. Answer questions directly and briefly.'

# Minimal tools
config['tools']['profile'] = 'minimal'
config['tools']['exec']['enabled'] = False
config['tools']['alsoAllow'] = []
config['tools']['web']['search']['enabled'] = False
config['tools']['web']['fetch']['enabled'] = False

# Write to DB
db_config.config_json = config
from sqlalchemy.orm.attributes import flag_modified
flag_modified(db_config, 'config_json')
db.commit()
print('DB config updated')

# Write to disk
config_builder.activate_instance(instance.name, instance.id, config)
print('Config written to disk')

# Stop existing if any PID leftover
if instance.pid:
    try:
        launcher.stop_instance(instance.pid)
    except:
        pass

# Start
pid = launcher.start_instance(instance.name, instance.id, instance.port)
crud.update_instance(db, instance.id, schemas.InstanceUpdate(status='running', pid=pid))
print(f'Started with PID {pid}')
