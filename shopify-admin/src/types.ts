// Shape of GET /shopify/admin/status (routes/shopify_admin.py, _status_payload).

/** _setup_state(): active -> ready, provision_failed / no tenant -> failed,
 *  anything else (warming, transitional) -> indexing. */
export type SetupState = "ready" | "indexing" | "failed";

/** _instagram(): only "available" when Instagram connect is configured
 *  on the backend and the store isn't archived. */
export type InstagramStatus =
  | { available: false }
  | {
      available: true;
      connected: boolean;
      account: string | null;
      /** Facebook Login can't run in the admin iframe — opened in a new tab. */
      connect_url: string;
    };

/** Only the MIRAQ_SUPPORT_* / MIRAQ_PRIVACY_URL values that are set. */
export interface Support {
  email?: string;
  url?: string;
  privacy_url?: string;
}

export interface Status {
  shop: string;
  setup: SetupState;
  /** Theme editor deep link that opens with the app embed switched on. */
  embed_url: string;
  instagram: InstagramStatus;
  support: Support;
}

// GET /shopify/admin/conversations and /shopify/admin/conversations/<id>
// (routes/shopify_admin.py). Read-only; App Store requirement 5.1.5.

export interface ConversationSummary {
  id: string;
  started_at: string;
  last_active_at: string;
  /** Shopify customer id of a signed-in shopper; null for guests. */
  customer_id: string | null;
  message_count: number;
  /** The shopper's first message, shortened. */
  first_question: string;
  /** Pass as `before` to get the conversations after this one. */
  cursor: string;
}

export interface ConversationPage {
  conversations: ConversationSummary[];
  has_more: boolean;
}

export interface TranscriptMessage {
  from: "shopper" | "miraq";
  text: string;
  sent_at: string;
  /** Product cards aren't sent, only how many the shopper saw. */
  products_shown: number;
}

export interface Transcript {
  id: string;
  started_at: string;
  customer_id: string | null;
  messages: TranscriptMessage[];
  /** More messages than the backend returns for one conversation. */
  truncated: boolean;
}
