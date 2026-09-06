import os
import psycopg
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set in .env")

# Connection pool
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)
pool.open()

def get_db_connection():
    """Returns a connection context manager from the connection pool."""
    return pool.connection()

def init_db():
    """Initializes database schema and ensures all required tables, columns, and enums exist."""
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    try:
        with conn.cursor() as cur:
            # 1. Update/create enums
            for val in ['Refunded', 'Rebooked']:
                try:
                    cur.execute(f"ALTER TYPE booking_status ADD VALUE IF NOT EXISTS '{val}';")
                except Exception:
                    pass

            # 2. Add new columns to flights if not exist
            cur.execute("ALTER TABLE flights ADD COLUMN IF NOT EXISTS fare_type VARCHAR(50) DEFAULT 'BasicEconomy';")
            cur.execute("ALTER TABLE flights ADD COLUMN IF NOT EXISTS overbooking_buffer INT DEFAULT 0;")
            cur.execute("ALTER TABLE flights ADD COLUMN IF NOT EXISTS cancelled BOOLEAN DEFAULT FALSE;")

            # 3. Add new columns to flight_seat_classes if not exist
            cur.execute("ALTER TABLE flight_seat_classes ADD COLUMN IF NOT EXISTS overbooking_buffer INT DEFAULT 0;")

            # 4. Add new columns to bookings if not exist
            cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS fare_type VARCHAR(50) DEFAULT 'BasicEconomy';")
            cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS email VARCHAR(255);")

            # 5. Create idempotency_keys table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                id SERIAL PRIMARY KEY,
                key VARCHAR(255) NOT NULL,
                method_path VARCHAR(255) NOT NULL,
                response_data JSONB NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_idempotency_key_method UNIQUE (key, method_path)
            );
            """)

            # 6. Create audit_log table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                table_name VARCHAR(100) NOT NULL,
                record_id INT,
                action VARCHAR(50) NOT NULL,
                changed_by VARCHAR(100) NOT NULL,
                old_data JSONB,
                new_data JSONB,
                changed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # 7. Create waitlist table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS waitlist (
                id SERIAL PRIMARY KEY,
                flight_id INT REFERENCES flights(id) ON DELETE CASCADE,
                seat_class VARCHAR(50) NOT NULL,
                passenger_name VARCHAR(255) NOT NULL,
                email VARCHAR(255) NOT NULL,
                loyalty_tier INT DEFAULT 0,
                fare_type VARCHAR(50) NOT NULL,
                status VARCHAR(50) DEFAULT 'Pending',
                requested_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """)
    finally:
        conn.close()

# Run initialization on import
init_db()