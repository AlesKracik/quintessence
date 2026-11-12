import Areas.Auth
import Areas.Subscription
import Areas.Reservation

/-- Every area the checker runs. -/
def Areas.all : List Spec.Entry := [Auth.entry, Subscription.entry, Reservation.entry]
