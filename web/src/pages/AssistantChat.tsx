import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { chatApi } from "../chatApi";
import { t } from "../i18n";
import { tasksApi } from "../tasksApi";

/** The Assistant is a colleague in chat (REVIZE-FUNKCI 4.3): /assistant opens
 *  the DM with it, and a question asked elsewhere (?q=) is sent there first. */
export default function AssistantChat() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    (async () => {
      const assistant = (await tasksApi.actors()).find((a) => a.kind === "ai");
      if (!assistant) throw new Error(t("assist.none"));
      const q = params.get("q");
      if (q) {
        const ch = await chatApi.dm(assistant.id);
        await chatApi.send(ch.id, q);
      }
      navigate(`/chat?dm=${assistant.id}`, { replace: true });
    })().catch((e) => setError(e.message));
  }, []);
  return <p className="text-xs text-ink-2">{error ?? t("assist.opening")}</p>;
}
