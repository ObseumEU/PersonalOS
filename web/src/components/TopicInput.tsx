import { useEffect, useState } from "react";
import { type Topic, topicsApi } from "../filesApi";
import { t } from "../i18n";

let cache: Promise<Topic[]> | null = null;

/** Known topics, fetched once per page load (refresh() after creating one). */
export function useTopics() {
  const [topics, setTopics] = useState<Topic[]>([]);
  useEffect(() => {
    cache ??= topicsApi.list().catch(() => []);
    cache.then(setTopics);
  }, []);
  return topics;
}

export function refreshTopics() {
  cache = null;
}

/** A free-text topic field that suggests the topics that exist. */
export default function TopicInput({
  id,
  value,
  onCommit,
  className = "",
}: {
  id: string;
  value: string | null;
  onCommit: (topic: string | null) => void;
  className?: string;
}) {
  const topics = useTopics();
  return (
    <>
      <input
        id={id}
        key={value ?? ""}
        list={`${id}-list`}
        defaultValue={value ?? ""}
        placeholder={t("topics.none")}
        onBlur={(e) => {
          const v = e.target.value.trim().replace(/^#/, "").toLowerCase() || null;
          if (v !== value) onCommit(v);
        }}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
        className={`h-8 min-w-0 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent ${className}`}
      />
      <datalist id={`${id}-list`}>
        {topics.map((x) => (
          <option key={x.slug} value={x.slug}>
            {x.name}
          </option>
        ))}
      </datalist>
    </>
  );
}
