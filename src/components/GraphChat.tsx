/**
 * Left-rail chat sidebar for /discover.
 *
 * Talks to FastAPI's `POST /chat` (via src/lib/agent.ts), renders the tool
 * loop as a live trace + clickable result chips, and Markdown-renders the
 * assistant's prose. Each chip selects its node in the canvas (and through
 * the existing AgentContext callbacks, opens the right-rail inspector).
 *
 * The chat is **atomic** for now — one round-trip per turn, no streaming.
 * Streaming SSE is the next pass once the rest of the demo loop is solid.
 */
import {
  KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import {
  runAgent,
  type AgentContext,
  type ChatMessage,
  type ToolCallTrace,
} from "@/lib/agent";
import { cn } from "@/lib/utils";

// ─── Suggested prompts (verified end-to-end against live data) ───────────────

const HINTS: { label: string; query: string }[] = [
  { label: "Top 5 NVIDIA leaders by score", query: "Top 5 NVIDIA leaders by overall score." },
  { label: "VPs who came from Intel", query: "Find VPs who used to work at Intel." },
  { label: "MIT engineers, ranked", query: "Show me engineers with MIT education, sorted by score." },
  { label: "Marc Hamilton's network", query: "Find Marc Hamilton and show his 1-hop network." },
  { label: "Highest-trust manufacturing leads", query: "Show me the highest-trust manufacturing leaders by score." },
];

// ─── Markdown styling — tight, fits the 360px rail ───────────────────────────

const MD_COMPONENTS: Components = {
  p: (p) => <p className="my-1 first:mt-0 last:mb-0" {...p} />,
  ul: (p) => <ul className="my-1 list-disc space-y-0.5 pl-4" {...p} />,
  ol: (p) => <ol className="my-1 list-decimal space-y-0.5 pl-4" {...p} />,
  li: (p) => <li className="leading-snug" {...p} />,
  strong: (p) => <strong className="font-semibold text-foreground" {...p} />,
  em: (p) => <em className="italic" {...p} />,
  code: (p) => (
    <code
      className="rounded bg-muted px-1 py-0.5 font-mono text-[11.5px]"
      {...p}
    />
  ),
  a: (p) => (
    <a
      target="_blank"
      rel="noreferrer"
      className="underline decoration-muted-foreground/40 underline-offset-2 hover:decoration-foreground"
      {...p}
    />
  ),
  hr: () => <hr className="my-2 border-border" />,
  h1: (p) => <h3 className="mt-2 text-[13px] font-semibold" {...p} />,
  h2: (p) => <h3 className="mt-2 text-[13px] font-semibold" {...p} />,
  h3: (p) => <h3 className="mt-2 text-[13px] font-semibold" {...p} />,
};

function MarkdownContent({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
      {text}
    </ReactMarkdown>
  );
}

// ─── Tool result -> chip list ────────────────────────────────────────────────

interface ChipItem {
  id: string;
  label: string;
  sub?: string;
  score?: number;
}

interface ToolPreview {
  summary: string;
  chips: ChipItem[];
  total: number;
}

function asNumber(v: unknown): number | undefined {
  if (typeof v === "number") return v;
  if (typeof v === "string") {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

// Mirrors agent.ts toGraphId — keep in sync. Server uses raw UUIDs / `co:` /
// `in:`; the graph builder uses `person:` / `company:` / `industry:`.
function toGraphId(id: string, kind?: string): string {
  if (!id) return id;
  if (kind === "person") return id.startsWith("person:") ? id : `person:${id}`;
  if (id.startsWith("co:")) return `company:${id.slice(3)}`;
  if (id.startsWith("in:")) return `industry:${id.slice(3)}`;
  return id;
}

function previewFromTrace(trace: ToolCallTrace): ToolPreview | null {
  const r = trace.result as Record<string, unknown> | undefined;
  if (!r || typeof r !== "object") return null;

  if (trace.name === "filter") {
    const list = (r.prospects ?? []) as Array<Record<string, unknown>>;
    const total = (r.count as number | undefined) ?? list.length;
    const chips: ChipItem[] = list.slice(0, 8).map((p) => ({
      id: toGraphId(String(p.id ?? ""), "person"),
      label: String(p.name ?? "?"),
      sub: typeof p.role === "string" ? p.role : undefined,
      score: asNumber(p.overall_score),
    }));
    return { summary: `${total} match${total === 1 ? "" : "es"}`, chips, total };
  }

  if (trace.name === "expand_node") {
    const list = (r.neighbors ?? []) as Array<Record<string, unknown>>;
    const total = list.length;
    const chips: ChipItem[] = list.slice(0, 8).map((n) => ({
      id: toGraphId(String(n.id ?? ""), typeof n.kind === "string" ? n.kind : "person"),
      label: String(n.name ?? "?"),
      sub: typeof n.via === "string" ? n.via : undefined,
      score: asNumber(n.overall_score),
    }));
    return { summary: `${total} neighbor${total === 1 ? "" : "s"}`, chips, total };
  }

  if (trace.name === "focus_node") {
    const list = (r.results ?? []) as Array<Record<string, unknown>>;
    const total = list.length;
    const chips: ChipItem[] = list.slice(0, 8).map((x) => ({
      id: toGraphId(String(x.id ?? ""), typeof x.kind === "string" ? x.kind : undefined),
      label: String(x.name ?? "?"),
      sub: typeof x.kind === "string" ? x.kind : undefined,
    }));
    return { summary: `${total} candidate${total === 1 ? "" : "s"}`, chips, total };
  }

  if (trace.name === "explain") {
    const node = r.node as Record<string, unknown> | null | undefined;
    if (!node) return null;
    const chip: ChipItem = {
      id: toGraphId(String(node.id ?? ""), typeof node.kind === "string" ? node.kind : "person"),
      label: String(node.name ?? "?"),
      sub: typeof node.role === "string" ? node.role : undefined,
      score: asNumber(node.overall_score),
    };
    return { summary: "evidence bundle", chips: [chip], total: 1 };
  }

  return null;
}

function ResultChips({
  preview,
  onPick,
}: {
  preview: ToolPreview;
  onPick: (id: string) => void;
}) {
  if (preview.chips.length === 0) return null;
  const hidden = preview.total - preview.chips.length;
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {preview.chips.map((c) => (
        <button
          key={c.id}
          type="button"
          onClick={() => c.id && onPick(c.id)}
          className="group inline-flex max-w-full items-center gap-1.5 truncate rounded-md border border-border bg-background px-2 py-1 text-left text-[11.5px] leading-tight transition-colors hover:bg-muted"
          title={c.sub ? `${c.label} — ${c.sub}` : c.label}
        >
          <span className="truncate font-medium">{c.label}</span>
          {typeof c.score === "number" && c.score > 0 && (
            <span className="shrink-0 rounded bg-muted px-1 font-mono text-[10px] text-muted-foreground group-hover:bg-background">
              {Math.round(c.score)}
            </span>
          )}
        </button>
      ))}
      {hidden > 0 && (
        <span className="self-center text-[11px] text-muted-foreground">
          +{hidden} more
        </span>
      )}
    </div>
  );
}

// ─── Tool trace block ────────────────────────────────────────────────────────

function argsLine(args: unknown): string {
  if (!args || typeof args !== "object") return "";
  const entries = Object.entries(args as Record<string, unknown>)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => {
      const sv = typeof v === "string" ? v : JSON.stringify(v);
      return `${k}: ${sv}`;
    });
  return entries.join(", ");
}

function ToolTraceBlock({
  trace,
  onPick,
}: {
  trace: ToolCallTrace;
  onPick: (id: string) => void;
}) {
  const preview = previewFromTrace(trace);
  return (
    <div className="rounded-md border border-border/60 bg-muted/30 px-2.5 py-1.5">
      <div className="flex items-baseline justify-between gap-2 font-mono text-[11px] leading-snug">
        <span className="truncate text-muted-foreground">
          <span className="text-foreground/80">{trace.name}</span>
          {argsLine(trace.args) && (
            <span className="text-muted-foreground/70"> · {argsLine(trace.args)}</span>
          )}
        </span>
        {preview && (
          <span className="shrink-0 text-[10px] uppercase tracking-wider text-muted-foreground">
            {preview.summary}
          </span>
        )}
      </div>
      {preview && <ResultChips preview={preview} onPick={onPick} />}
    </div>
  );
}

// ─── Message bubble ──────────────────────────────────────────────────────────

function MessageBubble({
  msg,
  onPick,
}: {
  msg: ChatMessage;
  onPick: (id: string) => void;
}) {
  const isUser = msg.role === "user";
  const traces = msg.role === "assistant" ? msg.toolCalls ?? [] : [];

  return (
    <div className={cn("flex flex-col gap-1.5", isUser ? "items-end" : "items-start")}>
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
        {isUser ? "YOU" : "CREDENCE"}
      </span>

      {traces.length > 0 && (
        <div className="flex w-full flex-col gap-1">
          {traces.map((t, i) => (
            <ToolTraceBlock key={i} trace={t} onPick={onPick} />
          ))}
        </div>
      )}

      {msg.content && (
        <div
          className={cn(
            "max-w-[88%] rounded-md px-3 py-2 text-[13px] leading-[1.55]",
            isUser
              ? "bg-foreground text-background"
              : "border border-border bg-muted text-foreground",
          )}
        >
          {isUser ? msg.content : <MarkdownContent text={msg.content} />}
        </div>
      )}
    </div>
  );
}

// ─── Component ───────────────────────────────────────────────────────────────

export interface GraphChatProps {
  ctx: AgentContext;
  initialMessages?: ChatMessage[];
}

export function GraphChat({ ctx, initialMessages }: GraphChatProps): JSX.Element {
  const [messages, setMessages] = useState<ChatMessage[]>(() => initialMessages ?? []);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);

  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

  const send = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      if (!text || pending) return;

      const next: ChatMessage[] = [...messages, { role: "user", content: text }];
      setMessages(next);
      setInput("");
      setPending(true);
      requestAnimationFrame(() => inputRef.current?.focus());

      try {
        const result = await runAgent(next, ctx);
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: result.finalText, toolCalls: result.toolCalls },
        ]);
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : String(err);
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `**Something went wrong**\n\n\`${errMsg}\``, toolCalls: [] },
        ]);
      } finally {
        setPending(false);
      }
    },
    [ctx, messages, pending],
  );

  const submit = useCallback(() => void send(input), [send, input]);

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handlePick = useCallback(
    (id: string) => {
      ctx.setSelectedId(id);
    },
    [ctx],
  );

  const showEmptyState = useMemo(() => messages.length === 0 && !pending, [messages.length, pending]);

  return (
    <aside className="flex h-full w-[360px] shrink-0 flex-col border-r border-border bg-background">
      {/* Header */}
      <div className="sticky top-0 z-10 border-b border-border bg-background px-5 pb-4 pt-5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
              ASK THE NETWORK
            </div>
            <h2 className="mt-1 text-[16px] font-semibold leading-tight text-foreground">
              Talk to the graph
            </h2>
            <p className="mt-1 text-[12px] leading-[1.45] text-muted-foreground">
              Describe the buyer you want — the network filters in real time.
            </p>
          </div>
          {messages.length > 0 && (
            <button
              type="button"
              onClick={() => {
                setMessages([]);
                // Clear the chat-driven filter / selection so the canvas
                // returns to its default top-by-score view.
                ctx.setVisibleNodeIds(null);
                ctx.setSelectedId(null);
              }}
              disabled={pending}
              className="shrink-0 rounded-md border border-border bg-background px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground transition-colors hover:bg-muted disabled:opacity-40"
              title="Clear conversation"
            >
              Reset
            </button>
          )}
        </div>
      </div>

      {/* Conversation */}
      <ScrollArea className="flex-1">
        <div className="flex flex-col gap-4 px-5 py-4">
          {showEmptyState && <EmptyState onPick={(q) => void send(q)} />}

          {messages.map((m, i) => (
            <MessageBubble key={i} msg={m} onPick={handlePick} />
          ))}

          {pending && (
            <div className="flex flex-col items-start gap-1">
              <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                CREDENCE
              </span>
              <div className="inline-flex items-center gap-2 text-[12px] italic text-muted-foreground">
                <ThinkingDots /> querying signals…
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </ScrollArea>

      <Separator />

      {/* Input bar */}
      <div className="sticky bottom-0 border-t border-border bg-background px-4 py-3">
        <div className="flex items-end gap-2">
          <Textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder="Ask anything about this network…"
            className="min-h-[38px] resize-none text-[13px]"
          />
          <Button
            type="button"
            size="sm"
            disabled={pending || input.trim().length === 0}
            onClick={submit}
          >
            Send
          </Button>
        </div>
      </div>
    </aside>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function EmptyState({ onPick }: { onPick: (query: string) => void }) {
  return (
    <div className="flex flex-col gap-3 py-2">
      <div className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
        Try one of these
      </div>
      <div className="flex flex-col gap-1.5">
        {HINTS.map((h) => (
          <button
            key={h.label}
            type="button"
            onClick={() => onPick(h.query)}
            className="group flex items-center justify-between gap-2 rounded-md border border-border/60 bg-muted/40 px-3 py-2 text-left text-[12.5px] leading-snug transition-colors hover:border-border hover:bg-muted"
          >
            <span className="truncate text-foreground">{h.label}</span>
            <span className="shrink-0 text-[11px] text-muted-foreground/60 transition-colors group-hover:text-muted-foreground">
              ↵
            </span>
          </button>
        ))}
      </div>
      <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
        Or ask anything — try filtering by company, role, school, past
        employer, or score.
      </p>
    </div>
  );
}

function ThinkingDots() {
  return (
    <span className="inline-flex items-center gap-0.5">
      <span className="h-1 w-1 animate-bounce rounded-full bg-current [animation-delay:-0.3s]" />
      <span className="h-1 w-1 animate-bounce rounded-full bg-current [animation-delay:-0.15s]" />
      <span className="h-1 w-1 animate-bounce rounded-full bg-current" />
    </span>
  );
}

export default GraphChat;
