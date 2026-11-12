import Areas.Auth
import Areas.Subscription

/-- Every area the checker runs. -/
def Areas.all : List Spec.Entry := [Auth.entry, Subscription.entry]
