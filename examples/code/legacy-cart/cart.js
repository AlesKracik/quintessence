// EXAMPLE ONLY — pretend this is the code that was already there.
//
// It exists so the brownfield beat has something real to extract from:
// tools/spec-extract-audit.py scans it, and every decision site below is
// accounted for in examples/specs/cart.area.json's extraction_triage[].
//
// Deliberately written the way real code is: a threshold nobody documented,
// a rejection with no test, a defensive branch, some logging, and one line
// of genuinely dead code.

const MAX_ITEMS = 20;

class Cart {
  constructor(store, logger) {
    this.store = store;
    this.logger = logger;
  }

  addItem(cartId, sku, qty) {
    const cart = this.store.get(cartId);

    if (cart.status === "checked_out") {
      throw new Error("cart is closed");
    }

    if (cart.items.length >= MAX_ITEMS) {
      throw new Error("cart is full");
    }

    const existing = cart.items.find((i) => i.sku === sku);
    if (existing) {
      existing.qty += qty;
    } else {
      cart.items.push({ sku, qty });
    }

    this.logger.info("item added", { cartId, sku });
    this.store.put(cartId, cart);
    return cart;
  }

  checkout(cartId) {
    const cart = this.store.get(cartId);

    if (cart.items.length === 0) {
      throw new Error("cart is empty");
    }

    try {
      const result = this.payments.charge(cartId, total(cart));
      if (result.status === "declined") {
        return { ok: false, reason: "declined" };
      }
      cart.status = "checked_out";
      this.store.put(cartId, cart);
      return { ok: true };
    } catch (err) {
      // Timeout or transport failure: the charge may or may not have landed,
      // so the cart is left open and the caller retries.
      this.logger.warn("charge failed", { cartId, err });
      return { ok: false, reason: "unavailable" };
    }
  }

  clear(cartId) {
    const cart = this.store.get(cartId);
    if (!cart) {
      return null; // defensive: store.get always returns a cart today
    }
    cart.items = [];
    this.store.put(cartId, cart);
    return cart;
  }
}

function total(cart) {
  return cart.items.reduce((sum, i) => sum + i.qty, 0);
}

module.exports = { Cart, MAX_ITEMS };
