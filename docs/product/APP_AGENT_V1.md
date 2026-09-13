# Parcours agent réel v1

## Objectif
Assembler le parcours Gmail réel → deadline confirmée → prévision et plans correctifs, avec interfaces simples et budgets visibles. Sprint final autorisé, revue/merge Astra autonomes.

## Propriétaire
Astra

## Fichiers réservés
- apps/api/connections.py
- apps/api/configuration.py
- apps/api/server.py
- apps/api/live.py
- apps/api/intelligence.py
- apps/web/
- tests/api/test_intelligence.py
- tests/api/test_live.py
- tests/api/test_configuration.py
- .env.example
- docs/product/APP_AGENT_V1.md

## Contrats consommés
APP_AUTH_V1, APP_LIVE_V1 ; DeadlineExtractor contrat PR29 stable (revue sans bloquant, fusion après sync) ; plan_live_day(payload, selection) contrat Issue30 stable ; modèle Anthropic port generate(request, max_output_tokens=300) livré séparément.

## Contrats produits
Le serveur conserve une révision par import Gmail et par scénario Calendar. GET /api/intelligence retourne configuration fournisseur, messages déjà lus (id/subject/excerpt/source, pas nouveaux mails), révision Gmail, proposition en attente et métriques. POST /api/intelligence/extract {revision,gmail_revision,message_id,event_ids,consent:true} transmet uniquement l’extrait choisi et 1..20 événements au fournisseur nommé ; port max300, budget10/session. POST /api/intelligence/confirm {revision,gmail_revision,proposal_id} transforme uniquement la proposition serveur en contrainte arrival_deadline liée au message ; aucun champ de contrainte forgé du navigateur. POST /api/intelligence/reset {revision} efface contraintes/proposition mais pas budget. Forget/relecture Gmail invalide ses propositions/contraintes ; import Calendar invalide scénario ; résultat/plans effacés à toute modification d’entrée. Tous POST cookie origine CSRF existants, guards après I/O et erreurs fixes expurgées.
GET /api/live/scenario ajoute intelligence et planning ou champs additionnels compatibles. POST /api/live/plans {revision,target_event_id,movable_event_ids,window:{start,end}} utilise plan_live_day sur snapshot serveur ; classification flexible seulement pour blocs privés explicitement sélectionnés, aucun événement impliquant un tiers n’est rendu mobile. Réponse baseline/plans/search véritables ; pas de nouvelle lecture Routes dans le planner. Prévisualiser/fermer dans UI sans écrire Calendar. Les révisions empêchent anciens plans/réponses de revenir.
Configuration allowlist AEON_ANTHROPIC_API_KEY et AEON_ANTHROPIC_MODEL ; aucune clé côté navigateur ni Git. Modèle facultatif sans casser démo. Interface explique extrait transmis + fournisseur au clic ; preuve textuelle et confirmation visible, appels/tokens/Routes observés, aucune estimation monétaire inventée.

## Dépendances
Astra confirme paths disjoints Sol30 (planner) et adaptateur modèle séparé. Pas d’écriture Calendar dans ce lot. Revue précédente PR29 indépendante réalisée. Sous-agents Astra se partagent frontend et adaptateur sans chevauchement ; root code backend.

## Critères d’acceptation
Parcours fixture trois sources réelles injectées mais signalées tests : deadline Gmail change risque, planner trouve amélioration, preview cohérent. Absence modèle, abstention, budget, données périmées, oubli, invalidation et source incomplète donnent états honnêtes. HTTP et UI tests couvrent effet réel ; flux OAuth/démo existants conservés. Validation navigateur desktop/mobile sur comptes de démo sélectionnés quand configuration disponible.

## Commandes de validation
Tests API intelligence/live ciblés ; node --test apps/web/*.test.js ; runner complet avant handoff ; git diff --check ; revue indépendante puis CI et fusion SHA exact.

## Risques et points ouverts
Ports mémoire/session1h ; aucune écriture ni sync durable ; clé Anthropic locale uniquement. Facturation Maps non liée. La vidéo finale et écriture/undo seront traitées après ce parcours.

## Réponses HTTP complémentaires

GET /api/intelligence et les POST intelligence retournent `{revision,gmail_revision,provider:"Anthropic",model,configured,messages:[{id,subject,excerpt,source}],result:null|{status,reason_code,cached,proposal:null|{event_id,deadline,evidence_quote,source,requires_confirmation,proposal_id}},metrics:{calls,limit:10,remaining_calls,input_tokens,output_tokens},constraints:[]}`. Les métriques cumulent les appels de la session même après oubli/relecture/réinitialisation. `revision` est la révision live Calendar ; `gmail_revision` est opaque et change après nouvel import Gmail ou changement Calendar. `messages` contient seulement les extraits déjà lus. Les contraintes confirmées sont `{id,type:"arrival_deadline",event_id,deadline,source}`.

`GET /api/live/scenario` ajoute `constraints:[]` et `planning:null|{schema_version,status,baseline,plans:[{id,operations,simulation,target_risk_before,target_risk_after,shift_minutes,reason_codes,requires_approval}],search,llm_calls}` ; les scénarios internes ne sortent pas dans les plans. POST plans retourne le même objet live. La prévisualisation est locale et doit être effacée quand la révision ou les observations changent.

## Plan de réalisation

Décision utilisateur : aucun rattachement de facturation Google. Routes devient facultatif ; le trajet par défaut utilise `kind:"osrm"` avec les mêmes champs `origin_address` et `destination_address`, via OpenStreetMap/Nominatim et OSRM (contrat Sol40). `routes.open_routing:true` signale cette option. La réponse travel conserve les champs du prior et ajoute `origin_label`, `destination_label`, `observed_at`, `source.provider:"osrm"` et `kind:"osrm"`. Les 20 tentatives comptent les demandes de trajets de la session ; pas de trafic temps réel OSRM. Attribution OpenStreetMap/ODbL, FOSSGIS/OSRM et lien corriger la carte obligatoires dans l’interface. Claude est autorisé sur les crédits Anthropic déjà disponibles.

- [x] Adapter configuration et état de session, puis tests invalidation/budget.
- [x] Implémenter extraction et confirmation serveur, routes HTTP protégées ; tester deadline effective via vrai moteur.
- [x] Intégrer sélection privée/fenêtre/planner avec absence explicite si module indisponible.
- [ ] Interface frontend déléguée sur apps/web uniquement, utilisant ces réponses.
- [x] Adapter Anthropic livré indépendamment Issue32, planner par Sol Issue30.
- [ ] Recette desktop/mobile, revue indépendante, handoff SHA exact puis fusion.

Validation provider réelle sur données synthétiques (13 septembre 2026) : un appel Claude Haiku 4.5, HTTP200/end_turn, JSON strict sans Markdown, deadline du 14 septembre à18hParis,711 tokens entrée et71 sortie. Aucun message privé transmis. Tests live26 verts après intégration API OSRM injectée ; connecteur réseau Sol40 encore requis avant fusion.
