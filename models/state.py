"""
State SonarQube — Définition du state partagé entre tous les nodes.
"""

from __future__ import annotations
from typing import TypedDict, List


class SonarState(TypedDict):
    # ── Entrée ──────────────────────────────────────────────
    project_key:      str    # Clé du projet SonarQube
    project_path:     str    # Chemin local du code source

    # ── Node 0 : scan ───────────────────────────────────────
    scan_done:        bool   # True si scan lancé automatiquement

    # ── Node 1 : quality gate ───────────────────────────────
    quality_gate:     dict   # Résultat brut SonarQube gate (info seulement)

    # ── Node 2 : issues ─────────────────────────────────────
    issues:           list   # Issues brutes

    # ── Node 3 : enrichissement ─────────────────────────────
    issue_contexts:   list   # Issues enrichies (contexte + règle)

    # ── Node 4 : fix plan ───────────────────────────────────
    fix_plan:         dict   # Plan de correction SonarQube

    # ── Node 5 : métriques ──────────────────────────────────
    measures:         dict   # Métriques complètes de qualité

    # ── Node 6 : évaluation réelle ──────────────────────────
    gate_failed:      bool   # Décision RÉELLE basée sur métriques
    quality_reasons:  list   # Raisons du FAIL (ex: "6 vulnérabilités")

    # ── Node 7 : rapport ────────────────────────────────────
    report:           str    # Rapport final LLM

    # ── Erreur globale ──────────────────────────────────────
    error:            str    # Message d'erreur si échec critique