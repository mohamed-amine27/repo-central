"""
Agent Gatekeeper CI/CD — Décideur qualité pour merge vers main.

Rôle :
  - Analyse le code via SonarQube
  - Évalue la qualité selon des critères stricts
  - Décide PASS ou BLOCK
  - Retourne exit code 0 (PASS) ou 1 (BLOCK) pour GitHub Actions

Usage :
  python agent_gatekeeper.py <project_key> --path <chemin> --branch <branche>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from typing import Any

from agent_sonarqube import run_sonar_analysis



# ═════════════════════════════════════════════════════════════
# CRITÈRES DU GATEKEEPER
# Modifier ces seuils selon les standards de ton projet
# ═════════════════════════════════════════════════════════════

GATE_CRITERIA = {
    # ── Fiabilité ──────────────────────────────────────────
    "bugs":                     0,     # 0 bug toléré
    "new_bugs":                 0,     # 0 nouveau bug toléré

    # ── Sécurité ───────────────────────────────────────────
    "vulnerabilities":          0,     # 0 vulnérabilité tolérée
    "new_vulnerabilities":      0,     # 0 nouvelle vulnérabilité tolérée
    "blocker_issues":           0,     # 0 issue BLOCKER tolérée
    "critical_issues":          5,     # max 5 issues CRITICAL

    # ── Couverture ─────────────────────────────────────────
    "coverage_min":             20.0,  # minimum 20% coverage

    # ── Maintenabilité ─────────────────────────────────────
    "code_smells_max":          50,    # maximum 50 code smells
    "sqale_index_max":          120,   # maximum 120 min dette technique

    # ── Duplications ───────────────────────────────────────
    "duplicated_lines_max":     15.0,  # maximum 15% duplication
}


# ═════════════════════════════════════════════════════════════
# HELPER
# ═════════════════════════════════════════════════════════════

def get_measure(measures: dict, key: str, default: float = 0.0) -> float:
    """Extrait une métrique du dict measures."""
    val = measures.get(key, default)
    if isinstance(val, dict):
        val = val.get("value", default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ═════════════════════════════════════════════════════════════
# ÉVALUATION GATEKEEPER
# ═════════════════════════════════════════════════════════════

def evaluate_gate(
    measures: dict,
    issues: list,
) -> tuple[str, list[dict]]:
    """
    Évalue si le code peut être mergé.

    Returns:
        ("PASS" | "BLOCK", liste des violations)
    """
    violations = []

    # ── Fiabilité ──────────────────────────────────────────
    bugs = int(get_measure(measures, "bugs"))
    if bugs > GATE_CRITERIA["bugs"]:
        violations.append({
            "category": "🔴 Fiabilité",
            "message":  f"Bugs: {bugs} détecté(s) — 0 toléré",
            "severity": "CRITICAL",
        })

    new_bugs = int(get_measure(measures, "new_bugs"))
    if new_bugs > GATE_CRITERIA["new_bugs"]:
        violations.append({
            "category": "🔴 Fiabilité",
            "message":  f"Nouveaux bugs: {new_bugs} introduit(s) — 0 toléré",
            "severity": "CRITICAL",
        })

    # ── Sécurité ───────────────────────────────────────────
    vulns = int(get_measure(measures, "vulnerabilities"))
    if vulns > GATE_CRITERIA["vulnerabilities"]:
        violations.append({
            "category": "🔐 Sécurité",
            "message":  f"Vulnérabilités: {vulns} détectée(s) — 0 tolérée",
            "severity": "BLOCKER",
        })

    new_vulns = int(get_measure(measures, "new_vulnerabilities"))
    if new_vulns > GATE_CRITERIA["new_vulnerabilities"]:
        violations.append({
            "category": "🔐 Sécurité",
            "message":  f"Nouvelles vulnérabilités: {new_vulns} introduite(s) — 0 tolérée",
            "severity": "BLOCKER",
        })

    # Compter BLOCKER et CRITICAL dans les issues
    blockers  = sum(1 for i in issues if i.get("severity") == "BLOCKER")
    criticals = sum(1 for i in issues if i.get("severity") == "CRITICAL")

    if blockers > GATE_CRITERIA["blocker_issues"]:
        violations.append({
            "category": "🔐 Sécurité",
            "message":  f"Issues BLOCKER: {blockers} — 0 tolérée",
            "severity": "BLOCKER",
        })

    if criticals > GATE_CRITERIA["critical_issues"]:
        violations.append({
            "category": "⚠️  Qualité",
            "message":  f"Issues CRITICAL: {criticals} > {GATE_CRITERIA['critical_issues']} tolérées",
            "severity": "CRITICAL",
        })

    # ── Couverture ─────────────────────────────────────────
    coverage = get_measure(measures, "coverage")
    if coverage < GATE_CRITERIA["coverage_min"]:
        violations.append({
            "category": "🧪 Couverture",
            "message":  f"Coverage: {coverage:.1f}% < {GATE_CRITERIA['coverage_min']}% requis",
            "severity": "MAJOR",
        })

    # ── Maintenabilité ─────────────────────────────────────
    smells = int(get_measure(measures, "code_smells"))
    if smells > GATE_CRITERIA["code_smells_max"]:
        violations.append({
            "category": "🧹 Maintenabilité",
            "message":  f"Code Smells: {smells} > {GATE_CRITERIA['code_smells_max']} tolérés",
            "severity": "MAJOR",
        })

    debt = int(get_measure(measures, "sqale_index"))
    if debt > GATE_CRITERIA["sqale_index_max"]:
        violations.append({
            "category": "🧹 Maintenabilité",
            "message":  f"Dette technique: {debt}min > {GATE_CRITERIA['sqale_index_max']}min tolérée",
            "severity": "MAJOR",
        })

    # ── Duplications ───────────────────────────────────────
    duplication = get_measure(measures, "duplicated_lines_density")
    if duplication > GATE_CRITERIA["duplicated_lines_max"]:
        violations.append({
            "category": "📋 Duplications",
            "message":  f"Duplications: {duplication:.1f}% > {GATE_CRITERIA['duplicated_lines_max']}% tolérées",
            "severity": "MINOR",
        })

    decision = "BLOCK" if violations else "PASS"
    return decision, violations


# ═════════════════════════════════════════════════════════════
# RAPPORT GATEKEEPER
# ═════════════════════════════════════════════════════════════

def print_gate_report(
    decision:   str,
    violations: list,
    project:    str,
    branch:     str,
    measures:   dict,
    duration:   float,
) -> None:
    """Affiche le rapport du gatekeeper."""

    print("\n" + "═" * 60)
    print(f"🚦 GATEKEEPER REPORT — {project} ({branch})")
    print("═" * 60)

    if decision == "PASS":
        print("✅  DÉCISION : MERGE AUTORISÉ")
        print("    Tous les critères qualité sont respectés.")
    else:
        print("❌  DÉCISION : MERGE BLOQUÉ")
        print(f"    {len(violations)} violation(s) détectée(s) :\n")
        for v in violations:
            severity_icon = {
                "BLOCKER":  "🔴",
                "CRITICAL": "🟠",
                "MAJOR":    "🟡",
                "MINOR":    "🔵",
            }.get(v["severity"], "⚪")
            print(f"    {severity_icon} [{v['category']}] {v['message']}")

    print("\n── Résumé métriques ──────────────────────────────────")
    print(f"   Bugs              : {int(get_measure(measures, 'bugs'))}")
    print(f"   Vulnérabilités    : {int(get_measure(measures, 'vulnerabilities'))}")
    print(f"   Coverage          : {get_measure(measures, 'coverage'):.1f}%")
    print(f"   Code Smells       : {int(get_measure(measures, 'code_smells'))}")
    print(f"   Dette technique   : {int(get_measure(measures, 'sqale_index'))} min")
    print(f"   Duplications      : {get_measure(measures, 'duplicated_lines_density'):.1f}%")
    print(f"   Nouveaux bugs     : {int(get_measure(measures, 'new_bugs'))}")
    print(f"   Nouvelles vulnés  : {int(get_measure(measures, 'new_vulnerabilities'))}")
    print("═" * 60)
    print(f"⏱️  Durée totale : {duration:.1f}s")
    print("═" * 60)


# ═════════════════════════════════════════════════════════════
# EXPORT JSON (pour GitHub Actions)
# ═════════════════════════════════════════════════════════════

def export_json_report(
    decision:   str,
    violations: list,
    project:    str,
    branch:     str,
    measures:   dict,
) -> None:
    """Exporte le rapport en JSON pour GitHub Actions."""
    report = {
        "decision":   decision,
        "project":    project,
        "branch":     branch,
        "violations": violations,
        "summary": {
            "bugs":               int(get_measure(measures, "bugs")),
            "vulnerabilities":    int(get_measure(measures, "vulnerabilities")),
            "coverage":           get_measure(measures, "coverage"),
            "code_smells":        int(get_measure(measures, "code_smells")),
            "sqale_index":        int(get_measure(measures, "sqale_index")),
            "new_bugs":           int(get_measure(measures, "new_bugs")),
            "new_vulnerabilities":int(get_measure(measures, "new_vulnerabilities")),
        },
    }

    output_path = "gatekeeper_report.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n📄 Rapport JSON exporté : {output_path}")


# ═════════════════════════════════════════════════════════════
# POINT D'ENTRÉE PRINCIPAL
# ═════════════════════════════════════════════════════════════

async def run_gatekeeper(
    project_key:  str,
    project_path: str = ".",
    branch:       str = "main",
) -> dict[str, Any]:
    """
    Lance l'analyse complète et décide si le merge est autorisé.

    Returns:
        dict avec decision, violations, measures, report
    """
    t0 = time.time()

    print("=" * 60)
    print(f"🚦 AGENT GATEKEEPER CI/CD")
    print(f"   Projet  : {project_key}")
    print(f"   Branche : {branch}")
    print(f"   Path    : {os.path.abspath(project_path)}")
    print("=" * 60)

    # ── Étape 1 : analyse SonarQube complète ───────────────
    print("\n📊 Lancement analyse SonarQube...")
    sonar_result = await run_sonar_analysis(project_key, project_path)

    measures = sonar_result.get("measures", {})
    issues   = sonar_result.get("issue_contexts", [])
    report   = sonar_result.get("report", "")

    # ── Étape 2 : évaluation gatekeeper ────────────────────
    print("\n🔍 Évaluation des critères gatekeeper...")
    decision, violations = evaluate_gate(measures, issues)

    # ── Étape 3 : rapport ──────────────────────────────────
    duration = time.time() - t0
    print_gate_report(decision, violations, project_key, branch, measures, duration)

    # ── Étape 4 : export JSON pour GitHub Actions ──────────
    export_json_report(decision, violations, project_key, branch, measures)

    return {
        "decision":   decision,
        "violations": violations,
        "project":    project_key,
        "branch":     branch,
        "measures":   measures,
        "report":     report,
    }


# ═════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="🚦 Agent Gatekeeper CI/CD — Décideur qualité pour merge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python agent_gatekeeper.py my_project --path . --branch feature/auth
  python agent_gatekeeper.py my_project --path C:\\...\\my_project --branch develop

Exit codes :
  0 = PASS  → merge autorisé   → GitHub Actions continue
  1 = BLOCK → merge bloqué     → GitHub Actions échoue
        """,
    )
    parser.add_argument(
        "project_key",
        help="Clé du projet SonarQube",
    )
    parser.add_argument(
        "--path",
        default=".",
        help="Chemin local du code source",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Nom de la branche analysée",
    )
    args = parser.parse_args()

    try:
        result = asyncio.run(run_gatekeeper(
            args.project_key,
            args.path,
            args.branch,
        ))

        # Exit code pour GitHub Actions :
        # 0 = PASS  → pipeline continue → merge autorisé
        # 1 = BLOCK → pipeline échoue  → merge bloqué
        sys.exit(0 if result["decision"] == "PASS" else 1)

    except Exception as e:
        print(f"\n❌ {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)