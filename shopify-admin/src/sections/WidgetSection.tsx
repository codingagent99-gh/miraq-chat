interface Props {
  /** Theme editor deep link with the app embed switched on (embed_url). */
  embedUrl: string;
}

/** Onboarding: turn on the storefront app embed (App Store requirement 5.1.3). */
export default function WidgetSection({ embedUrl }: Props) {
  return (
    <s-section heading="Turn on the chat widget">
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
      <s-button slot="primary-action" variant="primary" href={embedUrl} target="_top">
        Turn on chat widget
      </s-button>
      <s-paragraph color="subdued">
        To turn it off later: theme editor → App embeds → switch off Miraq Shopper Agent → Save.
      </s-paragraph>
    </s-section>
  );
}
