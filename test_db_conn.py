# test_db_conn.py
import os
from pathlib import Path

# Force OpenSSL to allow legacy SQL Server handshakes on macOS
cnf_path = Path(__file__).parent / "openssl_legacy.cnf"
os.environ["OPENSSL_CONF"] = str(cnf_path.resolve())

import pyodbc
from dotenv import load_dotenv

load_dotenv(override=True)

user = os.getenv("DB_USER")
pwd = os.getenv("DB_PASSWORD")
server = "10.13.2.96"
db = os.getenv("DB_NAME")

conn_str = (
    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
    f"SERVER={server};"
    f"DATABASE={db};"
    f"UID={user};"
    f"PWD={pwd};"
    f"Encrypt=no;"
    f"TrustServerCertificate=yes;"
)

print("🔌 Testing connection with OpenSSL workaround...")
try:
    conn = pyodbc.connect(conn_str, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM ai.VW_Inspection")
    row = cursor.fetchone()
    print(f"✅ SUCCESS! Connected to BORE DB. Total inspection records: {row[0]}")
    conn.close()
except Exception as e:
    print(f"❌ Failed: {e}")