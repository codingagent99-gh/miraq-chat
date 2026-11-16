import { useEffect, useRef, useState } from "react";
import { fetchConversations, fetchTranscript } from "../api";
import type { ConversationSummary, Transcript } from "../types";

// Chats shoppers had with MiraQ on the storefront, shown back to the merchant
// (App Store requirement 5.1.5). Read-only. The backend deletes chats after
// 30 days (purge_chat_history.py).

const MODAL_ID = "miraq-transcript";

const when = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const fmt = (iso: string) => when.format(new Date(iso));

/** Shopify customer ids are numeric; anything else isn't linkable. */
const customerLink = (id: string | null) =>
  id && /^\d+$/.test(id) ? `shopify://admin/customers/${id}` : null;

type ListState =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ok"; rows: ConversationSummary[]; hasMore: boolean };

type TranscriptState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ok"; transcript: Transcript };

export default function ConversationsSection() {
  const [list, setList] = useState<ListState>({ kind: "loading" });
  // Cursors of the pages before the current one; [] on the first page.
  const [previous, setPrevious] = useState<(string | undefined)[]>([]);
  const [current, setCurrent] = useState<string | undefined>(undefined);
  const [transcript, setTranscript] = useState<TranscriptState>({ kind: "idle" });
  const modal = useRef<HTMLElementTagNameMap["s-modal"]>(null);
  const opened = useRef<string | null>(null);

  const loadPage = async (before: string | undefined) => {
    setList({ kind: "loading" });
    try {
      const page = await fetchConversations(before);
      setList({ kind: "ok", rows: page.conversations, hasMore: page.has_more });
    } catch {
      setList({ kind: "error" });
    }
  };

  useEffect(() => {
    void loadPage(undefined);
  }, []);

  const older = () => {
    if (list.kind !== "ok" || !list.hasMore) return;
    const next = list.rows[list.rows.length - 1].cursor;
    setPrevious((p) => [...p, current]);
    setCurrent(next);
    void loadPage(next);
  };

  const newer = () => {
    const back = previous[previous.length - 1];
    setPrevious((p) => p.slice(0, -1));
    setCurrent(back);
    void loadPage(back);
  };

  const open = async (id: string) => {
    opened.current = id;
    setTranscript({ kind: "loading" });
    modal.current?.showOverlay();
    try {
      const t = await fetchTranscript(id);
      // Ignore a slow answer for a conversation that is no longer open.
      if (opened.current === id) setTranscript({ kind: "ok", transcript: t });
    } catch {
      if (opened.current === id) setTranscript({ kind: "error" });
    }
  };

  return (
    <s-section heading="Recent conversations">
      <s-paragraph color="subdued">
        Chats shoppers had with MiraQ on your store. Conversations are deleted after 30 days.
      </s-paragraph>

      {list.kind === "loading" && (
        <s-stack direction="inline" gap="base" alignItems="center">
          <s-spinner accessibilityLabel="Loading conversations" />
          <s-text>Loading conversations…</s-text>
        </s-stack>
      )}

      {list.kind === "error" && (
        <s-banner tone="critical" heading="Conversations couldn't load">
          <s-paragraph>Try again in a moment.</s-paragraph>
          <s-button
            slot="secondary-actions"
            onClick={() => {
              void loadPage(current);
            }}
          >
            Try again
          </s-button>
        </s-banner>
      )}

      {list.kind === "ok" && list.rows.length === 0 && previous.length === 0 && (
        <s-paragraph>
          No conversations yet. They show up here after shoppers chat with MiraQ.
        </s-paragraph>
      )}

      {list.kind === "ok" && list.rows.length > 0 && (
        <s-table>
          <s-table-header-row>
            <s-table-header listSlot="primary">First question</s-table-header>
            <s-table-header listSlot="secondary">Last active</s-table-header>
            <s-table-header>Customer</s-table-header>
            <s-table-header format="numeric">Messages</s-table-header>
            <s-table-header listSlot="inline">Transcript</s-table-header>
          </s-table-header-row>
          <s-table-body>
            {list.rows.map((c) => {
              const link = customerLink(c.customer_id);
              return (
                <s-table-row key={c.id}>
                  <s-table-cell>{c.first_question || "—"}</s-table-cell>
                  <s-table-cell>{fmt(c.last_active_at)}</s-table-cell>
                  <s-table-cell>
                    {link ? <s-link href={link}>View customer</s-link> : <s-text color="subdued">Guest</s-text>}
                  </s-table-cell>
                  <s-table-cell>{c.message_count}</s-table-cell>
                  <s-table-cell>
                    <s-button
                      variant="tertiary"
                      onClick={() => {
                        void open(c.id);
                      }}
                    >
                      View
                    </s-button>
                  </s-table-cell>
                </s-table-row>
              );
            })}
          </s-table-body>
        </s-table>
      )}

      {list.kind === "ok" && (previous.length > 0 || list.hasMore) && (
        <s-stack direction="inline" gap="small-200">
          <s-button onClick={newer} disabled={previous.length === 0}>
            Newer
          </s-button>
          <s-button onClick={older} disabled={!list.hasMore}>
            Older
          </s-button>
        </s-stack>
      )}

      <s-modal id={MODAL_ID} ref={modal} heading="Conversation">
        <TranscriptBody state={transcript} />
        <s-button
          slot="secondary-actions"
          onClick={() => {
            opened.current = null;
            modal.current?.hideOverlay();
          }}
        >
          Close
        </s-button>
      </s-modal>
    </s-section>
  );
}

function TranscriptBody({ state }: { state: TranscriptState }) {
  if (state.kind === "idle") return null;
  if (state.kind === "loading") {
    return <s-spinner accessibilityLabel="Loading conversation" />;
  }
  if (state.kind === "error") {
    return <s-paragraph>This conversation couldn't load. Close it and try again.</s-paragraph>;
  }

  const { transcript } = state;
  const link = customerLink(transcript.customer_id);
  return (
    <s-stack gap="base">
      <s-paragraph color="subdued">
        Started {fmt(transcript.started_at)} ·{" "}
        {link ? <s-link href={link}>View customer</s-link> : "Guest"}
      </s-paragraph>
      {transcript.messages.map((msg, i) => (
        <s-stack key={i} gap="small-100">
          <s-text type="strong">
            {msg.from === "shopper" ? "Shopper" : "MiraQ"} · {fmt(msg.sent_at)}
          </s-text>
          <s-paragraph>{msg.text || "—"}</s-paragraph>
          {msg.products_shown > 0 && (
            <s-text color="subdued">
              {msg.products_shown === 1 ? "1 product shown" : `${msg.products_shown} products shown`}
            </s-text>
          )}
        </s-stack>
      ))}
      {transcript.truncated && (
        <s-paragraph color="subdued">Only the first part of this long conversation is shown.</s-paragraph>
      )}
    </s-stack>
  );
}
