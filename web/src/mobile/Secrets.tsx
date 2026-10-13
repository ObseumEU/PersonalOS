import { useNavigate } from "react-router-dom";
import SecretsView from "../components/SecretsView";
import { t } from "../i18n/core";
import { TopBar } from "./ui";

/** /m/secrets — Tajemství on the phone (loads on first open). Owner only; never a value. */
export default function MobileSecrets() {
  const navigate = useNavigate();
  return (
    <div>
      <TopBar title={t("nav.secrets")} back={() => navigate("/m/more")} />
      <div className="p-3">
        <SecretsView mobile />
      </div>
    </div>
  );
}
