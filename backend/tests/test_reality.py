"""Grounded in reality (2026-10-06, the owner: "marketing sends 'try it here' and nothing can be tried"):
the "Co je živé" registry and its expiry, the anonymous URL probe, the claim gate on outbound and
owner-facing content, the sequencing hold of promotion, done-means-delivered, grounded blockers and the
improve signal. Nothing leaves: DNS, HTTP and the model are stand-ins."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from pos import (actors, agents, asks, comments, delivery, grounding, integrations, outbound, projects, reality,
                 tasks)
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate
from pos.improve import signals

PERMS = ["tasks:read", "tasks:claim", "tasks:write", "approvals:request", "messages:send"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POS_DELIVERY_GATE", "1")
    monkeypatch.setenv("POS_GROUNDING", "1")
    for v in ("POS_DISCORD_WEBHOOK_URL", "POS_OUTBOUND_DRY_RUN", "POS_GITHUB_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    c = connect(tmp_path / "r.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    integrations.install()
    owner = Ctx(actors.owner_id(c))
    ids = {}
    for name, role, extra in (("Growth", "growth_sales", []), ("Dev", "developer", []),
                              ("Lead", "product_lead", ["tasks:review"]), ("Writer", "developer", [])):
        a = agents.create_agent(c, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                                permissions=[*PERMS, *extra])["agent"]
        c.execute("UPDATE actors SET role = ?, team = 'kniha' WHERE id = ?", (role, a["id"]))
        ids[name] = a["id"]
    p = projects.create(c, owner, name="Kniha", channel=False)
    assert reality.seed_kniha(c) == len(reality.KNIHA)
    c.commit()
    yield {"conn": c, "owner": owner, "project": p, **{k: Ctx(v, via="mcp") for k, v in ids.items()}, "ids": ids}
    c.close()


class Web:
    """A stand-in internet: DNS answers and pages by URL prefix."""

    def __init__(self):
        self.dns = {}
        self.pages = {}
        self.calls = []

    def resolve(self, host):
        return self.dns.get(host, [])

    def fetch(self, url, timeout):
        self.calls.append(url)
        for prefix, f in self.pages.items():
            if url.startswith(prefix):
                if isinstance(f, Exception):
                    raise f
                return f
        raise ConnectionError("no route")


@pytest.fixture
def web(monkeypatch):
    w = Web()
    monkeypatch.setenv("POS_GROUNDING_NETWORK", "1")
    monkeypatch.setattr(reality, "RESOLVE", w.resolve)
    monkeypatch.setattr(reality, "FETCH", w.fetch)
    return w


def page(status=200, body="<html>Rodinné příběhy — rezervovat</html>", final="", headers=None):
    return reality.Fetched(status, final, headers or {}, body)


def cap(env, key):
    return reality.view(reality.get(env["conn"], env["project"]["id"], key))


# ------------------------------------------------------------------ the registry

def test_seed_is_idempotent_and_never_live(env):
    c = env["conn"]
    assert reality.seed_kniha(c) == 0
    caps = {x["key"]: x for x in reality.registry(c, [env["project"]["id"]])}
    assert caps["landing"]["status"] == "unverified" and caps["reservation"]["url"] == "https://rodinne-pribehy.obseum.cz"
    assert caps["test_app"]["status"] == "test_only" and caps["test_app"]["access"] == "password"
    assert {caps[k]["status"] for k in ("narrator", "chapters")} == {"mock"}
    assert {caps[k]["status"] for k in ("order", "photos", "payment", "email")} == {"missing"}


def test_an_agent_cannot_declare_live(env):
    with pytest.raises(tasks.Invalid, match="only by a verification"):
        reality.upsert(env["conn"], env["Dev"], "kniha", "order", {"status": "live"})
    out = reality.upsert(env["conn"], env["Dev"], "kniha", "chat", {"name": "Chat s podporou", "status": "mock"})
    assert out["status"] == "mock"


def test_verification_expires_after_72_hours(env, monkeypatch):
    c, pid = env["conn"], env["project"]["id"]
    old = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat(timespec="seconds")
    c.execute("UPDATE reality_capabilities SET status = 'live', verified_at = ? WHERE project_id = ? AND key = 'landing'",
              (old, pid))
    assert cap(env, "landing")["status"] == "unverified" and cap(env, "landing")["expired"]
    assert reality.expire(c) == ["landing"]
    assert reality.get(c, pid, "landing")["status"] == "unverified"
    assert c.execute("SELECT 1 FROM audit_log WHERE action = 'reality_expired'").fetchone()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")
    c.execute("UPDATE reality_capabilities SET status = 'live', verified_at = ? WHERE key = 'landing'", (fresh,))
    assert cap(env, "landing")["status"] == "live"


# ------------------------------------------------------------------ the probe

@pytest.mark.parametrize("fetched,kind", [
    (page(), "ok"),
    (page(401, headers={"www-authenticate": "Basic"}), "password"),
    (page(200, final="https://x.obseum.cz/login?next=/"), "login"),
    (page(200, body='<form><input type="password" name="p"></form>'), "password"),
    (page(404), "http"),
    (page(200, body="<html>Nic</html>"), "content"),
])
def test_probe_as_an_outsider(web, fetched, kind):
    web.dns["x.obseum.cz"] = ["93.184.216.34"]
    web.pages["https://x.obseum.cz"] = fetched
    assert reality.probe("https://x.obseum.cz/", expect_text="rezervovat").kind == kind


def test_probe_dns_private_and_unreachable(web):
    assert reality.probe("https://nikde.obseum.cz").kind == "dns"
    web.dns["lan.obseum.cz"] = ["192.168.1.5"]
    assert reality.probe("https://lan.obseum.cz").kind == "private"
    web.dns["down.obseum.cz"] = ["93.184.216.34"]
    pr = reality.probe("https://down.obseum.cz")
    assert pr.kind == "unreachable" and "neodpovídá" in pr.why


def test_probe_is_off_without_network(env, monkeypatch):
    monkeypatch.setenv("POS_GROUNDING_NETWORK", "0")
    assert reality.probe("https://rodinne-pribehy.obseum.cz").kind == "unverifiable"


def test_the_probe_job_makes_live_reverts_and_flags_an_open_test_instance(env, web):
    c = env["conn"]
    web.dns.update({"rodinne-pribehy.obseum.cz": ["93.184.216.34"], "kniha-test.obseum.cz": ["93.184.216.34"]})
    web.pages["https://rodinne-pribehy.obseum.cz"] = page()
    web.pages["https://kniha-test.obseum.cz"] = page(200, body="<form method=post action=/objednat>")
    out = reality.tick(c)
    assert out["live"] == 2
    assert cap(env, "landing")["status"] == "live" and cap(env, "reservation")["status"] == "live"
    assert cap(env, "test_app")["status"] == "test_only" and cap(env, "test_app")["last_check_ok"] is False
    assert c.execute("SELECT 1 FROM audit_log WHERE action = 'reality_exposed'").fetchone()
    web.pages["https://rodinne-pribehy.obseum.cz"] = page(502)
    reality.tick(c)
    assert cap(env, "landing")["status"] == "unverified"


def test_a_reviewer_verifies_evidence_and_the_url_must_open(env, web):
    c = env["conn"]
    reality.upsert(c, env["Dev"], "kniha", "order", {"access": "public", "url": "https://rodinne-pribehy.obseum.cz/objednat"})
    reality.submit_evidence(c, env["Dev"], "kniha", "order", "Otevřel jsem /objednat v anonymním okně a odeslal objednávku.")
    with pytest.raises(Forbidden):
        reality.verify(c, env["Dev"], "kniha", "order", True)  # no tasks:review, and it is its own evidence
    web.dns["rodinne-pribehy.obseum.cz"] = ["93.184.216.34"]
    web.pages["https://rodinne-pribehy.obseum.cz/objednat"] = page(404)
    with pytest.raises(tasks.Invalid, match="not accepted"):
        reality.verify(c, env["Lead"], "kniha", "order", True)
    web.pages["https://rodinne-pribehy.obseum.cz/objednat"] = page(200, body="<form>Objednat</form>")
    assert reality.verify(c, env["Lead"], "kniha", "order", True)["status"] == "live"


# ------------------------------------------------------------------ the claim gate

def test_a_link_to_the_test_instance_is_blocked_with_a_clear_reason(env):
    c = env["conn"]
    with pytest.raises(tasks.Invalid) as e:
        grounding.gate(c, env["Growth"], "outbound:email.send",
                       "Dobrý den, aplikaci si můžete vyzkoušet tady: https://kniha-test.obseum.cz/objednat")
    assert "kniha-test.obseum.cz vyžaduje heslo" in str(e.value)
    c.rollback()  # the caller's call is rolled back; the check and the signal were committed
    row = c.execute("SELECT * FROM grounding_checks ORDER BY id DESC").fetchone()
    assert row["verdict"] == "block" and row["surface"] == "outbound:email.send"
    assert c.execute("SELECT 1 FROM audit_log WHERE action = 'claim_ungrounded'").fetchone()


def test_request_outbound_never_drafts_ungrounded_content(env):
    c = env["conn"]
    with pytest.raises(tasks.Invalid, match="vyžaduje heslo"):
        outbound.request(c, env["Growth"], "discord.post", {"content": "Zkuste to: https://kniha-test.obseum.cz"})
    if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'outbound_sends'").fetchone():
        assert not c.execute("SELECT 1 FROM outbound_sends").fetchone()
    out = outbound.request(c, env["Growth"], "discord.post", {"content": "Píšeme rodinné příběhy, ozvěte se nám."})
    assert out["status"] == "not_configured"  # grounded: it went on to the (unconfigured) connector


def test_a_call_to_action_for_what_is_not_live_is_blocked(env):
    with pytest.raises(tasks.Invalid, match="Objednávkový formulář"):
        grounding.gate(env["conn"], env["Growth"], "outbound:email.send", "Knihu si objednejte ještě dnes!")
    # A truthful "not yet" passes; so does a pitch with no promise of use.
    grounding.gate(env["conn"], env["Growth"], "outbound:email.send", "Objednávky zatím nejsou spuštěné, připravujeme je.")
    grounding.gate(env["conn"], env["Growth"], "outbound:email.send", "Z vyprávění rodičů vznikne rodinná kniha.")


def test_a_live_public_link_passes_and_a_dead_own_link_does_not(env, web):
    c = env["conn"]
    web.dns["rodinne-pribehy.obseum.cz"] = ["93.184.216.34"]
    web.pages["https://rodinne-pribehy.obseum.cz"] = page()
    reality.tick(c)
    out = grounding.gate(c, env["Growth"], "outbound:email.send",
                         "Nezávazně si knihu zarezervujte na https://rodinne-pribehy.obseum.cz")
    assert out["ok"] and out["checked"]
    web.dns["novy.obseum.cz"] = ["93.184.216.34"]
    web.pages["https://novy.obseum.cz"] = page(403)
    with pytest.raises(tasks.Invalid, match="HTTP 403"):
        grounding.gate(c, env["Growth"], "outbound:email.send", "Mrkněte na https://novy.obseum.cz")
    web.dns["example.org"] = ["93.184.216.35"]
    web.pages["https://example.org"] = page(403)  # bot protection on a third-party site is not a broken link
    grounding.gate(c, env["Growth"], "outbound:email.send", "Zdroj: https://example.org/clanek")


def test_the_model_finds_claims_only_with_a_real_quote(env, monkeypatch):
    calls = []

    def model(conn, prompt):
        calls.append(prompt)
        return (json.dumps({"ungrounded": [
            {"quote": "hotovou knihu máte do týdne", "capability": "chapters", "why": "kapitoly jsou atrapa"},
            {"quote": "věta, která v textu není", "capability": None, "why": "x"}]}), None)

    monkeypatch.setattr(grounding, "MODEL_CALL", model)
    text = "Novinka: hotovou knihu máte do týdne, cena 2 990 Kč."
    with pytest.raises(tasks.Invalid) as e:
        grounding.gate(env["conn"], env["Growth"], "outbound:email.send", text)
    assert "hotovou knihu máte do týdne" in str(e.value) and "Kapitoly knihy" in str(e.value)
    assert "věta, která" not in str(e.value)
    with pytest.raises(tasks.Invalid):
        grounding.gate(env["conn"], env["Growth"], "outbound:email.send", text)
    assert len(calls) == 1  # the verdict is cached per content and registry


def test_no_model_call_without_claims_or_registry(env, monkeypatch):
    monkeypatch.setattr(grounding, "MODEL_CALL", lambda conn, prompt: pytest.fail("no model call expected"))
    grounding.gate(env["conn"], env["Growth"], "outbound:email.send", "Děkujeme za odpověď, ozveme se.")


def test_only_the_owner_overrides_and_then_the_same_content_passes(env):
    c = env["conn"]
    text = "Vyzkoušejte aplikaci: https://kniha-test.obseum.cz"
    with pytest.raises(tasks.Invalid):
        grounding.gate(c, env["Growth"], "outbound:email.send", text)
    check_id = c.execute("SELECT MAX(id) FROM grounding_checks").fetchone()[0]
    with pytest.raises(Forbidden):
        grounding.override(c, env["Lead"], check_id, "chci")
    grounding.override(c, env["owner"], check_id, "partner je tester, má heslo")
    assert grounding.gate(c, env["Growth"], "outbound:email.send", text)["override"]
    assert grounding.recent(c, [env["project"]["id"]])[0]["overridden"]


def test_people_are_not_gated(env):
    out = grounding.gate(env["conn"], env["owner"], "outbound:email.send", "https://kniha-test.obseum.cz objednejte")
    assert out == {"ok": True, "checked": False}


def test_an_ask_to_approve_ungrounded_content_never_reaches_the_owner(env):
    c = env["conn"]
    with pytest.raises(tasks.Invalid, match="vyžaduje heslo"):
        asks.ask(c, env["Growth"], title="Schval e-mail partnerům", why="Rozesílka partnerům",
                 details="Text: Vyzkoušejte si to na https://kniha-test.obseum.cz", kind="approval", blocking=False)
    assert not c.execute("SELECT 1 FROM tasks WHERE source = 'ask_owner'").fetchone()


# ------------------------------------------------------------------ sequencing

def _promo(env, title, notes=""):
    return tasks.create(env["conn"], env["Growth"], {"title": title, "notes": notes, "project": "kniha",
                                                      "assignee": {"type": "agent", "id": env["ids"]["Growth"]},
                                                      "definition_of_done": "E-mail je odeslaný."})


def test_promotion_waits_until_the_capability_is_live(env):
    c = env["conn"]
    t = _promo(env, "Kampaň: e-mail partnerům", "Pozvat partnery, ať si knihu objednají.")
    assert t["status"] == "waiting" and "Objednávkový formulář" in t["note"]
    with pytest.raises(tasks.Invalid, match="Čeká na živé funkce"):
        tasks.claim(c, env["Growth"], t["id"])
    with pytest.raises(tasks.Invalid, match="Čeká na živé funkce"):
        tasks.update(c, env["Growth"], t["id"], {"status": "next"})
    row = reality.get(c, env["project"]["id"], "order")
    reality._set_live(c, row, "review", "test", "ok")
    c.execute("UPDATE reality_capabilities SET access = 'public' WHERE id = ?", (row["id"],))
    assert reality.release_holds(c) == [t["id"]]
    assert tasks.get(c, env["owner"], t["id"])["status"] == "next"
    tasks.claim(c, env["Growth"], t["id"])


def test_promotion_of_what_is_live_and_other_work_are_not_held(env):
    assert _promo(env, "Kampaň: pozvánka k rozhovoru", "Pozvat rodiny k rozhovoru o knize.")["status"] == "next"
    t = tasks.create(env["conn"], env["Dev"], {"title": "Oprava objednávkového formuláře", "project": "kniha",
                                                "assignee": {"type": "agent", "id": env["ids"]["Dev"]}})
    assert t["status"] == "next"


def test_the_owner_releases_a_hold(env):
    t = _promo(env, "Newsletter: platba kartou")
    assert t["status"] == "waiting"
    tasks.update(env["conn"], env["owner"], t["id"], {"status": "next"})
    assert reality.held(env["conn"], t["id"]) is None


# ------------------------------------------------------------------ done means delivered

def _work(env, dod, **fields):
    c = env["conn"]
    t = tasks.create(c, env["Lead"], {"title": "Objednávka", "assignee": {"type": "agent", "id": env["ids"]["Writer"]},
                                      "definition_of_done": dod, **fields})
    tasks.claim(c, env["Writer"], t["id"])
    return t


def test_criteria_are_split_from_the_definition_of_done():
    assert delivery.criteria("- Objednávku lze založit formulářem\n- Platba projde\n") == [
        "Objednávku lze založit formulářem", "Platba projde"]
    assert delivery.criteria("Formulář běží na webu; e-mail přijde zákazníkovi") == [
        "Formulář běží na webu", "e-mail přijde zákazníkovi"]
    assert delivery.criteria("Hotovo.") == ["Hotovo."]


def test_a_hand_in_needs_evidence_per_criterion(env):
    c = env["conn"]
    t = _work(env, "1. Objednávka se ukládá do databáze\n2. Testy prochází")
    with pytest.raises(tasks.Invalid) as e:
        tasks.complete(c, env["Writer"], t["id"], "Hotovo, vše funguje.")
    assert "K1" in str(e.value) and "K2" in str(e.value)
    assert c.execute("SELECT 1 FROM audit_log WHERE action = 'delivery_incomplete'").fetchone()
    with pytest.raises(tasks.Invalid, match="K2"):
        tasks.complete(c, env["Writer"], t["id"], "K1: commit 3f2a9c1d")
    out = tasks.complete(c, env["Writer"], t["id"], "K1: commit 3f2a9c1d\nK2: pytest: 14 passed")
    assert out["status"] in ("review", "done")
    view = delivery.criteria_view(c, c.execute("SELECT * FROM tasks WHERE id = ?", (t["id"],)).fetchone())
    assert view[1]["evidence"] == "pytest: 14 passed"


def test_criteria_evidence_from_the_tool_and_failing_evidence(env):
    c = env["conn"]
    t = _work(env, "- Data jsou uložená\n- Report je hotový")
    with pytest.raises(tasks.Invalid, match="neprošel"):
        with delivery.criteria_input(["commit 3f2a9c1d", "soubor #999"]):
            tasks.complete(c, env["Writer"], t["id"], "Hotovo")
    with delivery.criteria_input({"1": "commit 3f2a9c1d", "2": "12 passed"}):
        assert tasks.complete(c, env["Writer"], t["id"], "Hotovo")["status"] in ("review", "done")


def test_a_live_criterion_needs_a_url_an_outsider_opens(env, web):
    c = env["conn"]
    t = _work(env, "Objednávkový formulář běží na webu")
    with pytest.raises(tasks.Invalid, match="URL"):
        tasks.complete(c, env["Writer"], t["id"], "Hotovo, commit 3f2a9c1d")
    web.dns["kniha-test.obseum.cz"] = ["93.184.216.34"]
    web.pages["https://kniha-test.obseum.cz"] = page(401, headers={"www-authenticate": "Basic"})
    with pytest.raises(tasks.Invalid, match="vyžaduje heslo"):
        tasks.complete(c, env["Writer"], t["id"], "Běží na https://kniha-test.obseum.cz/objednat")
    web.dns["rodinne-pribehy.obseum.cz"] = ["93.184.216.34"]
    web.pages["https://rodinne-pribehy.obseum.cz"] = page()
    assert tasks.complete(c, env["Writer"], t["id"], "Běží na https://rodinne-pribehy.obseum.cz/objednat")["status"] \
        in ("review", "done")


def test_a_parent_is_not_done_while_steps_are_open_or_dropped_silently(env):
    c = env["conn"]
    parent = _work(env, "Objednávka funguje")
    child = tasks.create(c, env["Lead"], {"title": "Nahrávání fotek", "parent_id": parent["id"],
                                          "assignee": {"type": "agent", "id": env["ids"]["Dev"]}})
    with pytest.raises(tasks.Invalid, match=f"{child['ref']} je next"):
        tasks.complete(c, env["Writer"], parent["id"], "Hotovo: commit 3f2a9c1d")
    tasks.archive(c, env["owner"], child["id"])
    with pytest.raises(tasks.Invalid, match="archivován bez důvodu"):
        tasks.complete(c, env["Writer"], parent["id"], "Hotovo: commit 3f2a9c1d")
    comments.add(c, env["Lead"], child["id"], "Fotky odkládáme do verze 2 (rozhodnutí v decision logu).")
    assert tasks.complete(c, env["Writer"], parent["id"], "Hotovo: commit 3f2a9c1d")["status"] in ("review", "done")


def test_an_agent_reviewer_cannot_accept_what_lost_its_evidence(env, web):
    c = env["conn"]
    t = _work(env, "Formulář běží na webu")
    web.dns["rodinne-pribehy.obseum.cz"] = ["93.184.216.34"]
    web.pages["https://rodinne-pribehy.obseum.cz"] = page()
    out = tasks.complete(c, env["Writer"], t["id"], "Běží na https://rodinne-pribehy.obseum.cz/objednat")
    if out["status"] != "review":
        pytest.skip("accepted by the policy at once")
    c.execute("UPDATE tasks SET reviewer_id = ? WHERE id = ?", (env["ids"]["Lead"], t["id"]))
    web.pages["https://rodinne-pribehy.obseum.cz"] = page(503)  # it went down since the hand-in
    with pytest.raises(tasks.Invalid, match="nelze přijmout"):
        tasks.review(c, env["Lead"], t["id"], True)
    web.pages["https://rodinne-pribehy.obseum.cz"] = page()
    assert tasks.review(c, env["Lead"], t["id"], True)["status"] == "done"


def test_people_and_exempt_tasks_are_not_gated(env):
    c = env["conn"]
    t = tasks.create(c, env["owner"], {"title": "Moje", "definition_of_done": "- a\n- b", "status": "next"})
    assert tasks.complete(c, env["owner"], t["id"], "ok")["status"] == "done"


# ------------------------------------------------------------------ grounded blockers

def test_a_false_dns_claim_to_the_owner_is_rejected(env, web):
    c = env["conn"]
    web.dns["rodinne-pribehy.obseum.cz"] = ["93.184.216.34"]
    with pytest.raises(tasks.Invalid, match="DNS pro rodinne-pribehy.obseum.cz existuje"):
        grounding.blocker_gate(c, env["Lead"], "chat_owner", "Nasazení blokuje chybějící DNS pro rodinne-pribehy.obseum.cz.")
    assert c.execute("SELECT 1 FROM audit_log WHERE action = 'blocker_unfounded'").fetchone()
    grounding.blocker_gate(c, env["Lead"], "chat_owner", "Nasazení blokuje chybějící DNS pro kniha.obseum.cz.")


def test_a_url_claimed_down_that_answers_is_rejected(env, web):
    web.dns["rodinne-pribehy.obseum.cz"] = ["93.184.216.34"]
    web.pages["https://rodinne-pribehy.obseum.cz"] = page()
    with pytest.raises(tasks.Invalid, match="odpovídá"):
        grounding.blocker_gate(env["conn"], env["Lead"], "message_owner", "Web https://rodinne-pribehy.obseum.cz nefunguje.")
    web.pages["https://rodinne-pribehy.obseum.cz"] = page(502)
    grounding.blocker_gate(env["conn"], env["Lead"], "message_owner", "Web https://rodinne-pribehy.obseum.cz nefunguje.")


def test_a_credential_claimed_missing_that_exists_is_rejected(env):
    c = env["conn"]
    from pos.credentials import store

    store.ensure_schema(c)
    c.execute("INSERT INTO credentials (name, op_ref, created_at, updated_at) VALUES ('github-deploy', 'op://x/y/z', "
              "'2026-10-01', '2026-10-01')")
    with pytest.raises(tasks.Invalid, match="github-deploy"):
        grounding.blocker_gate(c, env["Lead"], "ask_owner", "Chybí nám GitHub token pro deploy.")
    grounding.blocker_gate(c, env["Lead"], "ask_owner", "Chybí nám API klíč pro ElevenLabs.")


def test_a_blocker_needs_a_verifiable_reason(env):
    with pytest.raises(tasks.Invalid, match="ověřitelného důvodu"):
        asks.ask(env["conn"], env["Lead"], title="Rozhodni o spuštění", why="Nasazení webu je zablokované.",
                 blocking=False)
    out = asks.ask(env["conn"], env["Lead"], title="Rozhodni o spuštění",
                   why="Nasazení webu je zablokované: build selhal s chybou `exit code 1` (T-816).", blocking=False)
    assert out["ref"]
    # A decision the owner owes is not a technical claim to prove.
    assert asks.ask(env["conn"], env["Lead"], title="Podepiš DPA", why="Chybí tvůj podpis smlouvy.",
                    blocking=False, topic="dpa")["ref"]


# ------------------------------------------------------------------ the improve signal

def test_ungrounded_claims_are_an_improve_signal(env):
    c = env["conn"]
    with pytest.raises(tasks.Invalid):
        grounding.gate(c, env["Growth"], "outbound:email.send", "Vyzkoušejte aplikaci na https://kniha-test.obseum.cz")
    now = datetime.now(timezone.utc)
    items = signals.events(c, (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                           (now + timedelta(minutes=1)).isoformat(timespec="seconds"))
    keys = [k for k in items if k.startswith("claim_ungrounded:")]
    assert keys and items[keys[0]]["category"] == "grounding" and items[keys[0]]["count"] == 1
