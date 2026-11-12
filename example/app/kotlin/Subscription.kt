package billing

import java.io.IOException

/** How the customer is told that the subscription was cancelled. */
enum class Notice { Email, InApp }

enum class SubscriptionStatus { Active, Cancelled, Expired }

/** What a cancellation request came to. */
enum class CancelOutcome(val unanswered: Boolean = false) {
    Cancelled, AlreadyCancelled, Declined, TimedOut(true), NetworkError(true), RetryLimitReached
}

enum class ProviderAnswer { Accepted, Declined }

/** The provider did not answer in time. */
class ProviderTimeoutException : Exception("billing provider did not answer in time")

/** A refund of the unused part of a paid period is not something we do. */
class RefundNotAvailableException : Exception("refunds for the unused period are not available")

/** The external billing provider. Throws [ProviderTimeoutException] or [IOException] when it cannot answer. */
interface BillingProvider {
    fun cancelSubscription(customerId: String): ProviderAnswer
}

class SubscriptionService(private val provider: BillingProvider) {

    companion object {
        const val MAX_UNANSWERED_TRIES = 3
    }

    private class Account {
        var status = SubscriptionStatus.Active
        var billingEnabled = true
        var accessUntilPeriodEnd = false
        var notice: Notice? = null
        var unansweredTries = 0
    }

    private val accounts = mutableMapOf<String, Account>()

    fun subscribe(customerId: String) {
        accounts.putIfAbsent(customerId, Account())
    }

    private fun account(customerId: String): Account =
        accounts[customerId] ?: throw NoSuchElementException("no subscription for $customerId")

    fun statusOf(customerId: String): SubscriptionStatus = account(customerId).status
    fun isBilled(customerId: String): Boolean = account(customerId).billingEnabled
    fun hasAccessUntilPeriodEnd(customerId: String): Boolean = account(customerId).accessUntilPeriodEnd
    fun noticeSentTo(customerId: String): Notice? = account(customerId).notice
    fun unansweredTries(customerId: String): Int = account(customerId).unansweredTries

    /**
     * Cancels the subscription with the billing provider. Only the provider's
     * acceptance stops the billing; any other answer leaves the subscription as it was.
     */
    // REQ-001, REQ-002, REQ-003, REQ-004, REQ-007, REQ-008
    fun cancel(customerId: String, notice: Notice): CancelOutcome {
        val account = account(customerId)
        when (account.status) {
            SubscriptionStatus.Cancelled -> return CancelOutcome.AlreadyCancelled // REQ-003
            SubscriptionStatus.Expired -> throw IllegalStateException("the subscription has expired")
            SubscriptionStatus.Active -> {}
        }
        val answer = try {
            provider.cancelSubscription(customerId)
        } catch (e: ProviderTimeoutException) {
            return unanswered(account, CancelOutcome.TimedOut)
        } catch (e: IOException) {
            return unanswered(account, CancelOutcome.NetworkError)
        }
        account.unansweredTries = 0
        return when (answer) {
            ProviderAnswer.Declined -> CancelOutcome.Declined // REQ-008: nothing else moves
            ProviderAnswer.Accepted -> {
                account.status = SubscriptionStatus.Cancelled
                account.billingEnabled = false
                account.accessUntilPeriodEnd = true
                account.notice = notice
                CancelOutcome.Cancelled
            }
        }
    }

    // REQ-004: the retry budget bounds the unanswered tries; the provider has to be asked to learn it did not answer
    private fun unanswered(account: Account, outcome: CancelOutcome): CancelOutcome {
        if (account.unansweredTries >= MAX_UNANSWERED_TRIES) return CancelOutcome.RetryLimitReached
        account.unansweredTries += 1
        return outcome
    }

    /** The paid period of a cancelled subscription is over. */
    // REQ-005
    fun expirePeriod(customerId: String) {
        val account = account(customerId)
        check(account.status == SubscriptionStatus.Cancelled) { "only a cancelled subscription can expire" }
        account.status = SubscriptionStatus.Expired
        account.accessUntilPeriodEnd = false
    }

    // REQ-006
    fun requestRefund(customerId: String) {
        account(customerId)
        throw RefundNotAvailableException()
    }
}
