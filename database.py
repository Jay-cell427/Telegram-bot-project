import aiopg
from aiopg import create_pool
from config import Config
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class Database:
    pool = None # Class variable to hold the connection pool

    @staticmethod
    async def get_connection():
        """Creates and returns the database connection pool."""
        if Database.pool is None:
            Database.pool = await create_pool(Config.DATABASE)
        return Database.pool

    @staticmethod
    async def init_db():
        """Initialize database with all required tables and extensions."""
        commands = (
            # Enable uuid-ossp extension
            "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";",
            """
            CREATE TABLE IF NOT EXISTS referrals (
              referral_id SERIAL PRIMARY KEY,
              referrer_id BIGINT NOT NULL REFERENCES users(user_id),
              referred_id BIGINT REFERENCES users(user_id),
              referral_code VARCHAR(20) UNIQUE NOT NULL,
              created_at TIMESTAMP DEFAULT NOW(),
              used_at TIMESTAMP,
              reward_given BOOLEAN DEFAULT FALSE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_rewards(
            reward_id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(user_id),
            reward_type VARCHAR(20) NOT NULL,
            reward_value DECIMAL(10,2),
            earned_at TIMESTAMP DEFAULT NOW(),
            used_at TIMESTAMP,
            status VARCHAR(20) DEFAULT 'active'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(32),
                first_name VARCHAR(64),
                last_name VARCHAR(64),
                last_active TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS payments (
                payment_id VARCHAR(128) PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                amount INTEGER NOT NULL,
                currency VARCHAR(3) NOT NULL,
                status VARCHAR(16) DEFAULT 'pending',
                request_timestamp TIMESTAMP DEFAULT NOW(),
                completion_timestamp TIMESTAMP,
                expiry_timestamp TIMESTAMP GENERATED ALWAYS AS
                    (request_timestamp + INTERVAL '%s HOURS') STORED,
                content_id UUID -- Changed from file_id, file_name, file_type
            )
            """ % Config.REQUEST_EXPIRY_HOURS,
            """
            -- Create content_library table
            CREATE TABLE IF NOT EXISTS content_library (
                content_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                content_name VARCHAR(255) NOT NULL UNIQUE,
                google_drive_file_id TEXT NOT NULL, -- This will store Google Drive File ID or CMS URL
                file_type VARCHAR(50) DEFAULT 'document',
                uploaded_at TIMESTAMP DEFAULT NOW(),
                admin_id BIGINT
            )
            """,
            """
            -- Add foreign key constraint to payments table (if not already added)
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'fk_content'
                ) THEN
                    ALTER TABLE payments
                    ADD CONSTRAINT fk_content
                    FOREIGN KEY (content_id) REFERENCES content_library(content_id)
                    ON DELETE SET NULL;
                END IF;
            END
            $$;
            """,
            """
            -- Add provider_charge_id column to payments table if it doesn't exist
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'payments' AND column_name = 'provider_charge_id'
                ) THEN
                    ALTER TABLE payments
                    ADD COLUMN provider_charge_id VARCHAR(255);
                END IF;
            END
            $$;
            """
        )
        for command in commands:
            logger.info(f"Executing DB command: {command.splitlines()[0]}...") # Log only first line of command
            await Database.execute_query(command)
        logger.info("Database initialized with tables.")

    @staticmethod
    async def execute_query(query, params=None, fetch=False):
        """
        Executes a database query.
        Relies on aiopg's context manager for transaction handling.
        """
        async with Database.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                if fetch:
                    return await cur.fetchall()
                # Removed explicit await conn.commit()
                # aiopg's 'async with conn:' context manager handles commit/rollback automatically.


    @staticmethod
    async def get_content_by_name(content_name: str)  -> dict | None: # Changed parameter name from 'title' to 'content_name' for consistency with usage
        """
        Retrieves content details (including content_id) by content title.
        """
        query = """
        SELECT content_id, content_name, file_type, google_drive_file_id
        FROM content_library
        WHERE LOWER(content_name) = LOWER(%s);
        """
        async with Database.pool.acquire() as conn: # FIX: Changed pg_pool to Database.pool
            async with conn.cursor() as cur:
              await cur.execute(query, (content_name,))
              result = await cur.fetchone()
              if result:
                    return {
                    'content_id': result[0],
                    'content_name': result[1],
                    'file_type': result[2],
                    'google_drive_file_id': result[3] # Changed this key
                }
              return None

    @staticmethod
    async def add_or_update_user(user_id: int, username: str, first_name: str, last_name: str):
        query = """
        INSERT INTO users (user_id, username, first_name, last_name, last_active)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET username = EXCLUDED.username,
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            last_active = NOW();
        """
        await Database.execute_query(query, (user_id, username, first_name, last_name))

    @staticmethod
    async def add_pending_payment(payment_id: str, user_id: int, amount: int, currency: str):
        query = """
        INSERT INTO payments (payment_id, user_id, amount, currency, status)
        VALUES (%s, %s, %s, %s, 'pending');
        """
        await Database.execute_query(query, (payment_id, user_id, amount, currency))

    @staticmethod
    async def update_payment_status(payment_id: str, status: str, provider_charge_id: str = None):
        query = """
        UPDATE payments
        SET status = %s,
            completion_timestamp = NOW(),
            provider_charge_id = %s -- Assuming you added this column for charge ID
        WHERE payment_id = %s;
        """
        await Database.execute_query(query, (status, provider_charge_id, payment_id))

    @staticmethod
    async def get_payment_details(payment_id: str):
        query = """
        SELECT payment_id, user_id, amount, currency, status, content_id, request_timestamp, completion_timestamp
        FROM payments
        WHERE payment_id = %s;
        """
        result = await Database.execute_query(query, (payment_id,), fetch=True)
        if result:
            # Map the result to a dictionary for easier access
            # This assumes a specific order of columns in your SELECT statement
            # Consider fetching column names from cur.description if you want a more robust mapping
            columns = ['payment_id', 'user_id', 'amount', 'currency', 'status', 'content_id', 'request_timestamp', 'completion_timestamp']
            return dict(zip(columns, result[0]))
        return None

    @staticmethod
    async def cleanup_expired_pending_payments():
        query = """
        UPDATE payments
        SET status = 'expired'
        WHERE status = 'pending' AND NOW() > expiry_timestamp;
        """
        await Database.execute_query(query)

    @staticmethod
    async def is_user_member(user_id: int):
        # This function might become redundant if membership is checked via Telegram API only
        # or if you track memberships in your DB
        # For now, it's a placeholder.
        return True # Placeholder

    # --- CMS Library Database Methods ---

    @staticmethod
    async def add_content_to_cms_library(content_id: str, content_name: str, google_drive_file_id: str, file_type: str):
        """
        Adds new content metadata to the content_library table.
        gogle_drive_file_id will store the Google Drive File ID.
        """
        query = """
        INSERT INTO content_library (content_id, content_name, file_type, google_drive_file_id)
        VALUES (%s, %s, %s, %s);
        """
        try:
           # Use the helper method execute_query only once
            await Database.execute_query(query, (content_id, content_name, file_type, google_drive_file_id))
            logger.info(f"Successfully added content '{content_name}' to cms_library.")
        except Exception as e:
            logger.error(f"Error adding content '{content_name}' to CMS library: {e}", exc_info=True)
            raise # Re-raise the exception to be handled by the caller (MovieBot)

    @staticmethod
    async def get_content_from_cms_library(content_id: str) -> dict | None:
        """
        Retrieves content details from the content_library based on content_id.
        """
        query = """
        SELECT content_id, content_name, file_type, google_drive_file_id, uploaded_at
        FROM content_library
        WHERE content_id = %s;
        """
        async with Database.pool.acquire() as conn: # FIX: Changed pg_pool to Database.pool
            async with conn.cursor() as cur:
               await cur.execute(query, (content_id,))
               result = await cur.fetchone()

               if result:
                  return {
                    'content_id': result[0],
                    'content_name': result[1],
                    'file_type': result[2],
                    'google_drive_file_id': result[3], # Changed this key
                    'uploaded_at': result[4] # Changed 'created_at' to 'uploaded_at' for consistency with schema
                }
               return None

    @staticmethod
    async def link_content_to_payment(payment_id: str, content_id: str):
        """
        Updates a payment record to link it to a specific content_id.
        """
        query = """
        UPDATE payments
        SET content_id = %s,
            status = 'delivered' -- Optional: Change status to 'delivered' upon linking
        WHERE payment_id = %s;
        """
        await Database.execute_query(query, (content_id, payment_id))


    @staticmethod
    async def get_stats():
        """Returns statistics about users and payments"""
        query = """
        SELECT
            COUNT(DISTINCT p.user_id) AS total_users,
            COUNT(DISTINCT CASE WHEN u.last_active > NOW() - INTERVAL '30 days' THEN p.user_id END) AS active_users,
            COUNT(*) AS total_payments,
            COUNT(CASE WHEN p.status = 'pending' THEN 1 END) AS pending_payments,
            SUM(CASE WHEN p.status = 'completed' THEN amount ELSE 0 END) AS revenue_completed,
            SUM(CASE WHEN p.status = 'pending' THEN amount ELSE 0 END) AS revenue_pending
        FROM payments p
        LEFT JOIN users u ON p.user_id = u.user_id;
        """
        result = await Database.execute_query(query, fetch=True)
        if result:
            # Note: Ensure the column names here match the aliases in the SQL query
            columns = [
                'total_users', 'active_users', 'total_payments',
                'pending_payments', 'revenue_completed', 'revenue_pending'
            ]
            return dict(zip(columns, result[0]))
        return None

    @staticmethod
    async def get_user_payments(user_id: int):
        """Retrieves last 5 payments for a given user."""
        query = """
        SELECT payment_id, request_timestamp, status
        FROM payments
        WHERE user_id = %s
        ORDER BY request_timestamp DESC
        LIMIT 5;
        """
        return await Database.execute_query(query, (user_id,), fetch=True)

    @staticmethod
    async def get_all_payment_ids():
        """Retrieves all payment IDs and their statuses."""
        query = """
        SELECT payment_id, status
        FROM payments
        ORDER BY request_timestamp DESC;
        """
        return await Database.execute_query(query, fetch=True)

    @staticmethod
    async def get_pending_payments_for_admin():
        """Retrieves all pending payments for admin review."""
        query = """
        SELECT payment_id, user_id, amount, currency, request_timestamp
        FROM payments
        WHERE status = 'completed' AND content_id IS NULL
        ORDER BY request_timestamp ASC;
        """
        result = await Database.execute_query(query, fetch=True)
        if result:
            columns = ['payment_id', 'user_id', 'amount', 'currency', 'request_timestamp']
            return [dict(zip(columns, row)) for row in result]
        return []

    @staticmethod
    async def get_user_info(user_id: int):
        """Retrieves user information."""
        query = """
        SELECT user_id, username, first_name, last_name
        FROM users
        WHERE user_id = %s;
        """
        result = await Database.execute_query(query, (user_id,), fetch=True)
        if result:
            columns = ['user_id', 'username', 'first_name', 'last_name']
            return dict(zip(columns, result[0]))
        return None