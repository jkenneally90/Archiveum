"""Session management for Public Mode - isolates user conversations and data.

Each session maintains:
- Unique session ID (UUID4, non-guessable)
- Isolated chat history
- Temporary memory/context
- Voice interaction state
- Last activity timestamp (for expiry)

Sessions expire after inactivity timeout (default 30 minutes).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta


@dataclass
class Session:
    """Represents a user session with isolated conversation data."""
    session_id: str  # Unique, non-guessable session ID
    created_at: float  # Unix timestamp
    last_activity: float  # Unix timestamp for expiry tracking
    chat_history: list[dict[str, Any]] = field(default_factory=list)
    voice_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)  # For extensibility
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "chat_history": self.chat_history,
            "voice_state": self.voice_state,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            session_id=data["session_id"],
            created_at=data["created_at"],
            last_activity=data["last_activity"],
            chat_history=data.get("chat_history", []),
            voice_state=data.get("voice_state", {}),
            metadata=data.get("metadata", {}),
        )
    
    def is_expired(self, timeout_minutes: int) -> bool:
        """Check if session has expired based on inactivity."""
        inactive_seconds = time.time() - self.last_activity
        return inactive_seconds > (timeout_minutes * 60)
    
    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = time.time()


class SessionManager:
    """Manages user sessions with automatic expiry and persistence."""
    
    def __init__(self, data_dir: Path, session_timeout_minutes: int = 30):
        self._data_dir = data_dir
        self._session_timeout_minutes = session_timeout_minutes
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self._sessions_file = data_dir / "sessions.json"
        
        # Ensure data directory exists
        self._data_dir.mkdir(parents=True, exist_ok=True)
        
        # Load existing sessions
        self._load_sessions()
        
        # Start cleanup thread
        self._start_cleanup_thread()
    
    def _generate_session_id(self) -> str:
        """Generate a secure, non-guessable session ID."""
        # Use secrets.token_urlsafe for cryptographically secure random IDs
        # 32 bytes = ~43 characters, plenty of entropy
        return secrets.token_urlsafe(32)
    
    def create_session(self, metadata: dict[str, Any] | None = None) -> Session:
        """Create a new session with unique ID."""
        with self._lock:
            session_id = self._generate_session_id()
            now = time.time()
            session = Session(
                session_id=session_id,
                created_at=now,
                last_activity=now,
                metadata=metadata or {},
            )
            self._sessions[session_id] = session
            self._save_sessions()
            return session
    
    def get_session(self, session_id: str | None) -> Session | None:
        """Get session by ID, or None if not found/expired."""
        if not session_id:
            return None
        
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            
            # Check expiry
            if session.is_expired(self._session_timeout_minutes):
                self._delete_session(session_id)
                return None
            
            # Update activity
            session.touch()
            return session
    
    def validate_session(self, session_id: str | None) -> bool:
        """Check if session exists and is valid (not expired)."""
        return self.get_session(session_id) is not None
    
    def delete_session(self, session_id: str) -> bool:
        """Delete a session (e.g., on logout or browser close)."""
        with self._lock:
            return self._delete_session(session_id)
    
    def _delete_session(self, session_id: str) -> bool:
        """Internal delete - must hold lock."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            self._save_sessions()
            return True
        return False
    
    def clear_chat_history(self, session_id: str) -> bool:
        """Clear chat history for a specific session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.chat_history = []
                session.touch()
                self._save_sessions()
                return True
            return False
    
    def add_chat_message(self, session_id: str, role: str, text: str, 
                        context: str = "", source: str = "text") -> bool:
        """Add a message to session's chat history."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return False
            
            session.chat_history.append({
                "role": role,
                "text": text,
                "context": context,
                "source": source,
                "timestamp": time.time(),
            })
            session.touch()
            self._save_sessions()
            return True
    
    def get_chat_history(self, session_id: str) -> list[dict[str, Any]]:
        """Get chat history for a session."""
        with self._lock:
            session = self.get_session(session_id)
            if session:
                return list(session.chat_history)
            return []
    
    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        with self._lock:
            expired = [
                sid for sid, session in self._sessions.items()
                if session.is_expired(self._session_timeout_minutes)
            ]
            for sid in expired:
                del self._sessions[sid]
            if expired:
                self._save_sessions()
                print(f"[SessionManager] Cleaned up {len(expired)} expired sessions")
    
    def _start_cleanup_thread(self) -> None:
        """Start background thread for periodic cleanup."""
        def cleanup_loop():
            while True:
                time.sleep(300)  # Check every 5 minutes
                try:
                    self._cleanup_expired()
                except Exception as e:
                    print(f"[SessionManager] Cleanup error: {e}")
        
        thread = threading.Thread(target=cleanup_loop, daemon=True)
        thread.start()
    
    def _save_sessions(self) -> None:
        """Persist sessions to disk."""
        try:
            data = {
                sid: session.to_dict()
                for sid, session in self._sessions.items()
            }
            self._sessions_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as e:
            print(f"[SessionManager] Save error: {e}")
    
    def _load_sessions(self) -> None:
        """Load sessions from disk, filtering expired ones."""
        if not self._sessions_file.exists():
            return
        
        try:
            data = json.loads(self._sessions_file.read_text(encoding="utf-8"))
            now = time.time()
            loaded = 0
            expired = 0
            
            for sid, session_data in data.items():
                session = Session.from_dict(session_data)
                # Only load non-expired sessions
                if not session.is_expired(self._session_timeout_minutes):
                    self._sessions[sid] = session
                    loaded += 1
                else:
                    expired += 1
            
            print(f"[SessionManager] Loaded {loaded} sessions, discarded {expired} expired")
        except Exception as e:
            print(f"[SessionManager] Load error: {e}")
    
    def get_stats(self) -> dict[str, Any]:
        """Get session manager statistics."""
        with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "session_timeout_minutes": self._session_timeout_minutes,
                "storage_file": str(self._sessions_file),
            }


# Global session manager instance (initialized in webapp.py)
_session_manager: SessionManager | None = None


def get_session_manager(data_dir: Path | None = None, 
                        session_timeout_minutes: int = 30) -> SessionManager:
    """Get or create the global session manager."""
    global _session_manager
    if _session_manager is None:
        if data_dir is None:
            from archiveum.config import build_paths
            data_dir = build_paths().data_dir
        _session_manager = SessionManager(data_dir, session_timeout_minutes)
    return _session_manager


def reset_session_manager() -> None:
    """Reset the global session manager (for testing)."""
    global _session_manager
    _session_manager = None
