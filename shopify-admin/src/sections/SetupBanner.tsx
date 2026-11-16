import type { Status } from "../types";

interface Props {
  status: Status;
  working: boolean;
  onRetry: () => void;
}

/** Store setup state: live, indexing the catalog, or failed (with retry). */
export default function SetupBanner({ status, working, onRetry }: Props) {
  if (status.setup === "ready") {
    return (
      <s-banner tone="success" heading="MiraQ is live on your store">
        <s-paragraph>
          Your catalog is indexed. Shoppers can chat with MiraQ once the widget is turned on.
        </s-paragraph>
      </s-banner>
    );
  }

  if (status.setup === "indexing") {
    return (
      <s-banner tone="info" heading="Indexing your catalog">
        <s-stack direction="inline" gap="small-200" alignItems="center">
          <s-spinner size="base" accessibilityLabel="Indexing" />
          <s-paragraph>
            This takes a few minutes for a small store, longer for a large one. You can turn on
            the widget meanwhile — this page updates by itself.
          </s-paragraph>
        </s-stack>
      </s-banner>
    );
  }

  return (
    <s-banner tone="critical" heading="Setup didn't finish">
      <s-paragraph>
        MiraQ couldn't finish setting up your store. Try again; if it fails again, contact
        support.
      </s-paragraph>
      <s-button slot="secondary-actions" onClick={onRetry} disabled={working}>
        Retry setup
      </s-button>
    </s-banner>
  );
}
