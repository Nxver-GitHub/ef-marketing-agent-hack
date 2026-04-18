import { useState, useRef, KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { db } from "@/lib/db";

const INDUSTRIES = ["Semiconductors", "Defense", "Pharma", "Quantum", "Aerospace"];

const Validate = () => {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [roles, setRoles] = useState<string[]>([]);
  const [roleInput, setRoleInput] = useState("");
  const [keywords, setKeywords] = useState<string[]>([]);
  const [keywordInput, setKeywordInput] = useState("");
  const [industry, setIndustry] = useState("Semiconductors");
  const [submitting, setSubmitting] = useState(false);
  const roleInputRef = useRef<HTMLInputElement>(null);
  const keywordInputRef = useRef<HTMLInputElement>(null);

  const makeTagHandlers = (
    items: string[],
    setItems: React.Dispatch<React.SetStateAction<string[]>>,
    inputValue: string,
    setInputValue: React.Dispatch<React.SetStateAction<string>>,
  ) => ({
    add: (value: string) => {
      const trimmed = value.trim();
      if (trimmed && !items.includes(trimmed)) {
        setItems((prev) => [...prev, trimmed]);
      }
      setInputValue("");
    },
    remove: (item: string) => setItems((prev) => prev.filter((i) => i !== item)),
    onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter" || e.key === "," || e.key === "Tab") {
        e.preventDefault();
        const trimmed = inputValue.trim();
        if (trimmed && !items.includes(trimmed)) {
          setItems((prev) => [...prev, trimmed]);
        }
        setInputValue("");
      } else if (e.key === "Backspace" && inputValue === "" && items.length > 0) {
        setItems((prev) => prev.slice(0, -1));
      }
    },
  });

  const roleHandlers = makeTagHandlers(roles, setRoles, roleInput, setRoleInput);
  const keywordHandlers = makeTagHandlers(keywords, setKeywords, keywordInput, setKeywordInput);

  const canSubmit = !submitting && company.trim() && roles.length > 0;

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    const finalRoles = roleInput.trim()
      ? [...new Set([...roles, roleInput.trim()])]
      : roles;
    const finalKeywords = keywordInput.trim()
      ? [...new Set([...keywords, keywordInput.trim()])]
      : keywords;
    if (finalRoles.length === 0) return;
    setSubmitting(true);
    const id = await db.createProspect({
      name: name.trim() || "Unknown",
      company,
      role: finalRoles[0],
      roles: finalRoles,
      keywords: finalKeywords,
      industry,
    });
    void db.runScoring(id);
    navigate(`/prospect/${id}`);
  };

  return (
    <PageShell>
      <div className="grid md:grid-cols-12 gap-10">
        <div className="md:col-span-5">
          <div className="label-eyebrow mb-3">Flow 01</div>
          <h1 className="text-4xl md:text-5xl font-light tracking-tight leading-[1.05] mb-6">
            Find the right person to contact.
          </h1>
          <p className="text-sm text-muted-foreground max-w-md">
            You know the role, the company, and the domain — but not the name. Describe what you
            know and we will surface the exact lead, scored and falsifiable.
          </p>
        </div>

        <form onSubmit={onSubmit} className="md:col-span-7 space-y-px">
          <Field
            label="Name"
            value={name}
            onChange={setName}
            placeholder="Jane Chen (optional)"
          />
          <Field label="Company" value={company} onChange={setCompany} placeholder="ASML" />

          {/* Multi-role tag input */}
          <TagInputField
            label="Role"
            items={roles}
            inputValue={roleInput}
            inputRef={roleInputRef}
            onInputChange={setRoleInput}
            onKeyDown={roleHandlers.onKeyDown}
            onBlur={() => { if (roleInput.trim()) roleHandlers.add(roleInput); }}
            onRemove={roleHandlers.remove}
            placeholder={roles.length === 0 ? "VP Lithography" : "Add another role…"}
            hint="Press Enter or comma to add. Multiple roles narrow the search."
          />

          {/* Keywords / descriptors */}
          <TagInputField
            label="Keywords"
            items={keywords}
            inputValue={keywordInput}
            inputRef={keywordInputRef}
            onInputChange={setKeywordInput}
            onKeyDown={keywordHandlers.onKeyDown}
            onBlur={() => { if (keywordInput.trim()) keywordHandlers.add(keywordInput); }}
            onRemove={keywordHandlers.remove}
            placeholder={keywords.length === 0 ? "chip manufacturing, NPI, wafer fab…" : "Add keyword…"}
            hint="Domain terms, team focus, or product areas that describe this person's work."
            tagStyle="secondary"
          />

          <div className="border border-border p-5">
            <div className="label-eyebrow mb-3">Industry</div>
            <div className="flex flex-wrap gap-2">
              {INDUSTRIES.map((i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => setIndustry(i)}
                  className={`text-xs px-3 py-1.5 border transition-colors ${
                    industry === i
                      ? "border-foreground bg-foreground text-background"
                      : "border-border text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {i}
                </button>
              ))}
            </div>
          </div>

          <button
            type="submit"
            disabled={!canSubmit}
            className="w-full text-left border border-border p-5 mt-px hover:bg-secondary transition-colors disabled:opacity-40 disabled:cursor-not-allowed group"
          >
            <div className="flex items-center justify-between">
              <div>
                <div className="label-eyebrow mb-1.5">Submit</div>
                <div className="text-2xl font-light tracking-tight">Find &amp; score the lead →</div>
              </div>
              <div
                className="w-10 h-10 rounded-full"
                style={{ background: "hsl(var(--accent))" }}
              />
            </div>
          </button>
        </form>
      </div>
    </PageShell>
  );
};

interface TagInputFieldProps {
  label: string;
  items: string[];
  inputValue: string;
  inputRef: React.RefObject<HTMLInputElement>;
  onInputChange: (v: string) => void;
  onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void;
  onBlur: () => void;
  onRemove: (item: string) => void;
  placeholder?: string;
  hint?: string;
  tagStyle?: "primary" | "secondary";
}

const TagInputField = ({
  label,
  items,
  inputValue,
  inputRef,
  onInputChange,
  onKeyDown,
  onBlur,
  onRemove,
  placeholder,
  hint,
  tagStyle = "primary",
}: TagInputFieldProps) => (
  <div
    className="border border-border p-5 cursor-text"
    onClick={() => inputRef.current?.focus()}
  >
    <div className="label-eyebrow mb-3">{label}</div>
    <div className="flex flex-wrap gap-2 mb-2">
      {items.map((item) => (
        <span
          key={item}
          className={`flex items-center gap-1.5 text-xs px-3 py-1.5 border transition-colors ${
            tagStyle === "primary"
              ? "border-foreground bg-foreground text-background"
              : "border-border text-foreground bg-secondary"
          }`}
        >
          {item}
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onRemove(item); }}
            className="opacity-60 hover:opacity-100 transition-opacity leading-none"
            aria-label={`Remove ${item}`}
          >
            ×
          </button>
        </span>
      ))}
      <input
        ref={inputRef}
        value={inputValue}
        onChange={(e) => onInputChange(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={onBlur}
        placeholder={placeholder}
        className="bg-transparent outline-none text-2xl font-light tracking-tight placeholder:text-muted-foreground/40 flex-1 min-w-[180px]"
      />
    </div>
    {items.length === 0 && hint && (
      <p className="text-xs text-muted-foreground/50 mt-1">{hint}</p>
    )}
  </div>
);

const Field = ({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) => (
  <label className="block border border-border p-5">
    <div className="label-eyebrow mb-2">{label}</div>
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full bg-transparent outline-none text-2xl font-light tracking-tight placeholder:text-muted-foreground/40"
    />
  </label>
);

export default Validate;
