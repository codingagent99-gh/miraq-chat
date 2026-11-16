import type { InstagramStatus } from "../types";

interface Props {
  instagram: Extract<InstagramStatus, { available: true }>;
  working: boolean;
  onDisconnect: () => void;
}

/** Connect / disconnect the store's Instagram account (routes/instagram_connect.py). */
export default function InstagramSection({ instagram, working, onDisconnect }: Props) {
  if (instagram.connected) {
    return (
      <s-section heading="Instagram">
        <s-stack direction="inline" gap="small-200" alignItems="center">
          <s-badge tone="success">Connected</s-badge>
          <s-text type="strong">{instagram.account}</s-text>
        </s-stack>
        <s-paragraph>Customers who message this account get answers from MiraQ.</s-paragraph>
        <s-stack direction="inline" gap="small-200">
          <s-button href={instagram.connect_url} target="_blank">
            Connect a different account
          </s-button>
          <s-button tone="critical" variant="tertiary" onClick={onDisconnect} disabled={working}>
            Disconnect
          </s-button>
        </s-stack>
      </s-section>
    );
  }

  return (
    <s-section heading="Instagram">
      <s-paragraph>Let MiraQ answer customers who message your store on Instagram.</s-paragraph>
      <s-paragraph color="subdued">
        You need an Instagram Business or Creator account linked to a Facebook Page you manage.
        Connecting opens a new tab.
      </s-paragraph>
      <s-button href={instagram.connect_url} target="_blank">
        Connect Instagram
      </s-button>
    </s-section>
  );
}
