import type { WidgetState } from "../types";

interface Props {
  /** Theme editor deep link with the app embed switched on (embed_url). */
  embedUrl: string;
  /** Whether the app embed is on in the live theme ("unknown" if it couldn't be read). */
  state: WidgetState;
  /** The store's myshopify domain, for the Test chat link. */
  shop: string;
}

/** Onboarding: turn on the storefront app embed (App Store requirement 5.1.3). */
export default function WidgetSection({ embedUrl, state, shop }: Props) {
  const storeUrl = `https://${shop}`;

  if (state === "on") {
    return (
      <s-section heading="Website chat">
        <s-stack direction="inline" gap="small-200" alignItems="center">
          <s-badge tone="success">On</s-badge>
          <s-text>The chat button shows on every page of your store.</s-text>
        </s-stack>
        <s-stack direction="inline" gap="small-200">
          <s-button href={storeUrl} target="_blank">
            Test chat
          </s-button>
          <s-button variant="tertiary" href={embedUrl} target="_top">
            Open theme editor
          </s-button>
        </s-stack>
        <s-paragraph color="subdued">
          To turn it off: theme editor → App embeds → switch off MiraQ Shopper Agent → Save.
        </s-paragraph>
      </s-section>
    );
  }

  return (
    <s-section heading="Website chat">
      {state === "off" && (
        <s-stack direction="inline" gap="small-200" alignItems="center">
          <s-badge tone="warning">Off</s-badge>
          <s-text>Shoppers can't see the chat yet.</s-text>
        </s-stack>
      )}
      <s-ordered-list>
        <s-list-item>
          Click <s-text type="strong">Turn on chat widget</s-text>. Your theme editor opens with
          the MiraQ app embed already switched on.
        </s-list-item>
        <s-list-item>
          Check the widget in the preview, then click <s-text type="strong">Save</s-text>.
        </s-list-item>
        <s-list-item>Open your store and look for the chat button in the corner.</s-list-item>
      </s-ordered-list>
      <s-stack direction="inline" gap="small-200">
        <s-button variant="primary" href={embedUrl} target="_top">
          Turn on chat widget
        </s-button>
        <s-button href={storeUrl} target="_blank">
          Test chat
        </s-button>
      </s-stack>
    </s-section>
  );
}
