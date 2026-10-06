import { useNavigate, useParams } from "react-router-dom";
import HandoffView from "../components/HandoffView";
import { t } from "../i18n/core";
import { TopBar } from "./ui";

/** /m/handoff/:id — the owner in an agent's live browser on the phone (opened from the push). */
export default function MobileHandoff() {
  const { id } = useParams();
  const navigate = useNavigate();
  return (
    <div>
      <TopBar title={t("ho.title")} back={() => navigate("/m/needs")} />
      <HandoffView id={Number(id)} mobile onClose={() => navigate("/m/needs")} />
    </div>
  );
}
