// Shape of GET /shopify/admin/status (routes/shopify_admin.py, _status_payload).

/** _setup_state(): active -> ready, provision_failed / no tenant -> failed,
 *  anything else (warming, transitional) -> indexing. */
export type SetupState = "ready" | "indexing" | "failed";

/** The merchant's Instagram switches (ChannelConnection.settings). */
export interface InstagramSettings {
  /** Off pauses MiraQ on Instagram. */
  ai_replies: boolean;
  /** Emoji-only messages get no reply. */
  text_only: boolean;
}

/** _instagram(): only "available" when Instagram connect is configured
 *  on the backend and the store isn't archived. */
export type InstagramStatus =
  | { available: false }
  | {
      available: true;
      connected: false;
      account: null;
      /** Facebook Login can't run in the admin iframe — opened in a pop-up,
       *  with &attempt=<id> added; the outcome is read back from
       *  GET /shopify/admin/instagram/attempt. */
      connect_url: string;
    }
  | {
      available: true;
      connected: true;
      /** "@handle" */
      account: string;
      /** The Facebook Page linked to the account. */
      page_name: string | null;
      /** The Facebook user who connected it. */
      connected_by: string | null;
      /** No saved token, or Meta rejected it: replies can't be sent. */
      needs_reconnect: boolean;
      settings: InstagramSettings;
      connect_url: string;
    };

/** Whether the chat app embed is switched on in the live theme. "unknown"
 *  when the theme couldn't be read (e.g. read_themes not yet approved). */
export type WidgetState = "on" | "off" | "unknown";

// GET /shopify/admin/instagram/attempt and POST /shopify/admin/instagram/choose
// (routes/shopify_admin.py, instagram_connect.attempt_view / choose_for_attempt).

/** "waiting" until the pop-up's outcome is recorded; "expired" after 10 minutes. */
export type InstagramAttemptStatus =
  | "waiting"
  | "expired"
  | "connected"
  | "choose"
  | "cancelled"
  | "no_account"
  | "taken"
  | "failed"
  | "error";

/** One account to choose from. No tokens ever reach the page. */
export interface InstagramAccountChoice {
  id: string;
  /** "@handle" */
  label: string;
  /** The Facebook Page it is linked to. */
  page: string;
}

export interface InstagramAttempt {
  status: InstagramAttemptStatus;
  /** "@handle" for connected / taken / failed. */
  account?: string | null;
  /** Only for "choose". */
  accounts?: InstagramAccountChoice[];
}

export interface InstagramChooseResult {
  attempt: InstagramAttempt;
  status: Status;
}

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
  widget: WidgetState;
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
