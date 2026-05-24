"""
Agent SonarCloud — DevOps Agent complet adapté pour SonarCloud.
Différences avec version SonarQube :
  - URL fixe : https://sonarcloud.io
  - Paramètre 'organization' obligatoire sur tous les appels MCP
  - Scan et indexation gérés par l'agent (pas le workflow)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from typing import Any

from settings.config import (
    SONAR_TOKEN,
    SONARQUBE_URL,
    SONARQUBE_ORGANIZATION,
    MAX_ISSUES,
    MODEL_NAME,
    SAMBANOVA_API_KEY,
    TEMPERATURE,
    SONAR_SCANNER_CMD,
)

from langchain_sambanova import ChatSambaNova
from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, END

from models.state import SonarState


# ═════════════════════════════════════════════════════════════
# MCP CONFIG
# ═════════════════════════════════════════════════════════════

def get_mcp_config() -> dict:
    return {
        "sonarqube": {
            "transport": "stdio",
            "command":   "sonarqube-api-mcp",
            "args":      [],
            "env": {
                "SONAR_TOKEN":        SONAR_TOKEN,
                "SONAR_HOST_URL":     SONARQUBE_URL,
                "SONAR_ORGANIZATION": SONARQUBE_ORGANIZATION,
            },
        }
    }


# ═════════════════════════════════════════════════════════════
# LLM
# ═════════════════════════════════════════════════════════════

def create_llm() -> ChatSambaNova:
    return ChatSambaNova(
        model=MODEL_NAME,
        api_key=SAMBANOVA_API_KEY,
        temperature=TEMPERATURE,
        max_tokens=8192,
    )


# ═════════════════════════════════════════════════════════════
# TOOL REGISTRY
# ═════════════════════════════════════════════════════════════

def build_tool_registry(all_tools: list) -> dict[str, Any]:
    registry = {t.name: t for t in all_tools}
    print(f"✅ {len(registry)} tools SonarCloud disponibles :")
    for name in registry:
        print(f"   - {name}")
    return registry


# ═════════════════════════════════════════════════════════════
# HELPER
# ═════════════════════════════════════════════════════════════

def _parse(result: Any) -> Any:
    """
    Normalise la réponse MCP en dict ou list exploitable.

    Les tools MCP retournent parfois :
      - une str JSON  → on parse
      - une liste de ToolMessage/ContentBlock → on extrait le texte du 1er item
      - un dict       → on retourne tel quel
    """
    # ── Cas 1 : liste (ex. [TextContent(text='{"projectStatus":...}')])
    if isinstance(result, list):
        if not result:
            return {}
        first = result[0]
        # LangChain ToolMessage / ContentBlock avec attribut .text ou .content
        text = getattr(first, "text", None) or getattr(first, "content", None)
        if text and isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}
        # Si c'est directement un dict dans la liste
        if isinstance(first, dict):
            return first
        return {}

    # ── Cas 2 : chaîne JSON brute
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {}

    # ── Cas 3 : dict ou autre → tel quel
    return result if result else {}


def _with_org(params: dict) -> dict:
    """Ajoute organization à chaque appel SonarCloud (obligatoire)."""
    if SONARQUBE_ORGANIZATION:
        params["organization"] = SONARQUBE_ORGANIZATION
    return params


# ═════════════════════════════════════════════════════════════
# NODES
# ═════════════════════════════════════════════════════════════

async def _wait_indexation(project_key: str, max_wait: int = 90, interval: int = 5) -> bool:
    """
    Attend que SonarCloud ait indexé les résultats du scan.
    Interroge l'API REST jusqu'à ce que le status ne soit
    plus UNKNOWN, ou jusqu'au timeout.
    Returns True si indexation confirmée, False si timeout.
    """
    import urllib.request
    import base64

    url = (
        f"https://sonarcloud.io/api/qualitygates/project_status"
        f"?projectKey={project_key}"
    )
    credentials = base64.b64encode(f"{SONAR_TOKEN}:".encode()).decode()
    headers = {"Authorization": f"Basic {credentials}"}

    print(f"      ⏳ Attente indexation SonarCloud (max {max_wait}s)...")
    elapsed = 0
    while elapsed < max_wait:
        try:
            req  = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            status = data.get("projectStatus", {}).get("status", "UNKNOWN")
            print(f"         [{elapsed:>3}s] status = {status}")
            if status not in ("UNKNOWN", ""):
                print(f"      ✅ Indexation confirmée ({status}) en {elapsed}s")
                return True
        except Exception as e:
            print(f"         [{elapsed:>3}s] ⚠️  {e}")

        await asyncio.sleep(interval)
        elapsed += interval

    print(f"      ⚠️  Timeout {max_wait}s — on continue quand même")
    return False


async def node_scan_if_needed(state: SonarState, tools: dict) -> SonarState:
    """
    Lance toujours le scan sonar-scanner depuis l'agent,
    puis attend que SonarCloud indexe les résultats.
    Fonctionne en local ET en CI (le workflow ne fait plus le scan).
    """
    print("  🔎 [0/7] Scan SonarCloud + attente indexation...")

    project_path = state.get("project_path", ".")

    # ── Créer sonar-project.properties si absent ───────────
    props_file = os.path.join(project_path, "sonar-project.properties")
    if not os.path.exists(props_file):
        print(f"      → Création sonar-project.properties")
        props_content = (
            f"sonar.projectKey={state['project_key']}\n"
            f"sonar.organization={SONARQUBE_ORGANIZATION}\n"
            f"sonar.host.url={SONARQUBE_URL}\n"
            f"sonar.token={SONAR_TOKEN}\n"
            f"sonar.sources=.\n"
        )
        with open(props_file, "w", encoding="utf-8") as f:
            f.write(props_content)

    # ── Lancer sonar-scanner ───────────────────────────────
    print(f"      → Lancement sonar-scanner sur {project_path}")
    try:
        proc = await asyncio.create_subprocess_exec(
            SONAR_SCANNER_CMD,
            f"-Dsonar.projectKey={state['project_key']}",
            f"-Dsonar.organization={SONARQUBE_ORGANIZATION}",
            "-Dsonar.sources=.",
            f"-Dsonar.host.url={SONARQUBE_URL}",
            f"-Dsonar.token={SONAR_TOKEN}",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode(errors="replace")[:500]
            print(f"      → ❌ Scan échoué (code {proc.returncode}) : {error_msg}")
            return {**state, "scan_done": False, "error": error_msg}

        print(f"      → ✅ Scan terminé (exit 0)")

    except FileNotFoundError:
        msg = f"sonar-scanner introuvable : {SONAR_SCANNER_CMD}"
        print(f"      → ❌ {msg}")
        return {**state, "scan_done": False, "error": msg}

    # ── Attendre l'indexation SonarCloud ──────────────────
    await _wait_indexation(state["project_key"], max_wait=90, interval=5)

    return {**state, "scan_done": True}


async def node_check_quality_gate(state: SonarState, tools: dict) -> SonarState:
    print("  📊 [1/7] Quality Gate SonarCloud...")
    try:
        result = _parse(await tools["get_quality_gate_status"].ainvoke(
            _with_org({"projectKey": state["project_key"]})
        ))
        project_status = result.get("projectStatus", {})
        sonar_status   = project_status.get("status", "UNKNOWN")
        conditions     = project_status.get("conditions", [])

        print(f"      → SonarCloud gate : {sonar_status}")
        for c in conditions:
            print(f"         • {c.get('metricKey')} : {c.get('status')}")

        return {**state, "quality_gate": project_status, "gate_failed": False}
    except Exception as e:
        print(f"      ⚠️  Erreur : {e}")
        return {**state, "quality_gate": {}, "gate_failed": False}


async def node_fetch_issues(state: SonarState, tools: dict) -> SonarState:
    print("  🐛 [2/7] Récupération des issues...")
    try:
        result = _parse(await tools["search_sonar_issues"].ainvoke(
            _with_org({
                "projectKey": state["project_key"],
                "statuses":   ["OPEN", "CONFIRMED"],
                "ps":         MAX_ISSUES,
            })
        ))
        issues = result.get("issues", [])
        print(f"      → {len(issues)} issue(s)")
        return {**state, "issues": issues}
    except Exception as e:
        print(f"      ⚠️  Erreur : {e}")
        return {**state, "issues": []}


async def node_enrich_issues(state: SonarState, tools: dict) -> SonarState:
    print(f"  🔍 [3/7] Enrichissement de {len(state['issues'])} issue(s)...")
    enriched = []

    for i, issue in enumerate(state["issues"][:MAX_ISSUES]):
        print(f"      → issue {i+1}/{min(len(state['issues']), MAX_ISSUES)}")
        enriched_issue = dict(issue)

        try:
            context = _parse(await tools["get_sonar_issue_context"].ainvoke(
                _with_org({"issue_key": issue["key"]})
            ))
            enriched_issue["context"] = context
        except Exception:
            enriched_issue["context"] = {}

        try:
            rule = _parse(await tools["get_rule_details"].ainvoke(
                _with_org({"rule_key": issue.get("rule", "")})
            ))
            enriched_issue["rule_details"] = rule
        except Exception:
            enriched_issue["rule_details"] = {}

        enriched.append(enriched_issue)

    return {**state, "issue_contexts": enriched}


async def node_get_fix_plan(state: SonarState, tools: dict) -> SonarState:
    print("  🗺️  [4/7] Plan de correction...")
    try:
        result = _parse(await tools["get_sonar_fix_plan"].ainvoke(
            _with_org({"projectKey": state["project_key"]})
        ))
        return {**state, "fix_plan": result}
    except Exception as e:
        print(f"      ⚠️  Erreur : {e}")
        return {**state, "fix_plan": {}}


async def node_get_measures(state: SonarState, tools: dict) -> SonarState:
    print("  📈 [5/7] Métriques projet...")
    try:
        result = _parse(await tools["get_component_measures"].ainvoke(
            _with_org({
                "projectKey": state["project_key"],
                "metricKeys": [
                    "bugs", "reliability_rating", "reliability_remediation_effort",
                    "vulnerabilities", "security_rating", "security_remediation_effort",
                    "security_hotspots", "security_hotspots_reviewed", "security_review_rating",
                    "code_smells", "sqale_rating", "sqale_index", "sqale_debt_ratio",
                    "coverage", "line_coverage", "branch_coverage",
                    "lines_to_cover", "uncovered_lines", "conditions_to_cover", "uncovered_conditions",
                    "tests", "test_success_density",
                    "duplicated_lines_density", "duplicated_blocks",
                    "complexity", "cognitive_complexity",
                    "ncloc", "lines", "statements", "files", "functions", "classes",
                    "comment_lines", "comment_lines_density", "new_lines",
                    "new_bugs", "new_vulnerabilities", "new_code_smells", "new_coverage",
                ],
            })
        ))

        # Parser liste measures → dict plat
        raw_list = result.get("component", {}).get("measures", [])
        flat: dict = {}
        for entry in raw_list:
            metric = entry.get("metric", "")
            if "value" in entry:
                flat[metric] = entry["value"]
            elif "period" in entry:
                flat[metric] = entry["period"].get("value", "0")

        print(f"      → {len(flat)} métriques extraites")
        return {**state, "measures": flat}
    except Exception as e:
        print(f"      ⚠️  Erreur : {e}")
        return {**state, "measures": {}}


async def node_evaluate_quality(state: SonarState) -> SonarState:
    print("  🧠 [6/7] Évaluation qualité réelle...")
    measures = state.get("measures", {})

    def get_metric(key: str, default: float = 0.0) -> float:
        val = measures.get(key, default)
        if isinstance(val, dict):
            val = val.get("value", default)
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    bugs            = int(get_metric("bugs"))
    vulnerabilities = int(get_metric("vulnerabilities"))
    new_bugs        = int(get_metric("new_bugs"))
    new_vulns       = int(get_metric("new_vulnerabilities"))
    code_smells     = int(get_metric("code_smells"))
    coverage        = get_metric("coverage")
    sqale_index     = get_metric("sqale_index")
    duplication     = get_metric("duplicated_lines_density")

    reasons = []
    if bugs > 0:               reasons.append(f"{bugs} bug(s)")
    if vulnerabilities > 0:    reasons.append(f"{vulnerabilities} vulnérabilité(s)")
    if new_bugs > 0:           reasons.append(f"{new_bugs} nouveau(x) bug(s)")
    if new_vulns > 0:          reasons.append(f"{new_vulns} nouvelle(s) vulnérabilité(s)")
    if coverage < 20:          reasons.append(f"coverage {coverage:.1f}% < 20%")
    if code_smells > 10:       reasons.append(f"{code_smells} code smells > 10")
    if sqale_index > 60:       reasons.append(f"dette {sqale_index:.0f}min > 60min")
    if duplication > 10:       reasons.append(f"duplications {duplication:.1f}% > 10%")

    real_failed = len(reasons) > 0
    print(f"      → {'❌ FAIL' if real_failed else '✅ PASS'} — {', '.join(reasons) if reasons else 'OK'}")

    return {**state, "gate_failed": real_failed, "quality_reasons": reasons}


async def node_generate_report(state: SonarState, llm: ChatSambaNova) -> SonarState:
    print("  📝 [7/7] Génération du rapport LLM...")

    real_status     = "FAIL ❌" if state.get("gate_failed") else "PASS ✅"
    sonar_status    = state["quality_gate"].get("status", "UNKNOWN")
    quality_reasons = state.get("quality_reasons", [])
    measures        = state.get("measures", {})

    def m(key: str, default: str = "N/A") -> str:
        val = measures.get(key, default)
        if isinstance(val, dict):
            val = val.get("value", default)
        return str(val) if val not in (None, "", {}) else default

    ratings = {"1.0": "A ✅", "2.0": "B ✅", "3.0": "C ⚠️", "4.0": "D ❌", "5.0": "E ❌"}
    def rating(key: str) -> str:
        return ratings.get(m(key), m(key))

    severity_order = {"BLOCKER": 0, "CRITICAL": 1, "MAJOR": 2, "MINOR": 3, "INFO": 4}

    # Grouper issues identiques
    groups: dict = {}
    for iss in state["issue_contexts"]:
        key = (iss.get("severity", "INFO"), iss.get("message", "?").strip())
        if key not in groups:
            groups[key] = {"severity": key[0], "message": key[1], "locations": []}
        component = iss.get("component", "?").replace(state["project_key"] + ":", "")
        groups[key]["locations"].append(f"{component}:{iss.get('line', '?')}")

    sorted_groups = sorted(groups.values(), key=lambda x: severity_order.get(x["severity"], 4))
    grouped_issues = "\n".join([
        f"  {i+1:>3}. [{g['severity']:8}] {g['message']}\n         → {' | '.join(g['locations'])}"
        for i, g in enumerate(sorted_groups)
    ]) or "  Aucune issue détectée"

    total_issues  = len(state["issue_contexts"])
    unique_issues = len(sorted_groups)

    severity_counts = {}
    for iss in state["issue_contexts"]:
        sev = iss.get("severity", "UNKNOWN")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    severity_summary = " | ".join([
        f"{sev}: {count}"
        for sev, count in sorted(severity_counts.items(), key=lambda x: severity_order.get(x[0], 9))
    ])

    prompt = f"""Tu es un expert qualité logicielle. Génère un rapport COMPLET.

PROJET   : {state['project_key']}
DÉCISION : {real_status}
SONARCLOUD : {sonar_status}
RAISONS  : {', '.join(quality_reasons) if quality_reasons else 'Aucune'}

━━━ MÉTRIQUES ━━━
🔴 Bugs: {m('bugs')} | Rating: {rating('reliability_rating')}
🔐 Vulnérabilités: {m('vulnerabilities')} | Rating: {rating('security_rating')} | Effort: {m('security_remediation_effort')} min
🔎 Hotspots: {m('security_hotspots')} (révisés: {m('security_hotspots_reviewed')})
🧹 Code Smells: {m('code_smells')} | Dette: {m('sqale_index')} min | Ratio: {m('sqale_debt_ratio')}%
🧪 Coverage: {m('coverage')}% (lines: {m('line_coverage')}%, branches: {m('branch_coverage')}%)
📋 Duplications: {m('duplicated_lines_density')}% ({m('duplicated_blocks')} blocs)
🧠 Complexité: cyclo={m('complexity')} | cognitive={m('cognitive_complexity')}
📦 Taille: ncloc={m('ncloc')} | functions={m('functions')} | classes={m('classes')}
🆕 Nouveau: bugs={m('new_bugs')} | vulns={m('new_vulnerabilities')} | smells={m('new_code_smells')}

━━━ ISSUES ({total_issues} occurrences → {unique_issues} uniques — {severity_summary}) ━━━
{grouped_issues}

FORMAT :

## 🏥 Statut Global
[PASS ✅ / FAIL ❌] — 1 phrase

## 📊 Tableau de Bord Qualité
| Catégorie | Indicateur | Valeur | Niveau |
|-----------|------------|--------|--------|
(reproduire les métriques en tableau)

## 🐛 Issues ({total_issues} → {unique_issues} uniques — {severity_summary})
(reproduire toutes les issues groupées)

## 🔐 Analyse Sécurité
- État : [critique/élevé/modéré/faible]
- Recommandation : action urgente

## ✅ Actions Prioritaires
1. [URGENT] ...
2. [URGENT] ...
3. [IMPORTANT] ...
4. [IMPORTANT] ...
5. [NORMAL] ...

## 📈 Tendance
[S'améliore ↗ / Se dégrade ↘ / Stable →] — justification
"""

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    report   = response.content

    print("\n" + "═" * 60)
    print(report)
    print("═" * 60)

    return {**state, "report": report}


# ═════════════════════════════════════════════════════════════
# GRAPH
# ═════════════════════════════════════════════════════════════

def build_sonar_graph(tools: dict, llm: ChatSambaNova):
    graph = StateGraph(SonarState)

    async def _scan(s):     return await node_scan_if_needed(s, tools)
    async def _gate(s):     return await node_check_quality_gate(s, tools)
    async def _issues(s):   return await node_fetch_issues(s, tools)
    async def _enrich(s):   return await node_enrich_issues(s, tools)
    async def _fix(s):      return await node_get_fix_plan(s, tools)
    async def _measures(s): return await node_get_measures(s, tools)
    async def _eval(s):     return await node_evaluate_quality(s)
    async def _report(s):   return await node_generate_report(s, llm)

    graph.add_node("scan",     _scan)
    graph.add_node("gate",     _gate)
    graph.add_node("issues",   _issues)
    graph.add_node("enrich",   _enrich)
    graph.add_node("fix",      _fix)
    graph.add_node("measures", _measures)
    graph.add_node("eval",     _eval)
    graph.add_node("report",   _report)

    graph.set_entry_point("scan")
    graph.add_edge("scan",     "gate")
    graph.add_edge("gate",     "issues")
    graph.add_edge("issues",   "enrich")
    graph.add_edge("enrich",   "fix")
    graph.add_edge("fix",      "measures")
    graph.add_edge("measures", "eval")
    graph.add_edge("eval",     "report")
    graph.add_edge("report",   END)

    return graph.compile()


# ═════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ═════════════════════════════════════════════════════════════

async def run_sonar_analysis(project_key: str, project_path: str = ".") -> dict:
    t0 = time.time()

    print("=" * 60)
    print(f"🔍 SONARCLOUD AGENT")
    print(f"   Projet       : {project_key}")
    print(f"   Organization : {SONARQUBE_ORGANIZATION}")
    print(f"   URL          : {SONARQUBE_URL}")
    print("=" * 60)

    print("🔌 Connexion MCP SonarCloud...")
    client    = MultiServerMCPClient(get_mcp_config())
    all_tools = await client.get_tools()
    tools     = build_tool_registry(all_tools)

    llm   = create_llm()
    graph = build_sonar_graph(tools, llm)

    initial_state: SonarState = {
        "project_key":     project_key,
        "project_path":    os.path.abspath(project_path),
        "scan_done":       False,
        "quality_gate":    {},
        "issues":          [],
        "issue_contexts":  [],
        "measures":        {},
        "fix_plan":        {},
        "gate_failed":     False,
        "quality_reasons": [],
        "report":          "",
        "error":           "",
    }

    result = await graph.ainvoke(initial_state)
    print(f"\n⏱️  Terminé en {time.time() - t0:.1f}s")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("project_key")
    parser.add_argument("--path", default=".")
    args = parser.parse_args()

    try:
        asyncio.run(run_sonar_analysis(args.project_key, args.path))
    except Exception as e:
        print(f"\n❌ {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)