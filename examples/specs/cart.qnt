/// @req REQ-001
/// While the cart is Open, when a shopper adds a SKU already in the cart,
/// the system shall increase that line's quantity.
/// @status needs-validation
/// @modality must
/// @source extracted
/// @while the Cart is Open
/// @when a shopper adds a SKU already present
/// @shall increase that line's quantity by the amount added
/// @via add_item
/// @evidence code/legacy-cart/cart.js:31-35
/// @confidence high
/// @inferred-by agent
///
/// @req REQ-002
/// While the cart is Open and holds fewer than MAX_ITEMS lines, when a
/// shopper adds a new SKU, the system shall append a line for it.
/// @status needs-validation
/// @modality must
/// @source extracted
/// @while the Cart is Open and holds fewer than MAX_ITEMS lines
/// @when a shopper adds a new SKU
/// @shall append a line for that SKU
/// @via add_item
/// @evidence code/legacy-cart/cart.js:26-36
/// @confidence high
/// @inferred-by agent
///
/// @req REQ-003
/// If a shopper adds an item to a CheckedOut cart, then the system shall
/// refuse the call and change nothing.
/// @status needs-validation
/// @modality forbidden
/// @source extracted
/// @while the Cart is CheckedOut
/// @when a shopper adds an item
/// @unwanted
/// @shall refuse the call and leave the cart untouched
/// @via add_item
/// @enforced-by INV-001
/// @witness skipped: A refusal has no reachable state to witness. INV-001
///   carries the model-side proof.
/// @evidence code/legacy-cart/cart.js:22-24
/// @confidence high
/// @inferred-by agent
///
/// @req REQ-004
/// If the payment gateway is unavailable during checkout, then the system
/// shall leave the cart Open and report that a retry is possible.
/// @status needs-validation
/// @modality must
/// @source extracted
/// @while the Cart is Open
/// @when the payment gateway throws during checkout
/// @unwanted
/// @shall leave the Cart Open and return ok:false with reason 'unavailable'
/// @error PaymentGateway.OK not-idempotent: CheckedOut
/// @error PaymentGateway.DECLINED idempotent: NO_CHANGE — the cart stays
///   Open; reason 'declined'
/// @error PaymentGateway.UNAVAILABLE idempotent: NO_CHANGE — the cart stays
///   Open; reason 'unavailable'; caller retries
/// @error-note Retry safety rests on ASM-001, which is carried as a known
///   risk.
/// @via checkout_unavailable
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
module cart {
}
