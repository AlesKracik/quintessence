// Quint sidecar for specs/auth-ui.json
//
// Models the navigation as a state machine over screens, with guards drawn from
// imports of the auth area. The invariant `guardedDashboard` proves Dashboard
// is unreachable without authenticated == true.

module authUi {

  // NO wildcard import of the auth module. `import auth.* from "./auth"` pulls
  // in that module's `init` and `step`, which collide with this module's own —
  // and nothing here actually references an auth symbol: `authenticated` is
  // modelled locally as the UI's view of session state. If a UI invariant ever
  // needs auth's state, import it QUALIFIED (`import auth from "./auth"`) and
  // write `auth::sessions`, so the two step relations stay distinct.

  // -- Screen state machine -----------------------------------------

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

  var current:       Screen
  var form:          FormState
  var authenticated: bool         // true when the user has an Active session in auth

  // -- Init ---------------------------------------------------------

  action init = all {
    current'       = Home,
    form'          = Idle,
    authenticated' = false,
  }

  // -- Navigation actions -------------------------------------------

  // UI-001: Click 'Sign in' on Home → go to Login.
  action click_sign_in: bool = all {
    current == Home,
    current'       = Login,
    form'          = Idle,
    authenticated' = authenticated,
  }

  // UI-002: Submit valid credentials on Login → Dashboard.
  // Guard: not locked, credentials are valid. Modeled as a precondition.
  action submit_success: bool = all {
    current == Login,
    not(authenticated),                       // not already logged in
    // Assumption: credentials would be checked against auth's User store.
    current'       = Dashboard,
    form'          = Idle,
    authenticated' = true,
  }

  // UI-003: Submit when account is Locked → LockedNotice.
  action submit_locked: bool = all {
    current == Login,
    not(authenticated),
    current'       = LockedNotice,
    form'          = ErrorLocked,
    authenticated' = authenticated,
  }

  // Submit invalid credentials → stay on Login, show error.
  action submit_invalid: bool = all {
    current == Login,
    not(authenticated),
    current'       = Login,
    form'          = ErrorBadCreds,
    authenticated' = authenticated,
  }

  // Click logout on Dashboard → Home.
  action click_logout: bool = all {
    current == Dashboard,
    authenticated,
    current'       = Home,
    form'          = Idle,
    authenticated' = false,
  }

  // After lockout expires, the user refreshes; LockedNotice → Home.
  action go_home: bool = all {
    current == LockedNotice,
    current'       = Home,
    form'          = Idle,
    authenticated' = authenticated,
  }

  // -- Step ---------------------------------------------------------

  action step = any {
    click_sign_in,
    submit_success,
    submit_locked,
    submit_invalid,
    click_logout,
    go_home,
  }

  // -- Invariants ---------------------------------------------------

  // INV-001: Dashboard reachable only when authenticated.
  // Apalache will verify this holds across all reachable states.
  /// @inv INV-001
  /// Dashboard is reachable only when authenticated == true. ('Shall always'
  /// statements are invariants, not requirements — Apalache proves them
  /// across all reachable states.)
  /// @criticality critical
  /// @proof inductive
  val guardedDashboard: bool =
    (current == Dashboard) implies authenticated

  // -- Scenarios ----------------------------------------------------

  run happyPathFlow = init
    .then(click_sign_in)
    .then(submit_success)
    .then(click_logout)
    .expect(current == Home and not(authenticated))

  run lockedFlow = init
    .then(click_sign_in)
    .then(submit_locked)
    .expect(current == LockedNotice)

  // ----------------------------------------------------------
  // REQUIREMENTS — witness predicates, constants and transition
  // properties whose records live on them (spec_source.py)
  // ----------------------------------------------------------

  /// @req UI-001
  /// While on Home, when the user clicks 'Sign in', the system shall
  /// navigate to Login.
  /// @status specified
  /// @while the user is on Home
  /// @when the user clicks 'Sign in'
  /// @shall navigate to Login
  /// @meaning From the home page, the Sign in button takes the user to the
  ///   login page. Nothing else happens yet — no credentials have been
  ///   entered.
  /// @meaning-author agent
  /// @via click_sign_in
  val shall_UI_001: bool =
    current == Login

  /// @req UI-002
  /// While on Login, when the user submits valid credentials, the system
  /// shall navigate to Dashboard.
  /// @status specified
  /// @while the user is on Login
  /// @when the user submits valid credentials
  /// @shall navigate to Dashboard
  /// @meaning A user who enters credentials that check out is taken straight
  ///   to the dashboard; the login page does not stay in the way.
  /// @meaning-author agent
  /// @via submit_success
  val shall_UI_002: bool =
    current == Dashboard

  /// @req UI-003
  /// If the user submits credentials for a Locked account, then the system
  /// shall navigate to LockedNotice.
  /// @status specified
  /// @while the user is on Login
  /// @when the user submits credentials for a Locked account
  /// @unwanted
  /// @shall navigate to LockedNotice
  /// @meaning If the credentials belong to an account that has been locked,
  ///   the user is sent to a page that tells them so, rather than to the
  ///   dashboard or back to a login form that will never work.
  /// @meaning-author agent
  /// @via submit_locked
  val shall_UI_003: bool =
    current == LockedNotice
}
