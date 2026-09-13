# Prévision réelle locale — contrat v1

Issue #26, propriétaire Astra. Consomme l’assembleur #22, le moteur et les connecteurs. Tous les appels sont liés à la session OAuth locale existante. GET est sans réseau ; les POST exigent origine canonique, cookie et CSRF. Aucune écriture Google. Aucun texte Gmail interprété dans cette tranche : `constraints=[]`.

## Réponse commune

GET `/api/live/scenario` et les trois POST retournent le même objet :

```json
{
  "revision": null,
  "status": "incomplete",
  "calendar": null,
  "travel_pairs": [],
  "routes": {"configured": false, "attempts": 0, "limit": 20, "remaining": 20},
  "issues": [{"code": "CALENDAR_NOT_READ", "event_ids": []}],
  "result": null
}
```

Après lecture Calendar, `calendar` contient `{read_at,complete,last_read_failed,horizon,timezone,events}`. `events` est la projection publique de `/api/connections`, triée chronologiquement UTC ; les données canoniques originales restent côté serveur. `revision` est opaque. `travel_pairs` contient chaque paire adjacente : `{from_event_id,to_event_id,from_title,to_title,departure_time,travel:null|{kind,duration_minutes,source,observed_at}}`. `departure_time` est la fin prévue du premier événement, avec offset. `observed_at` est null pour une déclaration de même lieu. Les horaires des événements ne changent jamais.

`status` vaut `incomplete`, `ready` ou `simulated`. `result`, lorsqu’il existe, est le JSON moteur original avec ses probabilités et hypothèses. Aucun pourcentage prédéfini. Les codes de manque incluent CALENDAR_NOT_READ, CALENDAR_INCOMPLETE, NO_EVENTS, MISSING_TRAVEL_EDGE, CALENDAR_TOO_LARGE, ASSEMBLER_UNAVAILABLE. Un échec de lecture ultérieur conserve la dernière révision, avec `last_read_failed=true` visible. Les résultats ne remplacent pas ceux de la démonstration synthétique.

## Actions

POST `/api/live/travel` : `{revision,from_event_id,to_event_id,kind}`. `kind="same_location"` déclare explicitement un même lieu : durée zéro, source user, hypothèse visible. `kind="google_routes"` exige aussi `origin_address` et `destination_address` (1 à 500 caractères chacune). Le départ est celui affiché pour la paire ; un départ passé est refusé avant tout appel Google. Aucune adresse n’est déduite d’un titre Calendar. La présence d’une clé n’est pas une lecture réussie.

Chaque action Routes lance au plus une requête ; aucune relance automatique. Maximum vingt tentatives par session, échecs compris ; le compteur survit aux remises à zéro et changements de calendrier. Une seule action trajet/simulation est active à la fois par session. Même lieu ne consomme pas d’appel Routes. Un manque de clé ne consomme pas de tentative. Ce plafond de requêtes ne constitue pas une garantie monétaire.

POST `/api/live/simulate` : `{revision}`. L’assembleur reçoit uniquement les caches serveur, horizon et fuseau de la lecture, `constraints=[]`, seed42, samples1000. S’il retourne incomplete, aucun appel au moteur. Une dépendance absente est signalée. Le moteur fournit les seules valeurs affichées ; les priors restent non calibrés.

POST `/api/live/reset` : `{revision}`. Efface trajets et résultat, renouvelle la révision et garde la lecture Calendar. Ne réinitialise pas le budget Routes et n’efface aucun agenda.

Nouvelle lecture Calendar réussie, reconnexion, oubli ou expiration invalident les trajets et résultats. Une réponse réseau/calcul arrivée après cette invalidation ne peut pas repeupler le cache. Une lecture en cours bloque les nouvelles actions ; une erreur de lecture conserve le précédent import en le signalant. Plus de100 événements produit une limite explicite, sans tronquage. Les erreurs utilisent `{error:{code,message}}` et des messages fixes, sans adresse ni erreur brute de fournisseur.

## Interface et validation

Panneau « Prévoir mon agenda réel », distinct de la démo. Actualisation sans lecture Google ; choix de même lieu ou deux adresses par paire ; départ et compteur visibles avant le clic ; calcul explicite ; tableau événement/début prévu/arrivée médiane/p10-p90/risque. Liste des informations manquantes, hypothèses et limite sans Gmail. Les titres, hypothèses et messages importés sont affichés comme texte. Tests hors réseau sur ports injectés, pas de `.env` réel.

Vérification navigateur effectuée avec observations injectées, vrai assembleur et vrai moteur : import Calendar, trajet manquant, lecture de trajet injectée, budget consommé, calcul de 1 000 tirages, tableau et hypothèses conservées. Interface contrôlée à 1280 px et 375 px : champs utilisables, table défilante et aucun débordement de page. Aucun compte Google ni Routes réel appelé durant ces essais. Le bouton d’actualisation ne lit que l’état serveur ; après erreur d’action, seul ce GET peut être effectué pour remettre à jour le budget, jamais un rejeu du POST.

Après fusion du module d’assemblage, les 21 tests du parcours réel passent, dont une intégration avec les paquets assembleur et moteur réellement importés. Les tests de dépendance absente utilisent un port distinct ; le test d’intégration ne saute pas silencieusement si le paquet attendu manque.
