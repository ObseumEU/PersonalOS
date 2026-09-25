export default function Placeholder({ title, phase }: { title: string; phase: number }) {
  return (
    <section>
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 text-slate-500">Coming in phase {phase} of the roadmap.</p>
    </section>
  );
}
