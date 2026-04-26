import {
  KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
// agent.ts is being authored in parallel — types resolve once it lands.
import {
  runAgent,
  type ChatMessage,
  type AgentContext,
  type ToolCallTrace,
} from "@/lib/agent";

export interface GraphChatProps {
  ctx: AgentContext;
  initialMessages?: ChatMessage[];
}

const HINTS: string[] = [
  "Show only people with patents",
  "Defense primes hiring AI leads",
  "ICs promoted in last 12mo",
];

// Canned dialogue from the Figma so the dev surface looks alive when the
// caller doesn't seed `initialMessages`. Mirrors the Sarah Chen / VP Eng /
// fintech NYC scenario.
const SAMPLE_MESSAGES: ChatMessage[] = [
  {
    role: "user",
    content: "Find me a Sarah Chen — VP Eng at a Series B fintech in NYC.",
  },
  {
    role: "assistant",
    content:
      "Filtered to fintech · NYC · VP Eng+. Three plausible matches in the network — Lin Wei tops the list on scope evidence.",
    toolCalls: [
      { name: "filter", args: { industry: "fintech", city: "New York", role: "VP Engineering" } },
      { name: "focus_node", args: { query: "Lin Wei" } },
    ],
  },
];

function ToolTrace({ trace }: { trace: ToolCallTrace }) {
  const argStr = (() => {
    try {
      return JSON.stringify(trace.args);
    } catch {
      return "{…}";
    }
  })();
  return (
    <div className="font-mono text-[11px] leading-snug text-muted-foreground/70">
      &gt; {trace.name}({argStr})
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === "user";
  return (
    <div className={cn("flex flex-col gap-1", isUser ? "items-end" : "items-start")}>
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
        {isUser ? "YOU" : "CREDENCE"}
      </span>
      {msg.role === "assistant" && msg.toolCalls && msg.toolCalls.length > 0 && (
        <div className="mb-0.5 flex w-full flex-col gap-0.5">
          {msg.toolCalls.map((t, i) => (
            <ToolTrace key={i} trace={t} />
          ))}
        </div>
      )}
      <div
        className={cn(
          "max-w-[88%] rounded-md px-3 py-2 text-[13px] leading-[1.5]",
          isUser
            ? "bg-foreground text-background"
            : "border border-border bg-muted text-foreground",
        )}
      >
        {msg.content}
      </div>
    </div>
  );
}

export function GraphChat(props: GraphChatProps): JSX.Element {
  const { ctx, initialMessages } = props;

  const [messages, setMessages] = useState<ChatMessage[]>(
    () => initialMessages ?? SAMPLE_MESSAGES,
  );
  const [input, setInput] = useState<string>("");
  const [pending, setPending] = useState<boolean>(false);

  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to the latest message whenever the conversation grows or a
  // pending state flips on. `scrollIntoView` plays nicely inside the Radix
  // ScrollArea viewport.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

  const showHints = useMemo(() => messages.length <= 2, [messages.length]);

  const submit = useCallback(async () => {
    const text = input.trim();
    if (!text || pending) return;

    const userMsg: ChatMessage = { role: "user", content: text };
    const next = [...messages, userMsg];
    setMessages(next);
    setInput("");
    setPending(true);
    // Keep focus pinned on the composer so the operator can keep typing.
    requestAnimationFrame(() => inputRef.current?.focus());

    try {
      const result = await runAgent(next, ctx);
      const assistantMsg: ChatMessage = {
        role: "assistant",
        content: result.finalText,
        toolCalls: result.toolCalls,
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `Something went wrong: ${errMsg}`,
          toolCalls: [],
        },
      ]);
    } finally {
      setPending(false);
    }
  }, [ctx, input, messages, pending]);

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  };

  const onHintClick = (hint: string) => {
    setInput(hint);
    inputRef.current?.focus();
  };

  return (
    <aside className="flex h-full w-[360px] shrink-0 flex-col border-r border-border bg-background">
      {/* Header */}
      <div className="sticky top-0 z-10 border-b border-border bg-background px-5 pb-4 pt-5">
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

      {/* Conversation */}
      <ScrollArea className="flex-1">
        <div className="flex flex-col gap-4 px-5 py-4">
          {messages.map((m, i) => (
            <MessageBubble key={i} msg={m} />
          ))}
          {pending && (
            <div className="flex flex-col gap-1 items-start">
              <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                CREDENCE
              </span>
              <div className="text-[12px] italic text-muted-foreground">thinking…</div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </ScrollArea>

      {/* Hint chips */}
      {showHints && (
        <div className="border-t border-border bg-background px-5 py-3">
          <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            TRY
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {HINTS.map((h) => (
              <button
                key={h}
                type="button"
                onClick={() => onHintClick(h)}
                className="rounded-full border border-border bg-muted/60 px-2.5 py-1 text-[11px] text-foreground transition-colors hover:bg-muted"
              >
                {h}
              </button>
            ))}
          </div>
        </div>
      )}

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
            onClick={() => void submit()}
          >
            Send
          </Button>
        </div>
      </div>
    </aside>
  );
}

export default GraphChat;
