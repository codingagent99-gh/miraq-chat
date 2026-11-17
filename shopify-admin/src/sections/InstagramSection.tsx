import { useEffect, useRef, useState } from "react";
import { chooseInstagramAccount, fetchInstagramAttempt } from "../api";
import type {
  InstagramAccountChoice,
  InstagramAttemptStatus,
  InstagramSettings,
  InstagramStatus,
  Status,
} from "../types";

/** How often the page asks for the pop-up's outcome while it waits. */
const POLL_MS = 2000;
/** Same window as the backend (_ATTEMPT_MAX_AGE): after this the attempt is expired. */
const GIVE_UP_MS = 10 * 60 * 1000;

type ResultStatus = Exclude<InstagramAttemptStatus, "waiting" | "choose" | "connected">;

type Phase =
  | { kind: "idle" }
  | { kind: "waiting"; id: string; url: string; blocked: boolean; startedAt: number }
  | { kind: "choose"; id: string; accounts: InstagramAccountChoice[]; selected: string }
  | { kind: "result"; status: ResultStatus; account: string | null };

interface Props {
  instagram: Extract<InstagramStatus, { available: true }>;
  working: boolean;
  onDisconnect: () => void;
  /** Save Instagram switches (AI replies, text only). */
  onSaveSettings: (changes: Partial<InstagramSettings>) => void;
  /** Show a fresh status the backend returned (after choosing an account). */
  onStatus: (status: Status) => void;
  /** Reload the status (after the pop-up connected an account). */
  onRefresh: () => void;
}

function newAttemptId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function Requirements() {
  return (
    <s-unordered-list>
      <s-list-item>An Instagram Business or Creator account</s-list-item>
      <s-list-item>Linked to a Facebook Page you manage</s-list-item>
      <s-list-item>
        "Allow access to messages" switched on in Instagram (Settings → Messages and story
        replies → Message controls → Connected tools)
      </s-list-item>
    </s-unordered-list>
  );
}

function ResultBanner({ status, account }: { status: ResultStatus; account: string | null }) {
  const name = account ?? "This account";
  switch (status) {
    case "cancelled":
      return (
        <s-banner tone="info" heading="Instagram wasn't connected">
          <s-paragraph>The Facebook login was cancelled. You can try again any time.</s-paragraph>
        </s-banner>
      );
    case "no_account":
      return (
        <s-banner tone="warning" heading="No Instagram account found">
          <s-paragraph>
            Check these, and that you selected the Page and the Instagram account when Facebook
            asked:
          </s-paragraph>
          <Requirements />
        </s-banner>
      );
    case "taken":
      return (
        <s-banner tone="critical" heading={`${name} is connected to another store`}>
          <s-paragraph>Disconnect it from that store's MiraQ first, then try again.</s-paragraph>
        </s-banner>
      );
    case "failed":
      return (
        <s-banner tone="critical" heading={`Couldn't connect ${name}`}>
          <s-paragraph>
            Facebook didn't allow MiraQ to receive this account's messages. Try again, and accept
            all the permissions Facebook asks for.
          </s-paragraph>
        </s-banner>
      );
    case "expired":
      return (
        <s-banner tone="warning" heading="Instagram wasn't connected">
          <s-paragraph>The connection wasn't finished in time. Please try again.</s-paragraph>
        </s-banner>
      );
    default:
      return (
        <s-banner tone="critical" heading="Instagram wasn't connected">
          <s-paragraph>Something went wrong with the Facebook login. Please try again.</s-paragraph>
        </s-banner>
      );
  }
}

/**
 * Connect / disconnect the store's Instagram account.
 *
 * Facebook Login can't run inside the admin, so only that step opens in a
 * pop-up; its outcome is read back from the backend and shown here, and with
 * several accounts the merchant chooses here (instagram_connect.py, EMBEDDED
 * ADMIN). The page asks the backend rather than listening to the pop-up:
 * Facebook's pages can cut the pop-up's link back to this window.
 */
export default function InstagramSection({
  instagram,
  working,
  onDisconnect,
  onSaveSettings,
  onStatus,
  onRefresh,
}: Props) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [choosing, setChoosing] = useState(false);
  const polling = useRef(false);

  const start = () => {
    const id = newAttemptId();
    const sep = instagram.connect_url.includes("?") ? "&" : "?";
    const url = `${instagram.connect_url}${sep}attempt=${encodeURIComponent(id)}`;
    // Opened in the click itself, so the browser treats it as user-initiated.
    const popup = window.open(url, "miraq-instagram", "popup=yes,width=600,height=760");
    setPhase({ kind: "waiting", id, url, blocked: popup === null, startedAt: Date.now() });
  };

  const waitingId = phase.kind === "waiting" ? phase.id : null;
  const startedAt = phase.kind === "waiting" ? phase.startedAt : 0;

  useEffect(() => {
    if (waitingId === null) return;
    let stopped = false;

    const check = async () => {
      if (polling.current || stopped) return;
      if (Date.now() - startedAt > GIVE_UP_MS) {
        setPhase({ kind: "result", status: "expired", account: null });
        return;
      }
      polling.current = true;
      try {
        const attempt = await fetchInstagramAttempt(waitingId);
        if (stopped) return;
        switch (attempt.status) {
          case "waiting":
            break;
          case "connected":
            setPhase({ kind: "idle" });
            shopify.toast.show(`Instagram connected: ${attempt.account ?? ""}`.trim());
            onRefresh();
            break;
          case "choose": {
            const accounts = attempt.accounts ?? [];
            if (accounts.length === 0) {
              setPhase({ kind: "result", status: "error", account: null });
            } else {
              setPhase({ kind: "choose", id: waitingId, accounts, selected: accounts[0].id });
            }
            break;
          }
          default:
            setPhase({ kind: "result", status: attempt.status, account: attempt.account ?? null });
        }
      } catch {
        // A failed check is retried on the next tick; the page keeps waiting.
      } finally {
        polling.current = false;
      }
    };

    const timer = window.setInterval(() => {
      void check();
    }, POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [waitingId, startedAt, onRefresh]);

  const choose = async () => {
    if (phase.kind !== "choose") return;
    setChoosing(true);
    try {
      const res = await chooseInstagramAccount(phase.id, phase.selected);
      onStatus(res.status);
      if (res.attempt.status === "connected") {
        setPhase({ kind: "idle" });
        shopify.toast.show(`Instagram connected: ${res.attempt.account ?? ""}`.trim());
      } else if (res.attempt.status === "waiting" || res.attempt.status === "choose") {
        setPhase({ kind: "result", status: "error", account: null });
      } else {
        setPhase({
          kind: "result",
          status: res.attempt.status,
          account: res.attempt.account ?? null,
        });
      }
    } catch {
      shopify.toast.show("Something went wrong. Try again.", { isError: true });
    } finally {
      setChoosing(false);
    }
  };

  const cancel = () => setPhase({ kind: "idle" });

  if (phase.kind === "waiting") {
    return (
      <s-section heading="Instagram">
        <s-stack direction="inline" gap="small-200" alignItems="center">
          <s-spinner accessibilityLabel="Waiting for Facebook" />
          <s-text>Finish connecting in the Facebook window.</s-text>
        </s-stack>
        {phase.blocked && (
          <s-paragraph>
            Your browser blocked the window.{" "}
            <s-link href={phase.url} target="_blank">
              Open the Facebook login
            </s-link>{" "}
            instead; this page updates when you're done.
          </s-paragraph>
        )}
        <s-button variant="tertiary" onClick={cancel}>
          Cancel
        </s-button>
      </s-section>
    );
  }

  if (phase.kind === "choose") {
    return (
      <s-section heading="Instagram">
        <s-choice-list
          label="Which Instagram account should MiraQ answer?"
          name="miraq-instagram-account"
          onChange={(event) => {
            const value = event.currentTarget.values[0];
            if (value) setPhase({ ...phase, selected: value });
          }}
        >
          {phase.accounts.map((a) => (
            <s-choice key={a.id} value={a.id} selected={a.id === phase.selected}>
              {a.label}
              {a.page && <s-text slot="details">Facebook Page: {a.page}</s-text>}
            </s-choice>
          ))}
        </s-choice-list>
        <s-stack direction="inline" gap="small-200">
          <s-button variant="primary" onClick={() => void choose()} loading={choosing}>
            Connect
          </s-button>
          <s-button variant="tertiary" onClick={cancel} disabled={choosing}>
            Cancel
          </s-button>
        </s-stack>
      </s-section>
    );
  }

  const banner =
    phase.kind === "result" ? <ResultBanner status={phase.status} account={phase.account} /> : null;

  if (instagram.connected) {
    const { settings } = instagram;
    const details = [
      instagram.page_name && `Facebook Page: ${instagram.page_name}`,
      instagram.connected_by && `Connected by ${instagram.connected_by}`,
    ].filter(Boolean);
    return (
      <s-section heading="Instagram">
        {banner}
        {instagram.needs_reconnect && (
          <s-banner tone="critical" heading="MiraQ can't reply on Instagram right now">
            <s-paragraph>
              Connect {instagram.account} again so MiraQ can answer its messages.
            </s-paragraph>
          </s-banner>
        )}
        <s-stack direction="inline" gap="small-200" alignItems="center">
          {instagram.needs_reconnect ? (
            <s-badge tone="critical">Needs reconnect</s-badge>
          ) : settings.ai_replies ? (
            <s-badge tone="success">Connected</s-badge>
          ) : (
            <s-badge tone="warning">Paused</s-badge>
          )}
          <s-text type="strong">{instagram.account}</s-text>
        </s-stack>
        {details.length > 0 && <s-paragraph color="subdued">{details.join(" · ")}</s-paragraph>}
        <s-paragraph>
          {settings.ai_replies
            ? "Customers who message this account get answers from MiraQ."
            : "MiraQ is paused on Instagram. Reply to customers in the Instagram app."}
        </s-paragraph>
        <s-switch
          label="Reply with AI on Instagram"
          details="Turn off to pause MiraQ on this account."
          checked={settings.ai_replies}
          disabled={working}
          onChange={(event) => onSaveSettings({ ai_replies: event.currentTarget.checked })}
        />
        <s-switch
          label="Reply only to messages with text"
          details="Emoji-only messages get no reply."
          checked={settings.text_only}
          disabled={working}
          onChange={(event) => onSaveSettings({ text_only: event.currentTarget.checked })}
        />
        <s-stack direction="inline" gap="small-200">
          <s-button
            variant={instagram.needs_reconnect ? "primary" : "secondary"}
            onClick={start}
            disabled={working}
          >
            {instagram.needs_reconnect ? "Reconnect" : "Connect a different account"}
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
      {banner}
      <s-paragraph>Let MiraQ answer customers who message your store on Instagram.</s-paragraph>
      <s-paragraph color="subdued">You need:</s-paragraph>
      <Requirements />
      <s-paragraph color="subdued">Facebook login opens in a small window.</s-paragraph>
      <s-button variant="primary" onClick={start} disabled={working}>
        {phase.kind === "result" ? "Try again" : "Connect Instagram"}
      </s-button>
    </s-section>
  );
}
