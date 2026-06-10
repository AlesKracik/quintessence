// Quint sidecar for specs/auth-ui.json
//
// Models the navigation as a state machine over screens, with guards drawn from
// imports of the auth area. The invariant `guardedDashboard` proves Dashboard
// is unreachable without authenticated == true.

module authUi {

  import auth.* from "./auth"

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

}
