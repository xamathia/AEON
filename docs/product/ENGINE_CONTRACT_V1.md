# ÆON — contrat moteur v1

Statut : défini par Astra pour la première tranche de simulation. La spécification produit approuvée est conservée dans AEON_SPECIFICATIONS_COMPLETES.md. Sa planification en semaines ne s’applique pas au hackathon ; la vidéo finale reste limitée à deux minutes. Ce contrat ne remplace pas les fonctionnalités ultérieures (plans, connecteurs, politiques, audit et interface).

## Responsabilités et interface

Sol implémente un moteur Python 3.9+ utilisant uniquement la bibliothèque standard, isolé dans `packages/aeon_engine/`. L’API publique est :

```python
from packages.aeon_engine import simulate_day
result = simulate_day(payload)  # dict JSON → dict JSON, sans mutation de payload
```

CLI depuis la racine : `python3 -m packages.aeon_engine fixtures/demo/day-v1.json`. Elle écrit un seul objet JSON sur stdout, les erreurs sur stderr, et retourne un code non nul pour une entrée invalide. Aucun appel réseau, LLM, horloge système, fichier personnel, mutation d’agenda ou secret requis. Le nom historique simulate_day accepte une séquence allant jusqu’à sept jours.

Le moteur consomme uniquement le modèle canonique ci-dessous. Les structures Google sont converties par des adaptateurs indépendants. Aucun framework web, serveur ou dépendance ne doit être ajouté dans cette tâche.

## Entrée JSON v1

- `schema_version` : chaîne `1.0`.
- `scenario_id` : identifiant opaque non vide.
- `mode` : `synthetic` ou `live` ; chaque source conserve sa propre provenance.
- `timezone` : identifiant IANA valide, par exemple Europe/Paris. Les calculs utilisent des instants UTC ; timezone sert à la présentation ultérieure.
- `horizon.start`, `horizon.end` : ISO 8601 avec offset explicite ou Z ; intervalle positif d’au plus sept jours.
- `seed` : entier de 0 à 2**32-1 ; `samples` : entier de 1 à 10000, démonstration à 1000.
- `events` : liste de 1 à 100 événements triés par `planned_start`, avec identifiants uniques. Événements de durée positive entièrement dans l’horizon ; les chevauchements planifiés sont acceptés pour révéler leur risque.
- Chaque événement contient `id`, `title`, `planned_start`, `planned_end`, `classification` (`fixed` ou `flexible`), `private` booléen, `overrun_minutes` et `source`.
- `overrun_minutes` est une distribution triangulaire `{min, mode, max}` de minutes supplémentaires : valeurs finies, 0 ≤ min ≤ mode ≤ max. Une distribution constante est autorisée et retourne sa valeur sans division par zéro.
- `travel_edges` : exactement une arête par paire d’événements adjacents, y compris lorsqu’il n’y a aucun déplacement (durée constante zéro). Champs : `from_event_id`, `to_event_id`, `duration_minutes` triangulaire, `source`. Aucune arête manquante, doublonnée ou non adjacente ; ne jamais présumer qu’un trajet inconnu prend zéro minute.
- `constraints` : liste de contraintes `arrival_deadline`, chacune avec `id` unique non vide, `type` obligatoire égal à `arrival_deadline`, `event_id`, `deadline` ISO avec offset, et `source`. Tout autre type est refusé, jamais interprété implicitement comme une arrivée. Les cibles doivent exister ; deadline doit être dans l’horizon. Plusieurs deadlines d’une même cible se réduisent à la plus précoce. Une liste vide est valide.
- `source` : `{provider, reference, synthetic, assumption}`. Providers autorisés : `google_calendar`, `gmail`, `google_routes`, `user`. reference est un identifiant opaque non vide ; synthetic un booléen ; assumption une chaîne décrivant toute hypothèse. En mode synthetic, toutes les sources sont marquées synthetic=true. En mode live, le résultat conserve les sources synthétiques éventuelles au lieu de les présenter comme réelles, dans la liste `sources` de sortie. Les occurrences d’une même reference doivent avoir un descripteur source identique ; sinon refuser l’entrée pour provenance ambiguë.

Les chaînes dates sans fuseau, nombres booléens à la place d’entiers, NaN/Infinity, durées négatives, références inconnues et champs obligatoires manquants sont refusés par ValueError (message exploitable). Les propriétés supplémentaires sont ignorées pour permettre l’enrichissement du modèle.

## Calcul reproductible

Pour chaque tirage, parcourir les événements dans l’ordre fourni :

1. Le premier a actual_arrival = actual_start = planned_start. Pour chaque suivant, actual_arrival = actual_end du précédent + trajet échantillonné, puis actual_start = max(planned_start, actual_arrival). Une arrivée anticipée attend le début planifié ; elle ne décale pas le rendez-vous.
2. actual_end = actual_start + durée planifiée + dépassement échantillonné. Ne pas tronquer un événement fixe pour améliorer artificiellement le résultat. Dépasser l’horizon en simulation est permis et doit être conservé.
3. La limite d’arrivée d’un événement est le minimum entre son planned_start et ses contraintes arrival_deadline. Le retard vaut max(0, actual_arrival - limite) en minutes. Un retard strictement positif compte comme échec.
4. Agréger les tirages, sans LLM. Une probabilité vaut nombre d’échecs / samples.

Utiliser `random.Random` local. Pour chaque variable, dériver une graine stable avec SHA-256 de la représentation JSON compacte `[seed, kind, id]` (UTF-8, ensure_ascii=False, separators=(',', ':')), puis convertir le digest complet en entier. `kind` vaut `overrun` (id événement) ou `travel` (id = tableau JSON `[from_event_id, to_event_id]` ; ne pas concaténer les identifiants). Chaque variable a son propre générateur. Prélever un tirage par variable et par simulation, même si sa valeur n’affecte pas le risque ciblé. Ne pas utiliser hash() Python. Les plans futurs pourront ainsi partager les mêmes aléas par identifiant.

Quantiles : ordre croissant, interpolation linéaire à l’indice `(n-1)*q` pour q = 0.1, 0.5 et 0.9. Les instants agrégés sont retournés en ISO UTC avec Z, arrondis à la seconde ; les minutes à trois décimales. Les probabilités restent des fractions entre 0 et 1. Aucun pourcentage narratif 14/61/8 n’est imposé.

## Sortie JSON v1

```json
{
  "schema_version": "1.0",
  "scenario_id": "id-entree",
  "mode": "synthetic",
  "seed": 42,
  "samples": 1000,
  "events": [
    {
      "event_id": "id-evenement",
      "arrival": {"p10": "ISO UTC", "p50": "ISO UTC", "p90": "ISO UTC"},
      "start": {"p10": "ISO UTC", "p50": "ISO UTC", "p90": "ISO UTC"},
      "end": {"p10": "ISO UTC", "p50": "ISO UTC", "p90": "ISO UTC"},
      "late_arrival_probability": 0.0,
      "delay_minutes": {"p10": 0.0, "p50": 0.0, "p90": 0.0},
      "deadline": "ISO UTC",
      "reason_codes": [],
      "evidence_refs": []
    }
  ],
  "summary": {"probability_any_late": 0.0, "expected_total_delay_minutes": 0.0},
  "sources": [],
  "assumptions": [],
  "llm_calls": 0
}
```

Ce JSON illustre la structure, pas les résultats de la fixture. `probability_any_late` mesure la fraction des tirages avec au moins un événement en retard (pas la somme des probabilités). `expected_total_delay_minutes` est la moyenne des sommes de retard, même si cela compte plusieurs conséquences d’une même cascade ; l’UI ne doit pas l’appeler « minutes économisées ».

`sources` : liste de tous les objets source de l’entrée, dédupliqués par reference et triés par reference ; conserver provider, reference, synthetic et assumption. Le mode global live ne transforme jamais une source synthétique en donnée réelle.

`reason_codes` : liste dédupliquée et triée parmi `MEETING_OVERRUN_LIKELY` (dépassement non nul dans la chaîne), `INSUFFICIENT_TRAVEL_BUFFER` (trajet positif dans la chaîne) et `DEADLINE_AT_RISK` (deadline Gmail plus précoce que le début planifié). N’inclure ces causes potentielles que si la probabilité de retard de la cible est positive ; ce sont des hypothèses de modèle, pas une attribution causale prouvée. `evidence_refs` contient les références sources de la chaîne jusqu’à cet événement et de ses contraintes, triées et dédupliquées. `assumptions` contient les chaînes assumption non vides de l’entrée, triées et dédupliquées. Conserver ces preuves même si aucun retard n’est prédit.

## Validation et démonstration

La fixture day-v1.json est entièrement synthétique : réunion fixe, bloc privé flexible, dîner fixe ; Gmail avance l’heure d’arrivée, et une distribution de trajet construite par ÆON représente une hypothèse de trafic. Les min/mode/max ne sont pas des quantiles fournis par Google Routes.

Tests attendus : seed identique → sortie identique ; aucun changement de l’entrée ; trajets/dépassements constants → résultats calculables à la main ; propagation d’un retard ; risque « au moins un » distinct de somme ; contrainte Gmail prise en compte ; changement d’offset UTC et heure d’été ; distrib constante ; erreurs de schéma/références ; quantiles ordonnés ; probabilities bornées ; mêmes identifiants → mêmes aléas entre scénarios. Mesurer 1000 tirages et rapporter le temps observé dans le handoff sans le figer comme une garantie universelle.

L’amélioration du planning fera l’objet d’une tâche suivante. Ajouter un départ impératif ne réduit pas magiquement une durée fixe. Les actions Calendar exigent toujours les permissions produit, ETags, audit et compensation conditionnelle. La suppression de la revue humaine du code ne supprime pas le consentement utilisateur dans ÆON.
