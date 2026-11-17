import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  SetupInProgress,
  disconnectInstagram,
  saveInstagramSettings,
  fetchStatus,
  retrySetup,
} from "./api";
import type { Status } from "./types";
import SetupBanner from "./sections/SetupBanner";
import WidgetSection from "./sections/WidgetSection";
import ConversationsSection from "./sections/ConversationsSection";
import InstagramSection from "./sections/InstagramSection";
import HelpSection from "./sections/HelpSection";

/** Wait before asking again while the backend answers 202 (installing). */
const SETUP_RETRY_MS = 2000;
/** Refresh interval while the catalog is indexing. */
const INDEXING_POLL_MS = 10000;

type PageState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; status: Status };

const HEADING = "MiraQ Shopper Agent";

export default function App() {
  const [state, setState] = useState<PageState>({ kind: "loading" });
  const [working, setWorking] = useState(false);
  const inFlight = useRef(false);
  const timer = useRef<number | undefined>(undefined);

  const schedule = (fn: () => void, ms: number) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(fn, ms);
  };

  const showStatus = useCallback((status: Status) => {
    setState({ kind: "ok", status });
    if (status.setup === "indexing") {
      schedule(() => {
        void load.current();
      }, INDEXING_POLL_MS);
    }
  }, []);

  // Kept in a ref so timers and the focus listener always call the latest one.
  const load = useRef<() => Promise<void>>(async () => {});
  load.current = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      showStatus(await fetchStatus());
    } catch (err) {
      if (err instanceof SetupInProgress) {
        schedule(() => {
          void load.current();
        }, SETUP_RETRY_MS);
      } else {
        const message =
          err instanceof ApiError ? err.message : "Could not reach MiraQ.";
        // Once the page has loaded, a failed refresh keeps what's shown.
        setState((prev) => (prev.kind === "ok" ? prev : { kind: "error", message }));
      }
    } finally {
      inFlight.current = false;
    }
  };

  useEffect(() => {
    if (typeof shopify === "undefined") return;
    void load.current();
    const onFocus = () => {
      void load.current();
    };
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.clearTimeout(timer.current);
    };
  }, []);

  /** Stable callback for sections that need a status reload. */
  const refresh = useCallback(() => {
    void load.current();
  }, []);

  /** Run a button action; on success show its toast and the new status. */
  const act = async (action: () => Promise<Status>, done: string) => {
    setWorking(true);
    try {
      showStatus(await action());
      shopify.toast.show(done);
    } catch (err) {
      const message =
        err instanceof SetupInProgress
          ? "MiraQ is busy setting up. Try again in a moment."
          : "Something went wrong. Try again.";
      shopify.toast.show(message, { isError: true });
    } finally {
      setWorking(false);
    }
  };

  // Opened outside Shopify admin: App Bridge isn't there, so nothing works.
  if (typeof shopify === "undefined") {
    return (
      <s-page heading={HEADING}>
        <s-banner tone="warning" heading="Open MiraQ from your Shopify admin">
          <s-paragraph>
            This page only works inside Shopify admin → Apps → MiraQ Shopper Agent.
          </s-paragraph>
        </s-banner>
      </s-page>
    );
  }

  if (state.kind === "loading") {
    return (
      <s-page heading={HEADING}>
        <s-section>
          <s-stack direction="inline" gap="base" alignItems="center">
            <s-spinner accessibilityLabel="Loading" />
            <s-text>Setting things up…</s-text>
          </s-stack>
        </s-section>
      </s-page>
    );
  }

  if (state.kind === "error") {
    return (
      <s-page heading={HEADING}>
        <s-banner tone="critical" heading="MiraQ couldn't load">
          <s-paragraph>
            Reload the page. If it keeps happening, contact MiraQ support.
          </s-paragraph>
          <s-button
            slot="secondary-actions"
            onClick={() => {
              void load.current();
            }}
          >
            Try again
          </s-button>
        </s-banner>
      </s-page>
    );
  }

  const { status } = state;
  return (
    <s-page heading={HEADING}>
      <SetupBanner
        status={status}
        working={working}
        onRetry={() => {
          void act(retrySetup, "Setup restarted");
        }}
      />
      <WidgetSection embedUrl={status.embed_url} state={status.widget} shop={status.shop} />
      <ConversationsSection />
      {status.instagram.available && (
        <InstagramSection
          instagram={status.instagram}
          working={working}
          onDisconnect={() => {
            void act(disconnectInstagram, "Instagram disconnected");
          }}
          onSaveSettings={(changes) => {
            void act(() => saveInstagramSettings(changes), "Instagram settings saved");
          }}
          onStatus={showStatus}
          onRefresh={refresh}
        />
      )}
      <HelpSection support={status.support} />
    </s-page>
  );
}
