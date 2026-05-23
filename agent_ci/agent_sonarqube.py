"""
Agent Workflow SonarQube — DevOps Agent complet.

Chaîne complète :
  Code local → sonar-scanner → SonarQube → MCP → LLM → Rapport

Graph :
  [0] scan_if_needed
      ↓
  [1] check_quality_gate
      ↓
  [2] fetch_issues        ← toujours (PASS ou FAIL)
      ↓
  [3] enrich_issues
      ↓
  [4] get_fix_plan
      ↓
  [5] get_measures
      ↓
  [6] evaluate_quality    ← décision réelle basée sur métriques
      ↓
  [7] generate_report
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from typing import Any

# ─── Chemin complet vers sonar-scanner sur Windows ────────────
SONAR_SCANNER_CMD = (
    r"C:\Users\moham\Downloads\node-v23.7.0-win-x64"
    r"\node-v23.7.0-win-x64\sonar-scanner.cmd"
)

from langchain_sambanova import ChatSambaNova
from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, END

from models.state import SonarState
from settings.config import (
    SONAR_TOKEN,
    SONARQUBE_URL,
    MAX_ISSUES,
    MODEL_NAME,
    SAMBANOVA_API_KEY,
    TEMPERATURE,
    SONARQUBE_ORGANIZATION
)


# ═════════════════════════════════════════════════════════════
# MCP CONFIG
# ═════════════════════════════════════════════════════════════

# def get_mcp_config() -> dict:
#     return {
#         "sonarqube": {
#             "transport": "stdio",
#             "command":   "sonarqube-api-mcp",
#             "args":      [],
#             "env": {
#                 "SONAR_TOKEN":    SONARQUBE_TOKEN,
#                 "SONAR_HOST_URL": SONARQUBE_URL,
#             },
#         }
#     }

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
        max_tokens=8192
    )


# ═════════════════════════════════════════════════════════════
# TOOL REGISTRY
# ═════════════════════════════════════════════════════════════

def build_tool_registry(all_tools: list) -> dict[str, Any]:
    """Construit un dict name → tool pour appel direct."""
    registry = {t.name: t for t in all_tools}
    print(f"✅ {len(registry)} tools SonarQube disponibles :")
    for name in registry:
        print(f"   - {name}")
    return registry


# ═════════════════════════════════════════════════════════════
# HELPER — parse réponse MCP (dict ou string JSON)
# ═════════════════════════════════════════════════════════════

def _parse(result: Any) -> Any:
    """Parse la réponse MCP — peut être un dict ou une string JSON."""
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {}
    return result if result else {}


# ═════════════════════════════════════════════════════════════
# NODES
# ═════════════════════════════════════════════════════════════

async def node_scan_if_needed(
    state: SonarState,
    tools: dict,
) -> SonarState:
    """
    Node 0 — Scan toujours si --path fourni pour avoir les résultats à jour.
    - Projet absent + path fourni  → scan initial (création)
    - Projet présent + path fourni → re-scan (mise à jour)
    - Projet présent + pas de path → lecture résultats existants seulement
    - Projet absent + pas de path  → erreur
    """
    print("  🔎 [0/7] Vérification existence projet...")

    project_path = state.get("project_path", "")
    path_fourni  = bool(project_path and os.path.isdir(project_path))

    # ── Vérifier existence projet dans SonarQube ───────────
    exists = False
    try:
        result = _parse(await tools["get_quality_gate_status"].ainvoke(
            {"projectKey": state["project_key"],"organisation":SONARQUBE_ORGANIZATION}
        ))
        if result and "status" in result:
            exists = True
        elif isinstance(result, dict) and result.get("status") == 404:
            exists = False
        else:
            exists = True
    except Exception as e:
        error_str = str(e)
        if "404" in error_str or "not found" in error_str.lower():
            exists = False
        else:
            exists = True

    print(f"      → Projet SonarQube : {'✅ existe' if exists else '❌ absent'}")
    print(f"      → Path fourni      : {'✅ ' + project_path if path_fourni else '❌ non fourni'}")

    # ── Cas 1 : projet présent + pas de path → lecture seule
    if exists and not path_fourni:
        print(f"      → 📖 Lecture résultats existants (pas de re-scan sans --path)")
        return {**state, "scan_done": False}

    # ── Cas 2 : projet absent + pas de path → erreur
    if not exists and not path_fourni:
        msg = "Projet absent de SonarQube et --path non fourni — impossible d'analyser"
        print(f"      → ❌ {msg}")
        return {**state, "scan_done": False, "error": msg}

    # ── Cas 3 & 4 : path fourni → scan dans tous les cas ───
    if exists:
        print(f"      → 🔄 Re-scan pour mettre à jour les résultats...")
    else:
        print(f"      → 🆕 Scan initial — création du projet dans SonarQube...")

    # Créer sonar-project.properties si absent
    props_file = os.path.join(project_path, "sonar-project.properties")
    if not os.path.exists(props_file):
        print(f"      → 📝 Création de sonar-project.properties...")
        props_content = (
            f"sonar.projectKey={state['project_key']}\n"
            f"sonar.organization={SONARQUBE_ORGANIZATION}\n"
            f"sonar.projectName={state['project_key']}\n"
            f"sonar.sources=.\n"
            f"sonar.host.url={SONARQUBE_URL}\n"
            f"sonar.token={SONAR_TOKEN}\n"
        )
        with open(props_file, "w", encoding="utf-8") as f:
            f.write(props_content)
        print(f"      → ✅ Fichier créé : {props_file}")

    # Lancer sonar-scanner
    try:
        print(f"      → 🚀 sonar-scanner en cours (peut prendre 1-2 min)...")
        proc = await asyncio.create_subprocess_exec(
            SONAR_SCANNER_CMD,
            f"-Dsonar.projectKey={state['project_key']}",
            f"-Dsonar.organization={SONARQUBE_ORGANIZATION}",
            f"-Dsonar.projectName={state['project_key']}",
            "-Dsonar.sources=.",
            f"-Dsonar.host.url={SONARQUBE_URL}",
            f"-Dsonar.token={SONAR_TOKEN}",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            print(f"      → ✅ Scan terminé avec succès")
            print(f"      → ⏳ Attente indexation SonarQube (5s)...")
            await asyncio.sleep(5)
            return {**state, "scan_done": True}
        else:
            error_msg = stderr.decode(errors="replace")
            print(f"      → ❌ Scan échoué :\n{error_msg[:500]}")
            return {**state, "scan_done": False, "error": error_msg[:500]}

    except FileNotFoundError:
        msg = f"sonar-scanner introuvable : {SONAR_SCANNER_CMD}"
        print(f"      → ❌ {msg}")
        return {**state, "scan_done": False, "error": msg}

async def node_check_quality_gate(
    state: SonarState,
    tools: dict,
) -> SonarState:
    print("  📊 [1/7] Quality Gate SonarQube...")
    try:
        result = _parse(await tools["get_quality_gate_status"].ainvoke(
            {"projectKey": state["project_key"]}
        ))
        project_status = result.get("projectStatus", {})
        sonar_status   = project_status.get("status", "UNKNOWN")
        conditions     = project_status.get("conditions", [])
        
        print(f"      → SonarQube gate : {sonar_status}")
        if conditions:
            for c in conditions:
                print(f"         • {c.get('metricKey')} : {c.get('status')} "
                      f"(actual={c.get('actualValue')}, threshold={c.get('errorThreshold')})")

        return {
            **state,
            "quality_gate": project_status,  # ← stocke projectStatus directement
            "gate_failed":  False,
        }
    except Exception as e:
        print(f"      ⚠️  Erreur quality gate : {e}")
        return {**state, "quality_gate": {}, "gate_failed": False}
async def node_fetch_issues(
    state: SonarState,
    tools: dict,
) -> SonarState:
    """
    Node 2 — Récupère TOUTES les issues actives.
    Toujours exécuté — PASS ou FAIL — pour vue complète.
    """
    print("  🐛 [2/7] Récupération des issues...")
    try:
        result = _parse(await tools["search_sonar_issues"].ainvoke({
            "projectKey": state["project_key"],
            "statuses":   ["OPEN", "CONFIRMED"],
            "ps":         MAX_ISSUES,
        }))
        issues = result.get("issues", [])
        print(f"      → {len(issues)} issue(s) trouvée(s)")
        return {**state, "issues": issues}
    except Exception as e:
        print(f"      ⚠️  Erreur issues : {e}")
        return {**state, "issues": []}


async def node_enrich_issues(
    state: SonarState,
    tools: dict,
) -> SonarState:
    """
    Node 3 — Enrichit chaque issue avec contexte code + détails règle.
    """
    print(f"  🔍 [3/7] Enrichissement de {len(state['issues'])} issue(s)...")
    enriched = []

    for i, issue in enumerate(state["issues"][:MAX_ISSUES]):
        print(f"      → issue {i+1}/{min(len(state['issues']), MAX_ISSUES)}")
        enriched_issue = dict(issue)

        try:
            context = _parse(await tools["get_sonar_issue_context"].ainvoke(
                {"issue_key": issue["key"]}
            ))
            enriched_issue["context"] = context
        except Exception:
            enriched_issue["context"] = {}

        try:
            rule = _parse(await tools["get_rule_details"].ainvoke(
                {"rule_key": issue.get("rule", "")}
            ))
            enriched_issue["rule_details"] = rule
        except Exception:
            enriched_issue["rule_details"] = {}

        enriched.append(enriched_issue)

    return {**state, "issue_contexts": enriched}


async def node_get_fix_plan(
    state: SonarState,
    tools: dict,
) -> SonarState:
    """
    Node 4 — Génère un plan de correction priorisé.
    """
    print("  🗺️  [4/7] Plan de correction...")
    try:
        result = _parse(await tools["get_sonar_fix_plan"].ainvoke(
            {"projectKey": state["project_key"]}
        ))
        return {**state, "fix_plan": result}
    except Exception as e:
        print(f"      ⚠️  Erreur fix plan : {e}")
        return {**state, "fix_plan": {}}


async def node_get_measures(
    state: SonarState,
    tools: dict,
) -> SonarState:
    """
    Node 5 — Récupère les métriques complètes du projet.
    """
    print("  📈 [5/7] Métriques projet...")
    try:
        result = _parse(await tools["get_component_measures"].ainvoke({
            "projectKey": state["project_key"],
            "metricKeys": [
                # ── Fiabilité ──────────────────────────────
                "bugs",
                "reliability_rating",
                "reliability_remediation_effort",
                # ── Sécurité ───────────────────────────────
                "vulnerabilities",
                "security_rating",
                "security_remediation_effort",
                "security_hotspots",
                "security_hotspots_reviewed",
                "security_review_rating",
                # ── Maintenabilité ─────────────────────────
                "code_smells",
                "sqale_rating",
                "sqale_index",
                "sqale_debt_ratio",
                # ── Couverture ─────────────────────────────
                "coverage",
                "line_coverage",
                "branch_coverage",
                "lines_to_cover",
                "uncovered_lines",
                "conditions_to_cover",
                "uncovered_conditions",
                "tests",
                "test_success_density",
                # ── Duplications ───────────────────────────
                "duplicated_lines_density",
                "duplicated_blocks",
                # ── Complexité ─────────────────────────────
                "complexity",
                "cognitive_complexity",
                # ── Taille du code ─────────────────────────
                "ncloc",
                "lines",
                "statements",
                "files",
                "functions",
                "classes",
                "comment_lines",
                "comment_lines_density",
                "new_lines",
                # ── Nouveaux problèmes ─────────────────────
                "new_bugs",
                "new_vulnerabilities",
                "new_code_smells",
                "new_coverage",
            ],
        }))
 
        # ── Parser la liste measures → dict plat ───────────
        # Format SonarQube : {"component": {"measures": [{"metric": "bugs", "value": "1"}, ...]}}
        raw_list = result.get("component", {}).get("measures", [])
        flat: dict = {}
        for entry in raw_list:
            metric = entry.get("metric", "")
            if "value" in entry:
                flat[metric] = entry["value"]
            elif "period" in entry:
                flat[metric] = entry["period"].get("value", "0")
 
        print(f"      → {len(flat)} métriques extraites : {list(flat.keys())[:8]}...")
        return {**state, "measures": flat}
    except Exception as e:
        print(f"      ⚠️  Erreur measures : {e}")
        return {**state, "measures": {}}
 


async def node_evaluate_quality(state: SonarState) -> SonarState:
    """
    Node 6 — Évalue la VRAIE santé du projet basée sur les métriques réelles.

    Indépendant du Quality Gate SonarQube qui peut dire PASS
    même si le projet a des problèmes réels.

    Critères :
      - Bugs > 0
      - Vulnérabilités > 0
      - Nouveaux bugs > 0
      - Nouvelles vulnérabilités > 0
      - Coverage < 20%
      - Code Smells > 10
      - Dette technique > 60 min
      - Duplications > 10%
    """
    print("  🧠 [6/7] Évaluation qualité réelle...")

    measures = state.get("measures", {})

    def get_metric(key: str, default: float = 0.0) -> float:
        """Extrait une métrique — compatible format SonarQube dict ou valeur directe."""
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
    if bugs > 0:
        reasons.append(f"{bugs} bug(s)")
    if vulnerabilities > 0:
        reasons.append(f"{vulnerabilities} vulnérabilité(s)")
    if new_bugs > 0:
        reasons.append(f"{new_bugs} nouveau(x) bug(s)")
    if new_vulns > 0:
        reasons.append(f"{new_vulns} nouvelle(s) vulnérabilité(s)")
    if coverage < 20:
        reasons.append(f"coverage {coverage:.1f}% < 20%")
    if code_smells > 10:
        reasons.append(f"{code_smells} code smells > 10")
    if sqale_index > 60:
        reasons.append(f"dette technique {sqale_index:.0f}min > 60min")
    if duplication > 10:
        reasons.append(f"duplications {duplication:.1f}% > 10%")

    real_failed = len(reasons) > 0

    if real_failed:
        print(f"      → ❌ FAIL réel — raisons : {', '.join(reasons)}")
    else:
        print(f"      → ✅ PASS réel — aucun critère critique dépassé")

    return {**state, "gate_failed": real_failed, "quality_reasons": reasons}


async def node_generate_report(
    state: SonarState,
    llm: ChatSambaNova,
) -> SonarState:
    """
    Node 7 — SEUL node LLM.
    Génère le rapport final basé sur l'évaluation réelle (node 6).
    Affiche TOUTES les issues récupérées triées par sévérité.
    """
    print("  📝 [7/7] Génération du rapport LLM...")
 
    real_status     = "FAIL ❌" if state.get("gate_failed") else "PASS ✅"
    sonar_status    = state["quality_gate"].get("status", "UNKNOWN")
    quality_reasons = state.get("quality_reasons", [])
    measures        = state.get("measures", {})
 
    # ── Helper extraction métrique ──────────────────────────
    def m(key: str, default: str = "N/A") -> str:
        val = measures.get(key, default)
        if isinstance(val, dict):
            val = val.get("value", default)
        return str(val) if val not in (None, "", {}) else default
 
    # Ratings 1.0→E, 2.0→D, etc.
    ratings = {"1.0": "A ✅", "2.0": "B ✅", "3.0": "C ⚠️", "4.0": "D ❌", "5.0": "E ❌"}
    def rating(key: str) -> str:
        return ratings.get(m(key), m(key))
 
    # Coverage niveau
    def cov_level(val: str) -> str:
        try:
            v = float(val)
            return "✅" if v >= 80 else "⚠️" if v >= 20 else "❌"
        except Exception:
            return "N/A"
 
    # Complexité niveau
    def complex_level(val: str) -> str:
        try:
            v = float(val)
            return "✅" if v <= 10 else "⚠️" if v <= 20 else "❌"
        except Exception:
            return "N/A"
 
    metrics_summary = f"""
🔴 FIABILITÉ
   • Issues (Bugs)               : {m('bugs')}
   • Rating                      : {rating('reliability_rating')}
   • Remediation Effort          : {m('reliability_remediation_effort')} min
 
🔐 SÉCURITÉ
   • Vulnérabilités              : {m('vulnerabilities')}
   • Rating                      : {rating('security_rating')}
   • Remediation Effort          : {m('security_remediation_effort')} min
 
🔎 SECURITY REVIEW
   • Hotspots détectés           : {m('security_hotspots')}
   • Hotspots révisés            : {m('security_hotspots_reviewed')}
   • Rating review               : {rating('security_review_rating')}
 
🧹 MAINTENABILITÉ
   • Code Smells                 : {m('code_smells')}
   • Rating                      : {rating('sqale_rating')}
   • Dette technique             : {m('sqale_index')} min
   • Debt Ratio                  : {m('sqale_debt_ratio')}%
 
🧪 COUVERTURE
   • Coverage global             : {m('coverage')}% {cov_level(m('coverage'))}
   • Line Coverage               : {m('line_coverage')}%
   • Branch Coverage             : {m('branch_coverage')}%
   • Lines to Cover              : {m('lines_to_cover')}
   • Uncovered Lines             : {m('uncovered_lines')}
   • Conditions to Cover         : {m('conditions_to_cover')}
   • Uncovered Conditions        : {m('uncovered_conditions')}
   • Tests                       : {m('tests')}
   • Taux succès tests           : {m('test_success_density')}%
 
📋 DUPLICATIONS
   • Taux de duplication         : {m('duplicated_lines_density')}%
   • Blocs dupliqués             : {m('duplicated_blocks')}
 
🧠 COMPLEXITÉ
   • Complexité cyclomatique     : {m('complexity')} {complex_level(m('complexity'))}
   • Complexité cognitive        : {m('cognitive_complexity')} {complex_level(m('cognitive_complexity'))}
 
📦 TAILLE DU CODE
   • Lines of Code (ncloc)       : {m('ncloc')}
   • Lines                       : {m('lines')}
   • New Lines                   : {m('new_lines')}
   • Statements                  : {m('statements')}
   • Files                       : {m('files')}
   • Fonctions                   : {m('functions')}
   • Classes                     : {m('classes')}
   • Comment Lines               : {m('comment_lines')}
   • Comments (%)                : {m('comment_lines_density')}%
 
🆕 NOUVEAUX PROBLÈMES (depuis dernier scan)
   • Nouveaux bugs               : {m('new_bugs')}
   • Nouvelles vulnérabilités    : {m('new_vulnerabilities')}
   • Nouveaux code smells        : {m('new_code_smells')}
   • Nouveau coverage            : {m('new_coverage')}%
"""
 
    # ── Grouper les issues identiques (même message) ───────
    severity_order = {"BLOCKER": 0, "CRITICAL": 1, "MAJOR": 2, "MINOR": 3, "INFO": 4}
 
    # Grouper par (severity, message normalisé)
    groups: dict[tuple, dict] = {}
    for iss in state["issue_contexts"]:
        severity = iss.get("severity", "INFO")
        message  = iss.get("message", "?").strip()
        key      = (severity, message)
        if key not in groups:
            groups[key] = {
                "severity": severity,
                "message":  message,
                "locations": [],
            }
        component = iss.get("component", "?").replace(
            state["project_key"] + ":", ""
        )
        line = iss.get("line", "?")
        groups[key]["locations"].append(f"{component}:{line}")
 
    # Trier par sévérité
    sorted_groups = sorted(
        groups.values(),
        key=lambda x: severity_order.get(x["severity"], 4)
    )
 
    # Formater les issues groupées
    grouped_issues = "\n".join([
        f"  {i+1:>3}. [{g['severity']:8}] {g['message']}\n"
        f"         → {' | '.join(g['locations'])}"
        for i, g in enumerate(sorted_groups)
    ]) or "  Aucune issue détectée"
 
    total_issues   = len(state["issue_contexts"])
    unique_issues  = len(sorted_groups)
 
    # Résumé par sévérité
    severity_counts = {}
    for iss in state["issue_contexts"]:
        sev = iss.get("severity", "UNKNOWN")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    severity_summary = " | ".join([
        f"{sev}: {count}"
        for sev, count in sorted(
            severity_counts.items(),
            key=lambda x: severity_order.get(x[0], 9)
        )
    ])
 
    scan_info = (
        "⚠️  Projet scanné automatiquement lors de cette analyse."
        if state.get("scan_done")
        else "Projet déjà présent dans SonarQube."
    )
 
    prompt = f"""Tu es un expert qualité logicielle. Génère un rapport COMPLET avec TOUTES les sections ci-dessous.
IMPORTANT : toutes les sections sont obligatoires, ne pas en sauter aucune.
 
PROJET      : {state['project_key']}
SCAN INFO   : {scan_info}
DÉCISION    : {real_status}
SONARQUBE   : {sonar_status} (notre évaluation corrige le biais "New Code Only")
RAISONS     : {', '.join(quality_reasons) if quality_reasons else 'Aucune'}
 
━━━ MÉTRIQUES ━━━
{metrics_summary}
 
━━━ ISSUES GROUPÉES ({total_issues} occurrences → {unique_issues} issues uniques — {severity_summary}) ━━━
(même issue regroupée avec tous ses fichiers/lignes)
{grouped_issues}
 
━━━ FORMAT OBLIGATOIRE — REPRODUIRE TOUTES CES SECTIONS ━━━
 
## 🏥 Statut Global
[PASS ✅ / FAIL ❌] — 1 phrase (baser sur DÉCISION)
 
## 📊 Tableau de Bord Qualité
| Catégorie          | Indicateur                        | Valeur              | Niveau       |
|--------------------|-----------------------------------|---------------------|--------------|
| 🔴 Fiabilité       | Issues (Bugs)                     | X                   | A/B/C/D/E    |
|                    | Remediation Effort                | X min               |              |
|                    | Rating                            | X                   | A/B/C/D/E    |
| 🔐 Sécurité        | Vulnérabilités                    | X                   | A/B/C/D/E    |
|                    | Remediation Effort                | X min               |              |
| 🔎 Security Review | Hotspots détectés / révisés       | X / X               |              |
|                    | Rating review                     | X                   | A/B/C/D/E    |
| 🧹 Mainten.        | Code Smells / Dette tech.         | X / X min           | A/B/C/D/E    |
|                    | Debt Ratio                        | X%                  |              |
| 🧪 Couverture      | Coverage global / Lignes / Branch | X% / X% / X%        | ✅ / ⚠️ / ❌ |
|                    | Lines to Cover / Uncovered Lines  | X / X               |              |
|                    | Conditions to Cover / Uncovered   | X / X               |              |
| 📋 Duplications    | Taux / Blocs dupliqués            | X% / X              | ✅ / ⚠️ / ❌ |
| 🧠 Complexité      | Cyclomatique / Cognitive          | X / X               | ✅ / ⚠️ / ❌ |
| 📦 Taille          | Lines of Code / Lines / New Lines | X / X / X           | —            |
|                    | Statements / Files                | X / X               | —            |
|                    | Comment Lines / Comments (%)      | X / X%              | —            |
| 🆕 Nouveautés      | Bugs / Vulnés / Smells nouveaux   | X / X / X           | ✅ / ⚠️ / ❌ |
 
## 🐛 Issues ({total_issues} occurrences → {unique_issues} uniques — {severity_summary})
(reproduire TOUTES les issues groupées du prompt — même issue = 1 ligne avec tous les fichiers)
N. [SEVERITY] description
   → fichier1:ligne1 | fichier2:ligne2 | ...
 
## 🔐 Analyse Sécurité
- État global : [critique / élevé / modéré / faible]
- Vulnérabilités : X — risque principal en 1 phrase
- Hotspots : X à revoir
- Recommandation : 1 action urgente concrète
 
## ✅ Actions Prioritaires
IMPORTANT : écrire exactement 5 actions complètes, ne pas tronquer.
1. [URGENT]    action complète — fichier:ligne si disponible
2. [URGENT]    action complète — fichier:ligne si disponible
3. [IMPORTANT] action complète
4. [IMPORTANT] action complète
5. [NORMAL]    action complète
 
## 📈 Tendance
IMPORTANT : cette section est obligatoire, ne pas l'omettre.
Choisir : [S'améliore ↗ / Se dégrade ↘ / Stable →]
Justification en 1 phrase basée sur :
- Nouveaux bugs depuis dernier scan : {m('new_bugs')}
- Nouvelles vulnérabilités : {m('new_vulnerabilities')}
- Nouveaux code smells : {m('new_code_smells')}
"""
 
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    report   = response.content
 
    print("\n" + "═" * 60)
    print(report)
    print("═" * 60)
 
    return {**state, "report": report}
 

# ═════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═════════════════════════════════════════════════════════════

def build_sonar_graph(tools: dict, llm: ChatSambaNova):
    graph = StateGraph(SonarState)

    # ── Async wrappers ──────────────────────────────────────
    async def _scan_if_needed(s):
        return await node_scan_if_needed(s, tools)

    async def _check_quality_gate(s):
        return await node_check_quality_gate(s, tools)

    async def _fetch_issues(s):
        return await node_fetch_issues(s, tools)

    async def _enrich_issues(s):
        return await node_enrich_issues(s, tools)

    async def _get_fix_plan(s):
        return await node_get_fix_plan(s, tools)

    async def _get_measures(s):
        return await node_get_measures(s, tools)

    async def _evaluate_quality(s):
        return await node_evaluate_quality(s)

    async def _generate_report(s):
        return await node_generate_report(s, llm)

    # ── Nodes ───────────────────────────────────────────────
    graph.add_node("scan_if_needed",     _scan_if_needed)
    graph.add_node("check_quality_gate", _check_quality_gate)
    graph.add_node("fetch_issues",       _fetch_issues)
    graph.add_node("enrich_issues",      _enrich_issues)
    graph.add_node("get_fix_plan",       _get_fix_plan)
    graph.add_node("get_measures",       _get_measures)
    graph.add_node("evaluate_quality",   _evaluate_quality)
    graph.add_node("generate_report",    _generate_report)

    # ── Entry point ─────────────────────────────────────────
    graph.set_entry_point("scan_if_needed")

    # ── Flux linéaire complet — plus de branchement conditionnel ──
    graph.add_edge("scan_if_needed",     "check_quality_gate")
    graph.add_edge("check_quality_gate", "fetch_issues")
    graph.add_edge("fetch_issues",       "enrich_issues")
    graph.add_edge("enrich_issues",      "get_fix_plan")
    graph.add_edge("get_fix_plan",       "get_measures")
    graph.add_edge("get_measures",       "evaluate_quality")
    graph.add_edge("evaluate_quality",   "generate_report")
    graph.add_edge("generate_report",    END)

    return graph.compile()


# ═════════════════════════════════════════════════════════════
# POINT D'ENTRÉE PUBLIC
# ═════════════════════════════════════════════════════════════

async def run_sonar_analysis(
    project_key: str,
    project_path: str = ".",
) -> dict[str, Any]:
    """
    Lance l'analyse SonarQube complète pour un projet.

    Args:
        project_key : clé du projet SonarQube (ex: "my_project")
        project_path: chemin local du code source à scanner si absent

    Returns:
        SonarState final avec report, measures, issues, etc.
    """
    t0 = time.time()

    print("=" * 60)
    print(f"🔍 SONARQUBE DEVOPS AGENT")
    print(f"   Projet : {project_key}")
    print(f"   Path   : {os.path.abspath(project_path)}")
    print("=" * 60)

    print("🔌 Connexion MCP SonarQube...")
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

    print(f"\n⏱️  Analyse terminée en {time.time() - t0:.1f}s")
    return result


# ═════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="🔍 Agent DevOps SonarQube — Scan + Analyse + Rapport",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  # Projet déjà dans SonarQube → rapport direct
  python agent_sonarqube.py my_project

  # Projet absent → scan auto puis rapport
  python agent_sonarqube.py my_project --path C:\\Users\\moham\\...\\my_project

  # Projet dans le répertoire courant
  python agent_sonarqube.py my_project --path .
        """,
    )
    parser.add_argument(
        "project_key",
        help="Clé du projet SonarQube (visible dans Project Information)",
    )
    parser.add_argument(
        "--path",
        default=".",
        help="Chemin local du code source à scanner (défaut: répertoire courant)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_sonar_analysis(args.project_key, args.path))
    except Exception as e:
        print(f"\n❌ {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)