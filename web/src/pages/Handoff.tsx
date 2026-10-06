import { useNavigate, useParams } from "react-router-dom";
import HandoffView from "../components/HandoffView";

/** /handoff/:id — the owner in an agent's live browser (desktop). */
export default function HandoffPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  return (
    <div className="mx-auto w-full max-w-5xl">
      <HandoffView id={Number(id)} onClose={() => navigate("/today")} />
    </div>
  );
}
