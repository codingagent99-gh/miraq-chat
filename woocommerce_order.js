/**
 * WooCommerce Order Placement Module
 * Integrates with WC REST API v3
 */

const WOO_API_BASE = "https://wgc.net.in/hn/wp-json/wc/v3";

// ============================================
// Configuration - Replace with your credentials
// ============================================
const WOO_CONFIG = {
  consumerKey: "ck_your_consumer_key",
  consumerSecret: "cs_your_consumer_secret",
};

// ============================================
// Product Catalog (Active Products Only)
// ============================================
const PRODUCT_CATALOG = {
  7272: {
    name: "Allspice",
    type: "variable",
    sku: "allspice",
    priceRange: { min: 40, max: 60 },
    variations: [7547, 7548, 7549, 7550],
    categories: ["Countertop", "New Releases", "Wall", "Wall/Floor"],
    colors: [
      "Beleza",
      "Brilho Azul",
      "Calacatta Oro",
      "Creamy Basalt",
      "Luxe White",
      "Mystic Cascade",
      "Namibia White",
      "Nero Marquina",
      "Pietra Grey",
      "Pure White",
      "Sand Basalt",
    ],
    finishes: ["Honed", "Polished", "Silky"],
    sizes: ['5" x 10"'],
    application: ["Countertop"],
    origin: "USA",
  },
  7275: {
    name: "Ansel",
    type: "variable",
    sku: "ansel",
    priceRange: { min: 30, max: 89 },
    variations: [7602, 7603, 7604, 7605, 7606, 7608, 7610, 7611, 7613, 7615],
    categories: ["Exterior", "Floor", "Interior", "Tile", "Wall"],
    colors: ["Charcoal", "Mica", "Smoke", "True White", "Warm White"],
    finishes: ["Matte", "Polished"],
    sizes: ['12"x12"', '12"x24"', '24"x24"', '24"x48"'],
    thickness: ['3/8" (9mm)', '3/8" (9.5mm)'],
    trim: ["Bullnose"],
    application: ["Interior Wall/Floor", "Exterior Wall"],
    quickShip: true,
    variation: "V1",
    origin: "India",
  },
  7276: {
    name: "Ansel Mosaic",
    type: "variable",
    sku: "304",
    priceRange: { min: 40, max: 65 },
    variations: [7637, 7638, 7639, 7640, 7641],
    categories: ["Mosaics", "Porcelain", "Tile Floor", "Tile Wall"],
    colors: [
      "Charcoal Block Mosaic",
      "Mica Block Mosaic",
      "Smoke Block Mosaic",
      "True White Block Mosaic",
      "Warm White Block Mosaic",
    ],
    finishes: ["Matte"],
    sizes: ['2"x2" Block Mosaic (12"x12" Mesh Mount)'],
    thickness: ['3/8" (9mm)'],
    application: ["Interior Wall/Floor/Wet Areas", "Exterior Wall/Floor"],
    quickShip: true,
    origin: "India",
  },
  7265: {
    name: "Cairo Mosaic",
    type: "variable",
    sku: "cairo-mosaic",
    priceRange: { min: 77, max: 100 },
    variations: [7654, 7655, 7656, 7657],
    categories: ["Floor", "Mosaics", "Tile", "Tile Floor", "Tile Wall", "Wall"],
    colors: [
      "Alsumur Block Mosaic",
      "Karima Block Mosaic",
      "Ramad Block Mosaic",
      "Ramil Block Mosaic",
    ],
    finishes: ["Matte"],
    sizes: ['2"x2" Block Mosaic (12"x12" Mesh Mount)'],
    thickness: ['5/16"'],
    application: ["Interior Wall/Floor/Wet Areas", "Exterior Wall"],
    origin: "Unknown",
  },
  7261: {
    name: "Cord",
    type: "variable",
    sku: "cord",
    priceRange: { min: 50, max: 77 },
    variations: [7684, 7685, 7686, 7687, 7688],
    categories: ["Interior", "Tile", "Wall"],
    colors: ["Dark Grey", "Deep Blue", "Sand", "Terracotta", "White"],
    finishes: ["Ribbed"],
    sizes: ['12"x32"'],
    thickness: ['3/8"'],
    application: ["Interior Wall"],
    variation: "V1",
    origin: "Unknown",
  },
  7263: {
    name: "Divine",
    type: "variable",
    sku: "divine",
    priceRange: { min: 10, max: null },
    variations: [], // Need to populate from API
    categories: ["Exterior", "Interior", "Tile", "Wall"],
    colors: [
      "Copper",
      "Copper Chevron",
      "Grey",
      "Grey Chevron",
      "White Chevron",
    ],
    finishes: ["Matte"],
    sizes: ['13"x36"'],
    thickness: ['3/8"'],
    application: ["Interior/Exterior Wall"],
    origin: "Unknown",
  },
};

// ============================================
// Category Mapping
// ============================================
const CATEGORIES = {
  3120: { name: "Countertop", parent: null },
  3104: { name: "Exterior", parent: null },
  3119: { name: "Floor (Exterior)", parent: 3104 },
  3106: { name: "Floor (Interior)", parent: 3105 },
  3107: { name: "Floor", parent: null },
  3105: { name: "Interior", parent: null },
  3113: { name: "Mosaics", parent: null },
  3125: { name: "New Releases", parent: null },
  3117: { name: "Panels", parent: null },
  3118: { name: "Pavers", parent: null },
};

// ============================================
// Auth Header Builder
// ============================================
function getAuthHeader() {
  const credentials = btoa(
    `${WOO_CONFIG.consumerKey}:${WOO_CONFIG.consumerSecret}`,
  );
  return {
    Authorization: `Basic ${credentials}`,
    "Content-Type": "application/json",
  };
}

// ============================================
// Order Builder Class
// ============================================
class WooCommerceOrder {
  constructor() {
    this.lineItems = [];
    this.billing = {};
    this.shipping = {};
    this.customerNote = "";
    this.paymentMethod = "";
    this.shippingLines = [];
  }

  /**
   * Set billing address
   */
  setBilling({
    firstName,
    lastName,
    email,
    phone,
    address1,
    address2 = "",
    city,
    state,
    postcode,
    country = "US",
  }) {
    this.billing = {
      first_name: firstName,
      last_name: lastName,
      email: email,
      phone: phone,
      address_1: address1,
      address_2: address2,
      city: city,
      state: state,
      postcode: postcode,
      country: country,
    };
    return this;
  }

  /**
   * Set shipping address (defaults to billing if not set)
   */
  setShipping({
    firstName,
    lastName,
    address1,
    address2 = "",
    city,
    state,
    postcode,
    country = "US",
  }) {
    this.shipping = {
      first_name: firstName,
      last_name: lastName,
      address_1: address1,
      address_2: address2,
      city: city,
      state: state,
      postcode: postcode,
      country: country,
    };
    return this;
  }

  /**
   * Add a product line item
   * @param {number} productId - The WooCommerce product ID
   * @param {number} quantity - Number of units
   * @param {number|null} variationId - Variation ID for variable products
   * @param {object} meta - Additional meta data (color, finish, size, etc.)
   */
  addItem(productId, quantity, variationId = null, meta = {}) {
    const product = PRODUCT_CATALOG[productId];
    if (!product) {
      throw new Error(`Product ID ${productId} not found in catalog`);
    }

    const item = {
      product_id: productId,
      quantity: quantity,
    };

    if (variationId) {
      if (product.variations && !product.variations.includes(variationId)) {
        throw new Error(
          `Variation ${variationId} is not valid for product ${product.name}`,
        );
      }
      item.variation_id = variationId;
    }

    // Add meta data for attributes
    if (Object.keys(meta).length > 0) {
      item.meta_data = Object.entries(meta).map(([key, value]) => ({
        key: key,
        value: value,
      }));
    }

    this.lineItems.push(item);
    return this;
  }

  /**
   * Set payment method
   */
  setPaymentMethod(method, title = "") {
    this.paymentMethod = method;
    this.paymentMethodTitle = title || method;
    return this;
  }

  /**
   * Add customer note
   */
  setNote(note) {
    this.customerNote = note;
    return this;
  }

  /**
   * Build the order payload
   */
  buildPayload() {
    if (this.lineItems.length === 0) {
      throw new Error("Order must have at least one line item");
    }
    if (!this.billing.email) {
      throw new Error("Billing email is required");
    }

    const payload = {
      payment_method: this.paymentMethod || "cod",
      payment_method_title: this.paymentMethodTitle || "Cash on Delivery",
      set_paid: false,
      billing: this.billing,
      shipping:
        Object.keys(this.shipping).length > 0 ? this.shipping : this.billing,
      line_items: this.lineItems,
      customer_note: this.customerNote,
    };

    if (this.shippingLines.length > 0) {
      payload.shipping_lines = this.shippingLines;
    }

    return payload;
  }

  /**
   * Submit order to WooCommerce
   */
  async placeOrder() {
    const payload = this.buildPayload();

    try {
      const response = await fetch(`${WOO_API_BASE}/orders`, {
        method: "POST",
        headers: getAuthHeader(),
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(
          `Order failed: ${errorData.message || response.statusText}`,
        );
      }

      const orderData = await response.json();
      return {
        success: true,
        orderId: orderData.id,
        orderNumber: orderData.number,
        status: orderData.status,
        total: orderData.total,
        currency: orderData.currency,
        orderUrl: orderData._links.self[0].href,
        lineItems: orderData.line_items.map((li) => ({
          name: li.name,
          quantity: li.quantity,
          price: li.price,
          total: li.total,
        })),
      };
    } catch (error) {
      return {
        success: false,
        error: error.message,
      };
    }
  }
}

// ============================================
// Product Search & Query Functions
// ============================================

/**
 * Search products by category
 */
function getProductsByCategory(categoryName) {
  return Object.entries(PRODUCT_CATALOG)
    .filter(([_, product]) =>
      product.categories.some(
        (cat) => cat.toLowerCase() === categoryName.toLowerCase(),
      ),
    )
    .map(([id, product]) => ({
      id: parseInt(id),
      name: product.name,
      priceRange: product.priceRange,
      colors: product.colors,
    }));
}

/**
 * Search products by finish
 */
function getProductsByFinish(finish) {
  return Object.entries(PRODUCT_CATALOG)
    .filter(([_, product]) =>
      product.finishes.some((f) => f.toLowerCase() === finish.toLowerCase()),
    )
    .map(([id, product]) => ({
      id: parseInt(id),
      name: product.name,
      priceRange: product.priceRange,
      finishes: product.finishes,
    }));
}

/**
 * Search products by color
 */
function getProductsByColor(color) {
  const colorLower = color.toLowerCase();
  return Object.entries(PRODUCT_CATALOG)
    .filter(([_, product]) =>
      product.colors.some((c) => c.toLowerCase().includes(colorLower)),
    )
    .map(([id, product]) => ({
      id: parseInt(id),
      name: product.name,
      matchingColors: product.colors.filter((c) =>
        c.toLowerCase().includes(colorLower),
      ),
    }));
}

/**
 * Search products by application
 */
function getProductsByApplication(application) {
  const appLower = application.toLowerCase();
  return Object.entries(PRODUCT_CATALOG)
    .filter(([_, product]) =>
      product.application.some((a) => a.toLowerCase().includes(appLower)),
    )
    .map(([id, product]) => ({
      id: parseInt(id),
      name: product.name,
      application: product.application,
    }));
}

/**
 * Get product details
 */
function getProductDetails(productId) {
  const product = PRODUCT_CATALOG[productId];
  if (!product) return null;
  return { id: productId, ...product };
}

/**
 * Get products sorted by price
 */
function getProductsByPrice(order = "asc") {
  return Object.entries(PRODUCT_CATALOG)
    .map(([id, product]) => ({
      id: parseInt(id),
      name: product.name,
      minPrice: product.priceRange.min,
      maxPrice: product.priceRange.max,
    }))
    .sort((a, b) =>
      order === "asc" ? a.minPrice - b.minPrice : b.minPrice - a.minPrice,
    );
}

/**
 * Get Quick Ship products
 */
function getQuickShipProducts() {
  return Object.entries(PRODUCT_CATALOG)
    .filter(([_, product]) => product.quickShip === true)
    .map(([id, product]) => ({
      id: parseInt(id),
      name: product.name,
      priceRange: product.priceRange,
    }));
}

/**
 * Fetch variation details from API
 */
async function getVariationDetails(productId, variationId) {
  try {
    const response = await fetch(
      `${WOO_API_BASE}/products/${productId}/variations/${variationId}`,
      { headers: getAuthHeader() },
    );
    if (!response.ok) throw new Error("Failed to fetch variation");
    return await response.json();
  } catch (error) {
    return { error: error.message };
  }
}

/**
 * Fetch all variations for a product
 */
async function getAllVariations(productId) {
  try {
    const response = await fetch(
      `${WOO_API_BASE}/products/${productId}/variations?per_page=100`,
      { headers: getAuthHeader() },
    );
    if (!response.ok) throw new Error("Failed to fetch variations");
    const variations = await response.json();
    return variations.map((v) => ({
      id: v.id,
      price: v.price,
      regularPrice: v.regular_price,
      salePrice: v.sale_price,
      attributes: v.attributes,
      stockStatus: v.stock_status,
    }));
  } catch (error) {
    return { error: error.message };
  }
}

// ============================================
// Cart Management
// ============================================
class ShoppingCart {
  constructor() {
    this.items = [];
  }

  addToCart(productId, quantity, variationId = null, attributes = {}) {
    const product = PRODUCT_CATALOG[productId];
    if (!product) {
      return { success: false, error: `Product ${productId} not found` };
    }

    const existingIndex = this.items.findIndex(
      (item) =>
        item.productId === productId && item.variationId === variationId,
    );

    if (existingIndex >= 0) {
      this.items[existingIndex].quantity += quantity;
    } else {
      this.items.push({
        productId,
        productName: product.name,
        quantity,
        variationId,
        attributes,
        priceRange: product.priceRange,
      });
    }

    return {
      success: true,
      message: `Added ${quantity}x ${product.name} to cart`,
      cartTotal: this.items.length,
    };
  }

  removeFromCart(productId, variationId = null) {
    const initialLength = this.items.length;
    this.items = this.items.filter(
      (item) =>
        !(item.productId === productId && item.variationId === variationId),
    );
    return {
      success: this.items.length < initialLength,
      cartTotal: this.items.length,
    };
  }

  updateQuantity(productId, quantity, variationId = null) {
    const item = this.items.find(
      (item) =>
        item.productId === productId && item.variationId === variationId,
    );
    if (item) {
      if (quantity <= 0) {
        return this.removeFromCart(productId, variationId);
      }
      item.quantity = quantity;
      return { success: true, quantity: item.quantity };
    }
    return { success: false, error: "Item not found in cart" };
  }

  getCart() {
    return {
      items: this.items,
      itemCount: this.items.reduce((sum, item) => sum + item.quantity, 0),
      estimatedMin: this.items.reduce(
        (sum, item) => sum + item.priceRange.min * item.quantity,
        0,
      ),
      estimatedMax: this.items.reduce(
        (sum, item) =>
          sum + (item.priceRange.max || item.priceRange.min) * item.quantity,
        0,
      ),
    };
  }

  clearCart() {
    this.items = [];
    return { success: true, message: "Cart cleared" };
  }

  /**
   * Convert cart to WooCommerce order
   */
  async checkout(billingInfo, shippingInfo = null, paymentMethod = "cod") {
    const order = new WooCommerceOrder();

    order.setBilling(billingInfo);
    if (shippingInfo) {
      order.setShipping(shippingInfo);
    }
    order.setPaymentMethod(paymentMethod);

    for (const item of this.items) {
      order.addItem(
        item.productId,
        item.quantity,
        item.variationId,
        item.attributes,
      );
    }

    const result = await order.placeOrder();

    if (result.success) {
      this.clearCart();
    }

    return result;
  }
}

// ============================================
// Usage Examples
// ============================================

/*
// --- Example 1: Direct Order ---
const order = new WooCommerceOrder();
order
  .setBilling({
    firstName: "John",
    lastName: "Doe",
    email: "john@example.com",
    phone: "555-1234",
    address1: "123 Main St",
    city: "Dallas",
    state: "TX",
    postcode: "75201",
    country: "US",
  })
  .addItem(7275, 2, 7602)  // 2x Ansel (variation 7602)
  .addItem(7261, 1, 7684)  // 1x Cord (variation 7684)
  .setPaymentMethod("cod", "Cash on Delivery")
  .setNote("Please deliver before Friday");

const result = await order.placeOrder();
console.log(result);


// --- Example 2: Cart-based Order ---
const cart = new ShoppingCart();

cart.addToCart(7272, 3, 7547, { color: "Brilho Azul" });
cart.addToCart(7276, 2, 7637, { color: "True White Block Mosaic" });

console.log(cart.getCart());

const checkoutResult = await cart.checkout({
  firstName: "Jane",
  lastName: "Smith",
  email: "jane@example.com",
  phone: "555-5678",
  address1: "456 Oak Ave",
  city: "Houston",
  state: "TX",
  postcode: "77001",
  country: "US",
});
console.log(checkoutResult);


// --- Example 3: Product Queries ---
console.log(getProductsByCategory("Mosaics"));
console.log(getProductsByFinish("Matte"));
console.log(getProductsByColor("white"));
console.log(getProductsByApplication("countertop"));
console.log(getProductsByPrice("asc"));
console.log(getQuickShipProducts());
console.log(getProductDetails(7275));
*/

// ============================================
// Exports
// ============================================
export {
  WooCommerceOrder,
  ShoppingCart,
  PRODUCT_CATALOG,
  CATEGORIES,
  getProductsByCategory,
  getProductsByFinish,
  getProductsByColor,
  getProductsByApplication,
  getProductDetails,
  getProductsByPrice,
  getQuickShipProducts,
  getVariationDetails,
  getAllVariations,
};
