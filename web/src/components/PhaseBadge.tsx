export default function PhaseBadge({ phase, compact = false }: { phase: number; compact?: boolean }) {
  return (
    <span className="inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full border border-line bg-surface-2 px-2.5 py-0.5 text-[0.7rem] font-medium text-ink-3">
      <span className="brand-gradient h-1.5 w-1.5 rounded-full" />
      {compact ? `Phase ${phase}` : `Arrives in phase ${phase}`}
    </span>
  );
}
