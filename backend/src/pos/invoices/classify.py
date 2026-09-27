"""The deterministic pre-classifier: is a mail an invoice, which attachment is the document, business or personal.

The owner's rule (2026-09-27): only invoices that relate to Obseum, even remotely, go to the company's
accounting folder; anything clearly unrelated goes to "Osobní". When unsure: business, with the reason.

`classify` returns a suggestion with a confidence:
- `strong` business: the document names Obseum (name, IČO, DIČ, address, bank account) or the supplier or
  the items are IT (hosting, domains, cloud, SaaS, licences, AI/API, hardware, telecom, IT services, dev
  tools, IT courses, coworking/office);
- `strong` personal: personal marks (groceries, meals, household, clothing, personal services…) and no
  business sign at all;
- `unsure`: anything else (e.g. a personal-looking invoice that came to the work address). The suggestion is
  then business (the owner's bias) and the CFO decides.

Pure functions only: no I/O, so the rules are tested with Czech and English samples.
"""

import re
import unicodedata
from datetime import date

BUSINESS = "business"
PERSONAL = "personal"

# Obseum s.r.o.: name, IČO/DIČ, the registered addresses and the company bank account (from its invoices).
OBSEUM_MARKS = [
    (r"obseum", "jmenuje Obseum"),
    (r"\b07098308\b|\b0709\s?8308\b", "IČO Obseum 07098308"),
    (r"cz\s?07098308", "DIČ Obseum CZ07098308"),
    (r"rybn[aá]\s+716/24", "adresa Obseum (Rybná 716/24)"),
    (r"6357234309", "účet Obseum 6357234309/0800"),
]
WORK_ADDRESSES = ("david.rosko@obseum.cz",)
COMPANY_DOMAINS = ("obseum.cz", "obseum.cloud", "obseum.eu")

# IT, even remotely (suppliers and items), Czech and English. Case-insensitive, word-ish boundaries.
IT_TERMS = [
    "hosting", "webhosting", "server", "vps", "cloud", "doména", "domena", "domain", "dns", "ssl",
    "saas", "software", "licence", "license", "licen[cs]e", "subscription", "předplatné", "predplatne",
    "api", "tokens?", "gpu", "llm", "openai", "chatgpt", "anthropic", "claude", "elevenlabs", "eleven labs",
    "github", "gitlab", "gitkraken", "jetbrains", "copilot", "cursor", "lovable", "vercel", "netlify",
    "cloudflare", "hetzner", "digitalocean", "aws", "amazon web services", "azure", "microsoft", "office 365",
    "google workspace", "google cloud", "gcp", "firebase", "notion", "figma", "slack", "atlassian", "jira",
    "1password", "bitwarden", "paddle", "chainstack", "acrcloud", "taskcall", "webex", "zoom", "twilio",
    "sentry", "grafana", "datadog", "netdata", "docker", "npm", "pypi", "voyage", "replicate", "hugging ?face",
    "wedos", "forpsi", "active24", "websupport", "seyfor",
    "alza", "czc", "datart", "notebook", "laptop", "monitor", "klávesnice", "keyboard", "myš", "mouse",
    "ssd", "hardware", "elektronik", "electronics", "tiskárn", "printer", "raspberry", "arduino",
    "telekomunika", "telecom", "internet", "připojení", "pripojeni", "mobilní tarif", "o2", "vodafone",
    "t-mobile", "starlink",
    "it služby", "it sluzby", "vývoj", "vyvoj", "development", "programování", "programovani", "consulting",
    "konzultac", "školení", "skoleni", "course", "udemy", "coursera", "pluralsight", "o'reilly",
    "coworking", "kancelář", "kancelar", "office space", "účetní služby", "ucetni sluzby", "účetnictví",
    "ucetnictvi", "datová schránka", "datova schranka",
]
IT_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(IT_TERMS) + r")(?![a-z0-9])", re.I)
# Case-sensitive acronyms (lowercase "ai" is too common in Czech words).
IT_ACRONYM_RE = re.compile(r"(?<![A-Za-z])(AI|IT|SaaS|API)(?![A-Za-z])")

# Personal: clearly nothing to do with the company.
PERSONAL_WORDS = [  # whole words
    "potraviny", "grocery", "groceries", "lidl", "albert", "billa", "kaufland", "penny", "restaurace",
    "restaurant", "meal", "meals", "food", "wolt", "foodora", "boty", "shoes", "h&m", "zara", "dance", "fitness",
    "gym", "wellness", "pharmacy", "toys", "household", "garden", "mortgage", "vacation", "kino", "cinema",
    "netflix", "spotify", "hbo", "disney", "ikea",
]
PERSONAL_STEMS = [  # word beginnings
    "rohlík", "rohlik", "košík.cz", "kosik.cz", "stravován", "stravovan", "jídl", "jidl", "krabičk", "krabick",
    "dáme jídlo", "dame jidlo", "bolt food", "oblečen", "oblecen", "clothing", "fashion", "zalando", "tanec",
    "taneč", "tanec", "posilovn", "kadeřn", "kadern", "kosmetik", "manikúr", "manikur", "masáž", "masaz",
    "lékárn", "lekarn", "hračk", "hrack", "dětsk", "detsk", "veterin", "domácnost", "domacnost", "klimatizac",
    "air condition", "kuchyň", "kuchyn", "nábyt", "nabyt", "zahrad", "hypoték", "hypotek", "dovolen",
    "vstupenk", "air-condition",
]
PERSONAL_RE = re.compile(r"(?<![a-z0-9])(?:(" + "|".join(map(re.escape, PERSONAL_WORDS)) + r")(?![a-z0-9])|("
                         + "|".join(map(re.escape, PERSONAL_STEMS)) + r"))", re.I)
# The owner's private addresses: an invoice made out to them (and not to Obseum) is a private purchase.
PRIVATE_ADDRESS_RE = re.compile(r"velvarsk[aá]\s+1152|statenice|horom[eě][rř]ice", re.I)

INVOICE_WORDS = re.compile(
    r"faktur|invoice|da[nň]ov\w*\s+doklad|doklad\s+o\s+zaplacen|[uú][cč]tenk|receipt|billing|vy[uú][cč]tov[aá]n|"
    r"credit\s?note|dobropis|rechnung|z[aá]lohov|proforma|payment\s+confirmation|quittung", re.I)
NOT_INVOICE_SUBJECT = re.compile(r"nab[ií]dk|quote|offer|poptávk|poptavk|upomínk|upomink|reminder|"
                                 r"rekapitulace\s+objedn|shrnut[ií]\s+objedn|order\s+summary|"
                                 r"platebn[ií]\s+instrukc", re.I)
INVOICE_LABELS = re.compile(r"^(invoice|invoices|faktur[ay]?|receipts?|ucetni|účetní|doklady)$", re.I)
DOC_EXT = (".pdf", ".isdoc", ".isdocx", ".xml")
NOT_DOCUMENT = re.compile(r"^(vop|terms|obchodn[ií]|smime|logo|image\d*|outlook-|protokol|jak_pouzivat|how_to_use|"
                          r"platebn[ií]_instrukce|.*platebn[ií] instrukce)", re.I)
GENERIC_NAME = re.compile(r"^(invoice|faktura|doklad|receipt|document|dokument|attachment|priloha|příloha|scan|"
                          r"danovy[-_ ]doklad.*|da[nň]ov[yý][-_ ]doklad.*|uctenka|účtenka|\d{1,4}|file|download)$",
                          re.I)


def fold(text: str) -> str:
    """Lowercase without diacritics: "Září" → "zari"."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _addresses(value: str) -> list[str]:
    return [a.lower() for a in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", value or "")]


def _no_emails(text: str) -> str:
    return re.sub(r"\S+@\S+", " ", text or "")


# ------------------------------------------------------------------ is it an invoice

def is_document(filename: str, mime: str = "") -> bool:
    name = (filename or "").strip()
    low = name.lower()
    if not name or NOT_DOCUMENT.match(name):
        return False
    return low.endswith(DOC_EXT) or mime in ("application/pdf", "application/xml", "text/xml")


def pick_documents(attachments: list[dict]) -> list[dict]:
    """The invoice documents of a mail: PDF/ISDOC/XML attachments, without terms, logos and signatures;
    a Stripe-style mail with both Invoice-X.pdf and Receipt-Y.pdf keeps only the invoice (the accounting
    folder has the invoices, not the receipts)."""
    docs = [a for a in attachments if is_document(a.get("filename", ""), a.get("mime", ""))]
    has_invoice = any(re.match(r"invoice", a["filename"], re.I) for a in docs)
    if has_invoice:
        docs = [a for a in docs if not re.match(r"receipt", a["filename"], re.I)]
    return docs


def looks_like_invoice(mail: dict) -> tuple[bool, str]:
    """(is it, why). `mail`: subject, sender, body, labels (names), attachments [{filename, mime}]."""
    docs = pick_documents(mail.get("attachments") or [])
    if not docs:
        return False, "bez přílohy dokladu (PDF/ISDOC/XML)"
    subject = mail.get("subject") or ""
    names = " ".join(a["filename"] for a in docs)
    labelled = any(INVOICE_LABELS.match((lab or "").strip()) for lab in mail.get("labels") or [])
    worded = INVOICE_WORDS.search(subject) or INVOICE_WORDS.search(names) or INVOICE_WORDS.search(
        mail.get("sender") or "")
    if NOT_INVOICE_SUBJECT.search(subject) and not re.search(r"faktur|invoice|receipt|doklad", subject + names, re.I):
        return False, "nabídka, objednávka nebo upomínka, ne doklad"
    if worded:
        return True, f"klíčové slovo „{worded.group(0)}“"
    if labelled:
        return True, "štítek faktur v Gmailu"
    body = (mail.get("body") or "")[:4000]
    if INVOICE_WORDS.search(body) and not NOT_INVOICE_SUBJECT.search(subject):
        return True, "text e-mailu mluví o faktuře/dokladu"
    return False, "nic nenaznačuje fakturu"


# ------------------------------------------------------------------ business or personal

def classify(mail: dict, document_text: str = "") -> dict:
    """{suggestion, confidence (strong|unsure), signals [..], reason}. `mail`: subject, sender, to, cc,
    body, account; `document_text`: the attachment's text (PDF/ISDOC)."""
    doc = document_text or ""
    body = _no_emails(mail.get("body") or "")
    text = f"{mail.get('subject') or ''}\n{mail.get('sender') or ''}\n{doc}\n{body}"
    ftext = fold(text)
    strong, weak, personal = [], [], []

    for pattern, why in OBSEUM_MARKS:
        if re.search(pattern, fold(_no_emails(text))):
            strong.append(why)
    m = IT_RE.search(ftext) or IT_RE.search(text)
    if m:
        strong.append(f"IT dodavatel/položka („{m.group(0).strip()}“)")
    elif (m := IT_ACRONYM_RE.search(text)):
        strong.append(f"IT položka („{m.group(0)}“)")

    recipients = _addresses(f"{mail.get('to') or ''} {mail.get('cc') or ''} {mail.get('delivered_to') or ''}")
    if any(a in WORK_ADDRESSES for a in recipients):
        weak.append("přišlo na pracovní adresu david.rosko@obseum.cz")
    elif any(a.rpartition("@")[2] in COMPANY_DOMAINS for a in recipients):
        weak.append("přišlo na firemní doménu Obseum")
    elif (mail.get("account") or "").lower() in WORK_ADDRESSES:
        weak.append("přišlo do pracovní schránky")

    if (p := PERSONAL_RE.search(text) or PERSONAL_RE.search(ftext)):
        personal.append(f"osobní položka („{p.group(0).strip()}“)")
    if PRIVATE_ADDRESS_RE.search(doc) and not any("Obseum" in s or "IČO" in s or "DIČ" in s for s in strong):
        personal.append("fakturováno soukromé osobě na domácí adresu")

    obseum_named = any(s.startswith(("jmenuje Obseum", "IČO", "DIČ", "adresa Obseum", "účet Obseum")) for s in strong)
    if obseum_named or (strong and not personal):
        return _out(BUSINESS, "strong", strong + weak + personal)
    if personal and not strong and not weak:
        return _out(PERSONAL, "strong", personal)
    signals = strong + weak + personal
    if not signals:
        signals = ["žádný jasný znak"]
    return _out(BUSINESS, "unsure", signals + ["nejisté → firma (pravidlo majitele)"])


def _out(suggestion: str, confidence: str, signals: list[str]) -> dict:
    return {"suggestion": suggestion, "confidence": confidence, "signals": signals,
            "reason": ("firma" if suggestion == BUSINESS else "osobní") + ": " + "; ".join(signals)}


# ------------------------------------------------------------------ date, number, file name

MONTHS_EN = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
_DATE_LABEL = (r"(?:datum\s+vystaven[ií](?:\s+dokladu)?|vystaven[oa]?|datum\s+vystaveni|date\s+of\s+issue|"
               r"invoice\s+date|date\s+issued|issue\s+date|issued\s+on|date\s+paid|rechnungsdatum)")


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        out = date(y, m, d)
    except ValueError:
        return None
    return out if 2000 <= y <= 2100 else None


def issue_date(text: str) -> date | None:
    """The document's issue date, when it is labelled (Czech `Datum vystavení: 25.9. 2026`, English
    `Date of issue September 21, 2026`, ISO `2026-09-21`)."""
    t = " ".join((text or "").split())
    for m in re.finditer(_DATE_LABEL + r"\s*[:.]?\s*", t, re.I):
        rest = t[m.end():m.end() + 40]
        if (d := re.match(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", rest)) and (v := _valid(int(d[3]), int(d[2]), int(d[1]))):
            return v
        if (d := re.match(r"(\d{4})-(\d{2})-(\d{2})", rest)) and (v := _valid(int(d[1]), int(d[2]), int(d[3]))):
            return v
        if (d := re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", rest)) and d[1].lower() in MONTHS_EN:
            if v := _valid(int(d[3]), MONTHS_EN[d[1].lower()], int(d[2])):
                return v
        if (d := re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", rest)) and d[2].lower() in MONTHS_EN:
            if v := _valid(int(d[3]), MONTHS_EN[d[2].lower()], int(d[1])):
                return v
    # ISDOC: <IssueDate>2026-09-21</IssueDate>
    if (d := re.search(r"<IssueDate>(\d{4})-(\d{2})-(\d{2})</IssueDate>", text or "")):
        return _valid(int(d[1]), int(d[2]), int(d[3]))
    return None


def invoice_number(text: str) -> str | None:
    t = " ".join((text or "").split())
    for pattern in (r"(?:faktura|doklad|dobropis)[^0-9A-Za-z]{0,20}(?:č\.|číslo|c\.|cislo)\s*:?\s*([A-Z0-9][\w/-]{2,30})",
                    r"doklad\s+o\s+zaplacen[ií]\s+č\.?\s*([A-Z0-9][\w/-]{2,30})",
                    r"invoice\s+(?:number|no\.?|#)\s*:?\s*([A-Z0-9][\w/-]{2,30})",
                    r"<ID>([\w/-]{3,30})</ID>"):
        if (m := re.search(pattern, t, re.I)):
            return m.group(1).rstrip(".")
    return None


def safe_name(value: str, limit: int = 60) -> str:
    out = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", value or "")
    out = re.sub(r"\s+", " ", out).strip(" .")
    return out[:limit].strip()


def sender_name(sender: str) -> str:
    """"Pražská energetika, a. s." <fakturace@pre.cz> → Pražská energetika; noreply@x.cz → x."""
    m = re.match(r"\s*\"?([^\"<]+?)\"?\s*<([^>]+)>", sender or "")
    name = m.group(1) if m else ""
    if not name or "@" in name:
        addr = m.group(2) if m else (sender or "")
        name = addr.rpartition("@")[2].split(".")[0] if "@" in addr else addr
    name = re.sub(r",?\s*(a\.\s?s\.|s\.\s?r\.\s?o\.|spol\.|inc\.?|ltd\.?|limited|gmbh|llc|pte\.?)\s*$", "", name, flags=re.I)
    return safe_name(name, 40) or "Dodavatel"


def is_generic(filename: str) -> bool:
    stem = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", (filename or "").strip())
    return len(stem) < 4 or bool(GENERIC_NAME.match(stem))


def file_name(original: str, *, when: date, sender: str, number: str | None, fallback_id: str) -> str:
    """The original attachment name (the folder's convention: originals, the accountant numbers them
    later), or `YYYY-MM-DD Dodavatel Číslo.pdf` when the original says nothing (invoice.pdf)."""
    original = safe_name(original, 150)
    if original and not is_generic(original):
        return original
    ext = (re.search(r"\.[A-Za-z0-9]{1,6}$", original or "") or [".pdf"])[0].lower()
    return f"{when.isoformat()} {sender_name(sender)} {safe_name(number or fallback_id, 40)}{ext}"


def normalized_name(name: str) -> str:
    """For duplicate checks: without the accountant's prefix (2611-143-…), case and diacritics."""
    return fold(re.sub(r"^\d{4}-\d{2,4}-", "", (name or "").strip()))
