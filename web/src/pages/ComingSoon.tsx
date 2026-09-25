import { Check } from "lucide-react";
import PhaseBadge from "../components/PhaseBadge";
import type { Section } from "../sections";

export default function ComingSoon({ section }: { section: Section }) {
  const Icon = section.icon;
  return (
    <div className="space-y-8">
      <header className="rise flex items-center gap-4">
        <div className="brand-gradient grid h-12 w-12 place-items-center rounded-2xl text-white shadow-lg">
          <Icon size={24} />
        </div>
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">{section.label}</h1>
          <p className="text-ink-2">{section.blurb}</p>
        </div>
      </header>

      <div className="card rise relative overflow-hidden p-8 sm:p-12" style={{ animationDelay: "80ms" }}>
        <div className="brand-gradient pointer-events-none absolute -right-20 -bottom-20 h-64 w-64 rounded-full opacity-15 blur-3xl" />
        <div className="relative max-w-lg">
          <PhaseBadge phase={section.phase} />
          <h2 className="mt-4 text-xl font-semibold tracking-tight">What's coming</h2>
          <ul className="mt-4 space-y-3">
            {section.features.map((f) => (
              <li key={f} className="flex items-center gap-3 text-ink-2">
                <span className="grid h-6 w-6 place-items-center rounded-full bg-surface-2 text-brand">
                  <Check size={14} strokeWidth={2.6} />
                </span>
                {f}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
