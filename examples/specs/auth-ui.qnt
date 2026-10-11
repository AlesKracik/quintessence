// The auth-ui area: its model, and every record checkable against it.
//
// Navigation is a state machine over screens. Each screen is a variant of
// `Screen`, documented by a `/// @screen` record on the type; each edge is the
// action that moves `current` between two screens, documented by a
// `/// @nav From -> To` record on that action. spec_source.py derives
// screens[] and navigation[] from them; lint checks every variant has a
// record and every edge's action reads and sets `current` as it claims. The
// invariant `guardedDashboard` proves Dashboard is unreachable without an
// Active session in auth.

module authUi {

  // auth's state and actions, imported QUALIFIED. The UI keeps no idea of
  // its own about who is signed in: the edge that signs in, the one that
  // fails, the one that signs out and the one that expires each CALL the
  // auth action (`auth::login(u, s)`), and auth's guard is the edge's guard.
  // So the invariant below is proven about the sessions auth keeps, not
  // about a local flag nothing else changes — and a journey step in auth is
  // covered by the UI step that performs it. Qualified, not `auth.*`: a
  // wildcard import would bring auth's own `init` and `step`, which collide
  // with this module's.
  import auth from "./auth"

  type UserId    = str
  type SessionId = str

  // -- Screen state machine -----------------------------------------

  /// @screen Home
  /// Public landing; shows 'Sign in' for unauthenticated users.
  /// @components Header
  ///
  /// @screen Login
  /// Email/password form.
  /// @components Header, LoginForm
  ///
  /// @screen Dashboard
  /// Logged-in landing.
  /// @auth-required
  /// @components Header
  ///
  /// @screen LockedNotice
  /// Shown when login fails because the account is locked.
  /// @components Header
  type Screen =
    | Home
    | Login
    | Dashboard
    | LockedNotice

  type FormState =
    | Idle
    | Submitting
    | ErrorBadCreds
    | ErrorLocked

  // -- State --------------------------------------------------------

  var current:   Screen
  var form:      FormState
  // The session this browser holds; "" when signed out. Whether it is
  // still good is auth's business, read through `authenticated`.
  var mySession: SessionId

  pure val USERS:    Set[UserId]    = Set("alice", "bob")
  pure val SESSIONS: Set[SessionId] = Set("s1", "s2")

  // What the UI reads of auth, named here. Signed in = the session this
  // browser holds is Active in auth. (Named rather than written inline
  // because the witness predicates below are inlined into the probe
  // module, which imports this module and not auth.)
  val authenticated: bool = auth::isActive(mySession)
  def accountLocked(u: UserId): bool = auth::isLocked(u)

  // -- Init ---------------------------------------------------------

  action init = all {
    auth::init,
    current'   = Home,
    form'      = Idle,
    mySession' = "",
  }

  // auth's state, left exactly as it is — for the UI steps that do not
  // touch it. Quint requires every variable assigned in every step, the
  // imported ones included.
  action authUnchanged = all {
    auth::sessions'          = auth::sessions,
    auth::sessionOwner'      = auth::sessionOwner,
    auth::accountStatus'     = auth::accountStatus,
    auth::failedAttempts'    = auth::failedAttempts,
    auth::userActiveSession' = auth::userActiveSession,
    auth::idleFor'           = auth::idleFor,
  }

  // -- Navigation actions -------------------------------------------

  /// @nav Home -> Login
  /// click 'Sign in'
  ///
  /// @req UI-001
  /// While on Home, when the user clicks 'Sign in', the system shall
  /// navigate to Login.
  /// @status specified
  /// @when the user clicks 'Sign in'
  /// @while current == Home
  /// @shall current == Login
  action click_sign_in: bool = all {
    current == Home,
    current'   = Login,
    form'      = Idle,
    mySession' = mySession,
    authUnchanged,
  }

  // Signing in IS auth's login (REQ-001): the screen moves because auth
  // accepted it, and auth's guard — account not locked, no other Active
  // session, a fresh session id — is this edge's guard.
  /// @nav Login -> Dashboard
  /// submit valid credentials
  /// @guard auth::login succeeds
  ///
  /// @req UI-002
  /// While on Login, when the user submits valid credentials, the system
  /// shall navigate to Dashboard.
  /// @status specified
  /// @when the user submits valid credentials
  /// @while current == Login
  /// @shall current == Dashboard and authenticated and mySession == s
  action submit_success(u: UserId, s: SessionId): bool = all {
    current == Login,
    auth::login(u, s),
    current'   = Dashboard,
    form'      = Idle,
    mySession' = s,
  }

  // A bad password is auth's failed login (REQ-003): it counts, and the
  // fifth one locks the account.
  /// @nav Login -> Login
  /// submit invalid credentials (stays, shows error)
  action submit_invalid(u: UserId): bool = all {
    current == Login,
    auth::login_failed(u),
    current'   = Login,
    form'      = ErrorBadCreds,
    mySession' = mySession,
  }

  // auth's REQ-005 as the user meets it: the login is refused — auth is
  // left exactly as it was — and the notice says why.
  /// @nav Login -> LockedNotice
  /// submit when account locked
  /// @guard auth::isLocked(user)
  ///
  /// @req UI-003
  /// If the user submits credentials for a Locked account, then the system
  /// shall navigate to LockedNotice.
  /// @status specified
  /// @unwanted
  /// @when the user submits credentials for a Locked account
  /// @while current == Login
  /// @shall current == LockedNotice and form == ErrorLocked and accountLocked(u)
  action submit_locked(u: UserId): bool = all {
    current == Login,
    accountLocked(u),
    current'   = LockedNotice,
    form'      = ErrorLocked,
    mySession' = mySession,
    authUnchanged,
  }

  /// @nav Dashboard -> Home
  /// click logout
  /// @guard auth::logout succeeds
  action click_logout: bool = all {
    current == Dashboard,
    auth::logout(mySession),
    current'   = Home,
    form'      = Idle,
    mySession' = "",
  }

  // auth expires the idle session (REQ-004) and the UI follows it back to
  // the login form.
  /// @nav Dashboard -> Login
  /// the session expires (sign in again)
  /// @guard auth::expire_session succeeds
  ///
  /// @req UI-004
  /// While on Dashboard, when the session expires, the system shall return
  /// to Login.
  /// @status specified
  /// @when the session expires
  /// @while current == Dashboard
  /// @shall current == Login and not(authenticated)
  action session_expired: bool = all {
    current == Dashboard,
    auth::expire_session(mySession),
    current'   = Login,
    form'      = Idle,
    mySession' = "",
  }

  /// @nav LockedNotice -> Home
  /// back to Home
  action go_home: bool = all {
    current == LockedNotice,
    current'   = Home,
    form'      = Idle,
    mySession' = mySession,
    authUnchanged,
  }

  // Time passes on whatever screen is showing: auth's clock ticks, and an
  // idle session gets older.
  action time_passes: bool = all {
    auth::tick,
    current'   = current,
    form'      = form,
    mySession' = mySession,
  }

  // -- Step ---------------------------------------------------------

  action step = {
    nondet u = oneOf(USERS)
    nondet s = oneOf(SESSIONS)
    any {
      click_sign_in,
      submit_success(u, s),
      submit_invalid(u),
      submit_locked(u),
      click_logout,
      session_expired,
      go_home,
      time_passes,
    }
  }

  // -- Invariants ---------------------------------------------------

  // INV-001: Dashboard reachable only when authenticated — and
  // `authenticated` reads auth's sessions, so this is a claim about the
  // real session, not about a flag the UI sets for itself.
  /// @inv INV-001
  /// Dashboard is shown only while the session this browser holds is Active
  /// in auth. ('Shall always' statements are invariants, not requirements —
  /// Apalache proves them across all reachable states.)
  /// @criticality critical
  /// @proof inductive
  val guardedDashboard: bool =
    (current == Dashboard) implies authenticated

  // -- Examples -----------------------------------------------------

  /// @example EX-001
  /// Sign in opens the login page
  /// @refs UI-001
  run openLogin = init
    .then(click_sign_in)
    .expect(current == Login and form == Idle)

  /// @example EX-002
  /// Valid credentials land on the Dashboard
  /// @refs UI-002
  run signInLands = init
    .then(click_sign_in)
    .then(submit_success("alice", "s1"))
    .expect(current == Dashboard and authenticated)

  run happyPathFlow = init
    .then(click_sign_in)
    .then(submit_success("alice", "s1"))
    .then(click_logout)
    .expect(current == Home and not(authenticated))

  /// @example EX-003
  /// Five bad passwords lock the account; the next submit shows the locked
  /// notice
  /// @refs UI-003
  run lockedFlow = init
    .then(click_sign_in)
    .then(submit_invalid("bob"))
    .then(submit_invalid("bob"))
    .then(submit_invalid("bob"))
    .then(submit_invalid("bob"))
    .then(submit_invalid("bob"))
    .then(submit_locked("bob"))
    .expect(current == LockedNotice and form == ErrorLocked)

  /// @example EX-004
  /// An idle session on the Dashboard expires back to the login form
  /// @refs UI-004
  run expiryReturnsToLogin = init
    .then(click_sign_in)
    .then(submit_success("alice", "s1"))
    .then(time_passes)
    .then(time_passes)
    .then(time_passes)
    .then(time_passes)
    .then(session_expired)
    .expect(current == Login and not(authenticated))

  // The "Sign in" journey (specs/journeys/sign-in.journey.json), end to
  // end: alice signs in and out; bob mistypes his password five times,
  // which locks his account (auth's REQ-003), and his next submit lands on
  // the locked notice. Each UI step here IS the auth step it causes, so
  // this one run walks the journey's auth steps and its UI steps alike.
  run signInJourney = init
    .then(click_sign_in)
    .then(submit_success("alice", "s1"))
    .then(click_logout)
    .then(click_sign_in)
    .then(submit_invalid("bob"))
    .then(submit_invalid("bob"))
    .then(submit_invalid("bob"))
    .then(submit_invalid("bob"))
    .then(submit_invalid("bob"))
    .then(submit_locked("bob"))
    .expect(current == LockedNotice and accountLocked("bob"))
}
