import type { Support } from "../types";

interface Props {
  support: Support;
}

/** Support email, help center and privacy policy — whichever are configured. */
export default function HelpSection({ support }: Props) {
  if (!support.email && !support.url && !support.privacy_url) return null;

  return (
    <s-section heading="Help">
      <s-stack gap="small-200">
        {support.email && (
          <s-paragraph>
            Email <s-link href={`mailto:${support.email}`}>{support.email}</s-link>
          </s-paragraph>
        )}
        {support.url && (
          <s-link href={support.url} target="_blank">
            Help center
          </s-link>
        )}
        {support.privacy_url && (
          <s-link href={support.privacy_url} target="_blank">
            Privacy policy
          </s-link>
        )}
      </s-stack>
    </s-section>
  );
}
