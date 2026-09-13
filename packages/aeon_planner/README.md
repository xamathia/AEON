# ÆON Planner — plans live avec fenêtre autorisée

Le planner produit uniquement des propositions déterministes. Il ne lit aucun
service, n'appelle aucun LLM et n'écrit jamais dans Calendar. `plan_day` reste
l'API compatible de la démonstration. La variante live construit son contexte à
partir des seules observations présentes dans un scénario canonique.

## API

```python
from packages.aeon_planner import plan_live_day

result = plan_live_day(payload, selection)
```

`payload` respecte le contrat moteur et provient de l'assembleur live. La fonction
ne modifie ni le scénario ni la sélection. Elle conserve le format de retour de
`plan_day` : `schema_version`, `status`, `baseline`, au plus trois `plans`,
`search` et `llm_calls=0`. Le scénario et son horizon ne sont jamais remplacés ou
élargis dans un candidat.

`selection` contient exactement :

```text
{
  target_event_id,
  movable_event_ids,
  window: {start, end},
  step_minutes,
  max_candidates
}
```

- la cible existe et ne figure pas parmi les événements mobiles;
- `movable_event_ids` contient de un à cinq identifiants uniques d'événements
  `private=true` et `classification="flexible"`;
- `window.start` et `window.end` sont des instants ISO 8601 avec offset explicite,
  compris dans l'horizon. La fenêtre est positive et dure au plus 24 heures;
- `step_minutes` est un entier entre 5 et 60;
- `max_candidates` est un entier entre 1 et 100.

Les clés supplémentaires, booléens numériques, dates naïves, Unicode invalide et
références inconnues sont refusés par `ValueError`. La grille est ancrée au début
de la fenêtre et seuls les déplacements entièrement contenus dans cette fenêtre
sont évalués. La durée de chaque événement reste identique.

## Construction déterministe des lieux et routes

Chaque événement commence dans un groupe de lieu distinct. Deux groupes sont
fusionnés uniquement lorsqu'une arête canonique les reliant a une durée constante
nulle et une source `provider="user"`. Cette déclaration explicite de même lieu
est une équivalence; elle peut donc servir dans les deux ordres sans inventer une
symétrie de trajet. En mode live, sa source utilisateur peut être synthétique ou
non synthétique. Les règles existantes du mode synthetic restent inchangées.

Après constitution des groupes, chaque arête canonique non nulle devient une
observation orientée du groupe source vers le groupe cible. L'ordre inverse n'est
jamais déduit. Une paire sans observation produit un rejet de candidat
`MISSING_ROUTE`. Si deux arêtes donnent des durées ou des sources différentes pour
la même paire de groupes, ou si une route non nulle se retrouve à l'intérieur
d'un groupe déclaré identique, l'entrée est ambiguë et `plan_live_day` lève un
`ValueError` avant la recherche.

Les durées et les champs `provider`, `reference` et `synthetic` des sources sont
conservés. Chaque source réutilisée dans un candidat reçoit dans `assumption` la
mention fixe que le prior observé est réutilisé après déplacement et ne constitue
pas une nouvelle mesure de trafic. Le scénario de référence conserve exactement
ses sources originales.

## Recherche et limites

La recherche réutilise le moteur réel et le classement de `plan_day`. Les
événements fixes, contraintes Gmail, seed, samples, provenance et horizon sont
préservés. Un chevauchement planifié ou une route absente rejette le candidat; le
plafond compte les candidats effectivement simulés. `search.truncated` indique
que ce plafond a interrompu la recherche, sans revendiquer un optimum global.

Un risque strictement inférieur est requis pour retourner un plan. En l'absence
d'amélioration réelle, le statut est `no_better_plan`. Les sources de trafic sont
des observations stationnaires réutilisées, jamais des requêtes correspondant au
nouvel horaire. Tous les plans conservent `requires_approval=true` et ne confèrent
aucune permission d'écriture.
