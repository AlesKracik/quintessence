"""Sessions and account lockout for one service. In-memory; time comes from an injected clock."""

MAX_FAILED_ATTEMPTS = 5   # failed sign-ins that lock an account
SESSION_TIMEOUT = 3       # time units a session may sit idle before it can be expired


class AuthError(Exception):
    pass


class Session:
    def __init__(self, sid, user, now):
        self.sid = sid
        self.user = user
        self.status = "active"       # active | expired | logged_out
        self.last_seen = now
        self.idle_at_end = 0         # idle time when the session stopped being active


class AuthService:
    def __init__(self, clock):
        self._clock = clock
        self._sessions = {}          # session id -> Session; ids are never reused
        self._failed = {}            # user -> consecutive failed sign-ins
        self._locked = set()         # users whose account is locked

    def _idle(self, session):
        if session.status == "active":
            return self._clock() - session.last_seen
        return session.idle_at_end

    def _end(self, session, status):
        session.idle_at_end = self._idle(session)
        session.status = status

    def _active_of(self, user):
        return [s for s in self._sessions.values() if s.user == user and s.status == "active"]

    def _active(self, sid):
        session = self._sessions.get(sid)
        if session is None or session.status != "active":
            raise AuthError("no such active session")
        return session

    # REQ-001, REQ-005
    def login(self, user, sid):
        if user in self._locked:
            raise AuthError("account is locked")
        if sid in self._sessions:
            raise AuthError("session id already used")
        if self._active_of(user):
            raise AuthError("user already has an active session")
        self._sessions[sid] = Session(sid, user, self._clock())
        self._failed[user] = 0

    # REQ-003, REQ-006
    def record_failed_login(self, user):
        if user in self._locked:
            raise AuthError("account is locked")
        self._failed[user] = self._failed.get(user, 0) + 1
        if self._failed[user] >= MAX_FAILED_ATTEMPTS:
            self._locked.add(user)
            for session in self._active_of(user):
                self._end(session, "logged_out")

    # REQ-002
    def logout(self, sid):
        self._end(self._active(sid), "logged_out")

    # REQ-004
    def expire(self, sid):
        session = self._active(sid)
        if self._idle(session) <= SESSION_TIMEOUT:
            raise AuthError("session has not been idle long enough")
        self._end(session, "expired")

    def unlock(self, user):
        if user not in self._locked:
            raise AuthError("account is not locked")
        self._locked.discard(user)
        self._failed[user] = 0

    def is_locked(self, user):
        return user in self._locked

    def failed_attempts(self, user):
        return self._failed.get(user, 0)

    def active_session(self, user):
        active = self._active_of(user)
        return active[0].sid if active else None

    def session_status(self, sid):
        session = self._sessions.get(sid)
        return session.status if session else None

    def session_user(self, sid):
        session = self._sessions.get(sid)
        return session.user if session else None

    def idle_time(self, sid):
        session = self._sessions.get(sid)
        return self._idle(session) if session else 0
