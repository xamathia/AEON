# API locale de démonstration v1

Lancer `scripts/dev`, puis ouvrir http://127.0.0.1:8787. Aucun secret ou service externe requis pour la démonstration synthétique. Python standard ; serveur lié exclusivement à la boucle locale. Ce serveur n’est pas conçu pour être exposé sur Internet.

- `GET /api/status` : `mode: synthetic`, `engine_available` et `planner_available` (booléens), liste des trois intégrations avec statut réel `not_connected` dans cette tranche.
- `GET /api/demo` : `{scenario: <fixture canonique>}`. Une nouvelle copie est retournée à chaque requête.
- `POST /api/simulate` : corps JSON `{traffic: normal}` ou `{traffic: incident}` (valeurs chaînes). Le défaut est normal. En-tête `X-Aeon-Request: demo-v1` et Content-Type application/json requis ; si Origin est présent, il doit correspondre à l’origine locale exacte.
- Réponse : `{scenario, simulation, metrics: {elapsed_ms}, intervention: {kind, synthetic: true, affected_edges}}`. simulation est exactement le résultat du moteur ; elapsed_ms est une mesure, pas un coût monétaire.
- L’incident ajoute 12 minutes aux trois paramètres de chaque trajet positif de la fixture, depuis une copie de la base à chaque appel. Il ne s’accumule jamais ; normal restitue les mêmes données et les mêmes aléas initiaux. La provenance décrit explicitement l’injection.
- Erreurs : `{error: {code, message}}`. 400 pour entrée invalide ; 403 pour origine/en-tête refusés ; 404 pour route inconnue ; 413 au-delà de 64 KiB ; 415 pour mauvais Content-Type ; 503 engine_unavailable si le paquet de Sol manque ; 500 engine_failed pour une erreur interne. Aucune prédiction de remplacement.

Seuls /, /app.js, /styles.css et /favicon.svg sont servis comme fichiers. Aucun accès au disque arbitraire. L’API de cette tranche ne reçoit pas de documents utilisateur et n’enregistre pas de calendrier.

## Recherche de plans privés

`POST /api/plans` accepte le même corps `{traffic: "normal" | "incident"}`, le même défaut et les mêmes protections HTTP que `/api/simulate`. Les champs supplémentaires sont refusés : le navigateur ne fournit ni scénario, ni autorisations, ni catalogue de routes.

Réponse : `{scenario, planning, metrics: {elapsed_ms}, intervention: {kind, synthetic: true, affected_edges}}`. `planning` est exactement la sortie de `packages.aeon_planner.plan_day`, comprenant son baseline, ses plans et les limites de recherche. Chaque plan contient un scénario complet, sa simulation et les opérations horaires avant/après. Le temps mesuré inclut le calcul de référence et l’exploration des candidats, pas un unique appel au moteur.

Le serveur copie la fixture courante (incident inclus) et déclare un horizon de recherche **14:00–21:00 Europe/Paris** le même jour. Cette convention de démonstration empêche de suggérer un créneau de travail nocturne ; ce n’est pas une préférence déduite de l’utilisateur. Le scénario retourné expose cet horizon. Rendez-vous, contraintes et distributions restent identiques, donc les résultats de référence restent ceux de `/api/simulate`.

Le contexte serveur cible `dinner` et autorise uniquement `focus` pour proposition. Les lieux déclarés sont `office` pour meeting/focus et `restaurant` pour dinner. Le catalogue office→restaurant reprend exactement le trajet courant de la fixture ; aucune route retour n’est inventée. La grille est de 15 minutes, le budget de 100 simulations alternatives. Le planificateur retourne au plus trois améliorations strictes, ou `no_better_plan`. `search.truncated` indique une exploration incomplète ; ces options ne constituent pas un optimum global.

Un paquet absent ou sans fonction `plan_day` produit `503 planner_unavailable`. Une erreur interne ou une sortie non sérialisable produit `500 planner_failed`, sans détail privé ni résultat de remplacement. Chaque appel reconstruit ses entrées et transmet des copies au planificateur. Une prévisualisation reste entièrement dans le navigateur ; aucun endpoint d’exécution Calendar n’existe dans cette tranche.
