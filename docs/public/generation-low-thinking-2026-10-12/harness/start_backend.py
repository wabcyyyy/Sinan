import os, sys
from pathlib import Path
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'travel-agent-python'))
os.chdir(root / 'travel-agent-python')
from dotenv import load_dotenv
load_dotenv(root / '.env')
load_dotenv(root / 'travel-agent-python/.env')
os.environ["TRACE_STORAGE_PATH"] = str(root / ".tmp-release-audit" / "lowthink" / os.environ.get("LT_TRACE", "traces.jsonl"))
os.environ['DB_HOST'] = '127.0.0.1'
os.environ['DB_PORT'] = os.getenv('MYSQL_PORT', '3307')
os.environ['DB_USER'] = 'root'
os.environ['DB_PASSWORD'] = os.getenv('MYSQL_ROOT_PASSWORD', 'travel_dev_only')
os.environ['DB_NAME'] = 'release_audit_20261011'
os.environ['AUTH_COOKIE_SECURE'] = 'false'
os.environ['AGENT_CORS_ORIGINS'] = 'http://127.0.0.1:5175'
if '--fault' in sys.argv:
    os.environ['LLM_BASE_URL'] = 'http://127.0.0.1:8009'
    os.environ['LLM_API_KEY'] = 'synthetic-invalid-test-key'
    for role in ['MAIN', 'FAST', 'SEARCH', 'JUDGE']:
        os.environ['LLM_ROLE_' + role] = ''
from urllib.parse import quote_plus
os.environ['REDIS_URL'] = 'redis://:' + quote_plus(os.getenv('REDIS_PASSWORD', 'ta_dev_redis_only')) + '@127.0.0.1:6380/1'
import pymysql
conn = pymysql.connect(host='127.0.0.1', port=int(os.environ['DB_PORT']), user='root', password=os.environ['DB_PASSWORD'])
with conn.cursor() as cursor:
    cursor.execute('CREATE DATABASE IF NOT EXISTS release_audit_20261011 CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
conn.close()
from app.db.migrate import ensure_schema
ensure_schema()
import uvicorn
uvicorn.run('main:app', host='127.0.0.1', port=8004)

