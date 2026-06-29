import os
import sqlite3

app_data = os.path.join(os.path.expanduser("~"), ".myapp")
os.makedirs(app_data, exist_ok=True)

DB_PATH = os.path.join(app_data, "chat_history.db")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages
               (id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        # Add session_id column if it doesn't exist (migration for existing DBs)
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN session_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists


def save_msg(role, content, session_id=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )


def get_history(session_id=None):
    with sqlite3.connect(DB_PATH) as conn:
        if session_id:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content FROM messages ORDER BY id ASC"
            ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


def clear_session(session_id):
    """Clear all messages for a specific session."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))


def get_all_sessions():
    """
    Retrieve all unique session IDs with their first user message as title 
    and last activity timestamp.
    """
    with sqlite3.connect(DB_PATH) as conn:
        try:
            # Get sessions with their last activity time and first user message as title
            query = '''
                SELECT 
                    m.session_id,
                    MAX(m.timestamp) as last_active,
                    (SELECT content FROM messages m2 WHERE m2.session_id = m.session_id AND m2.role = 'user' ORDER BY m2.id ASC LIMIT 1) as title
                FROM messages m
                WHERE m.session_id IS NOT NULL
                GROUP BY m.session_id
                ORDER BY last_active DESC
            '''
            rows = conn.execute(query).fetchall()
            
            sessions = []
            for r in rows:
                sid, last_active, title = r
                if not title:
                    title = "New Chat"
                elif len(title) > 30:
                    title = title[:27] + "..."
                    
                sessions.append({
                    "id": sid,
                    "title": title,
                    "timestamp": last_active
                })
            return sessions
        except Exception as e:
            print(f"Error getting sessions: {e}")
            return []
