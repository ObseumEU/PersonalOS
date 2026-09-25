export default function Logo({ size = 36 }: { size?: number }) {
  return (
    <div
      className="brand-gradient grid shrink-0 place-items-center rounded-xl text-white shadow-lg shadow-[color-mix(in_oklab,var(--color-brand)_40%,transparent)]"
      style={{ width: size, height: size }}
      aria-hidden
    >
      <svg viewBox="0 0 24 24" width={size * 0.55} height={size * 0.55} fill="none">
        <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="2.4" opacity="0.55" />
        <circle cx="12" cy="12" r="3.2" fill="currentColor" />
      </svg>
    </div>
  );
}
