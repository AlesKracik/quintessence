/// No model yet, so the requirements below are `raw`: the sentence and
/// the @when words are the claim until /spec formalizes each one on the
/// action it governs (formal @while / @shall).
///
/// @req REQ-001
/// While the cart is Open, when a shopper adds a SKU already in the cart,
/// the system shall increase that line's quantity.
/// @status raw
/// @modality must
/// @source extracted
/// @when a shopper adds a SKU already present
/// @evidence code/legacy-cart/cart.js:31-35
/// @confidence high
/// @inferred-by agent
///
/// @req REQ-002
/// While the cart is Open and holds fewer than MAX_ITEMS lines, when a
/// shopper adds a new SKU, the system shall append a line for it.
/// @status raw
/// @modality must
/// @source extracted
/// @when a shopper adds a new SKU
/// @evidence code/legacy-cart/cart.js:26-36
/// @confidence high
/// @inferred-by agent
///
/// @req REQ-003
/// If a shopper adds an item to a CheckedOut cart, then the system shall
/// refuse the call and change nothing.
/// @status raw
/// @modality forbidden
/// @source extracted
/// @when a shopper adds an item
/// @unwanted
/// @evidence code/legacy-cart/cart.js:22-24
/// @confidence high
/// @inferred-by agent
///
/// @req REQ-004
/// If the payment gateway is unavailable during checkout, then the system
/// shall leave the cart Open and report that a retry is possible.
/// @status raw
/// @modality must
/// @source extracted
/// @when the payment gateway throws during checkout
/// @unwanted
/// @error PaymentGateway.OK not-idempotent: CheckedOut
/// @error PaymentGateway.DECLINED idempotent: NO_CHANGE — the cart stays
///   Open; reason 'declined'
/// @error PaymentGateway.UNAVAILABLE idempotent: NO_CHANGE — the cart stays
///   Open; reason 'unavailable'; caller retries
/// @error-note Retry safety rests on ASM-001, which is carried as a known
///   risk.
/// @evidence code/legacy-cart/cart.js:58-62
/// @confidence medium
/// @inferred-by agent
///
/// @inv INV-001
/// A CheckedOut cart's items never change.
/// @criticality critical
/// @quint-name checkedOutIsFrozen
/// @evidence code/legacy-cart/cart.js:22-24
/// @confidence high
/// @inferred-by agent
///
/// @inv INV-002
/// A cart never holds more than MAX_ITEMS distinct lines.
/// @criticality high
/// @quint-name underItemCap
/// @evidence code/legacy-cart/cart.js:26-28
/// @confidence high
/// @inferred-by agent
///
/// @con CON-001
/// Maximum distinct lines in a cart. Read off the code — no document
/// mentioned it.
/// @name MAX_ITEMS
/// @pairs INV-002
/// @value 20
/// @evidence code/legacy-cart/cart.js:10
/// @confidence high
/// @inferred-by agent
///
/// @example EX-001
/// Adding a SKU twice merges the lines
/// @given {"cart.status": "Open", "cart.items": "[{sku: 'a', qty: 1}]"}
/// @when add_item {"sku": "a", "qty": 2}
/// @expect {"cart.items": "[{sku: 'a', qty: 3}]"}
/// @refs REQ-001
/// @source extracted-from-production
///
/// @example EX-002
/// Adding to a checked-out cart is refused and the cart is untouched
/// @given {"cart.status": "CheckedOut", "cart.items": "[{sku: 'a', qty: 1}]"}
/// @when add_item {"sku": "b", "qty": 1}
/// @expect {"result": "refused", "cart.items": "[{sku: 'a', qty: 1}]"}
/// @refs REQ-003
/// @source extracted
module cart {
}
