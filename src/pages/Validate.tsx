import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { store } from "@/lib/mockStore";

const INDUSTRIES = ["Semiconductors", "Defense", "Pharma", "Quantum", "Aerospace"];

const Validate = () => {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [industry, setIndustry] = useState("Semiconductors");
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name || !company || !role) return;
    setSubmitting(true);
    const id = store.createProspect({ name, company, role, industry });
    // Kick off scoring; result page will subscribe and render progress.
    void store.runScoring(id);
    navigate(`/prospect/${id}`);
  };

  return (
    <PageShell>
      <div className="grid md:grid-cols-12 gap-10">
        <div className="md:col-span-5">
          <div className="label-eyebrow mb-3">Flow 01</div>
          <h1 className="text-4xl md:text-5xl font-light tracking-tight leading-[1.05] mb-6">
            Validate a specific person.
          </h1>
          <p className="text-sm text-muted-foreground max-w-md">
            We pull from LinkedIn, USPTO, GitHub, conference programs, hiring boards, and your
            mutual graph. Every signal is logged, weighted, and falsifiable.
          </p>
        </div>

        <form onSubmit={onSubmit} className="md:col-span-7 space-y-px">
          <Field label="Name" value={name} onChange={setName} placeholder="Jane Chen" />
          <Field label="Company" value={company} onChange={setCompany} placeholder="ASML" />
          <Field label="Role" value={role} onChange={setRole} placeholder="VP Lithography" />
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
            disabled={submitting || !name || !company || !role}
            className="w-full text-left border border-border p-5 mt-px hover:bg-secondary transition-colors disabled:opacity-40 disabled:cursor-not-allowed group"
          >
            <div className="flex items-center justify-between">
              <div>
                <div className="label-eyebrow mb-1.5">Submit</div>
                <div className="text-2xl font-light tracking-tight">Compute trust score →</div>
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
