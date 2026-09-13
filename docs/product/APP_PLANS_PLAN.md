# Comparaison et prévisualisation des plans — Issue #13

## Décision et périmètre

La démo révèle un risque puis propose jusqu’à trois déplacements du seul bloc privé flexible. Les probabilités et horaires proviennent de `plan_day` et de ses simulations. L’interface permet de prévisualiser chaque option et de revenir à la référence, sans écriture Calendar. La recherche est bornée, sans promesse d’optimum ou d’économie monétaire mesurée.

Réservations Astra : `apps/`, `tests/api/`, `APP_API_V1.md`, ce plan et `README.md`. Sol reste propriétaire de ses paquets. Une délégation locale peut modifier seulement `apps/web/` ; Astra implémente l’API, les tests HTTP, la documentation et la revue finale.

## Contrat de la tranche

`POST /api/plans`, corps `{traffic: "normal" | "incident"}`, renvoie `{scenario, planning, metrics: {elapsed_ms}, intervention: {kind, synthetic: true, affected_edges}}`. `planning` est la sortie intégrale de `plan_day`. Les protections HTTP existantes sont partagées. Le serveur construit ses propres lieux et permissions de proposition ; aucun scénario ou plan arbitraire n’est accepté du navigateur. Une dépendance absente produit `503 planner_unavailable`, jamais de plan inventé. `GET /api/status` ajoute `planner_available`.

La copie du scénario utilisée pour la planification possède un horizon déclaré de 14:00 à 21:00, le jour de la fixture, dans son fuseau. Il s’agit d’une convention visible de démo pour empêcher une proposition de focus nocturne. Tous les rendez-vous et contraintes existants restent identiques. Le contexte déclare `meeting` et `focus` à `office`, `dinner` à `restaurant`, seul `focus` mobile, cible `dinner`. Le catalogue reprend le trajet aller du scénario courant, incident inclus. Aucune route retour n’est inventée. Grille de 15 minutes, budget de 100 candidats.

L’interface conserve séparément référence et prévisualisation. Chaque carte expose avant/après, déplacement et risque résiduel. Les options deviennent obsolètes après un nouveau calcul réussi du trafic. Une erreur garde les derniers résultats cohérents et permet de réessayer. Les propositions absentes, la dépendance indisponible, la recherche en cours et la recherche tronquée ont des états explicites.

## Étapes et validation

1. Réserver, créer la branche et publier une draft PR avec ce contrat.
2. Écrire des tests HTTP des plans avant le serveur : disponibilité, requêtes invalides, contexte borné, incident sans accumulation, absence de mutation et erreurs expurgées.
3. Implémenter l’API ; déléguer en parallèle l’interface dans `apps/web/`.
4. Relire et intégrer le paquet Sol sur un SHA vérifié.
5. Exécuter les tests complets et le vrai parcours HTTP avec le planner. Vérifier dans le navigateur analyse → plans → prévisualisation → retour → incident → nouveaux plans, puis mobile.
6. Documenter les résultats observés, effectuer la revue indépendante, synchroniser, valider le SHA final et fusionner après CI.

## Validation observée

Le 13 septembre 2026, les tests de frontière HTTP passent avec un planner injecté et avec le vrai paquet Sol chargé depuis son worktree de revue. La revue indépendante de l’API et de l’interface ne relève aucun point bloquant.

Parcours réel observé dans le navigateur : analyse normale (risque 99,6 %, arrivée médiane 18:13), recherche (9 candidats évalués, trois options), prévisualisation du focus à 15:30–15:50 (risque 43 %, arrivée médiane 17:53), fermeture (retour exact à 99,6 % / 18:13), incident (100 % / 18:25 et anciennes options supprimées), nouvelle recherche (90,9 % de risque résiduel). Ces chiffres sont des observations de la fixture et ne sont codés en dur dans aucune vue.

À 375 pixels de largeur de contenu mobile, les cartes et le calendrier restent dans le viewport, sans débordement horizontal. La prévisualisation reçoit le focus ; son bouton de fermeture restaure la référence. Dépendance absente : message d’indisponibilité et analyse conservée. Coupure volontaire du serveur de test : résultats précédents conservés, bouton de reprise disponible et message de connexion en français.

Après fusion du planner corrigé via #18, les suites sont rejouées sans chemin externe : **78 tests réussis** (17 coordination, 17 API, 22 moteur, 22 planner), dont le parcours HTTP avec les vrais paquets. Aucun test intégré n’est ignoré. Aucune connexion Google ou écriture externe n’est validée par cette tranche.
