# Connexions locales ÆON — contrat HTTP v1

L’Issue #20 réserve cette intégration à Astra. La démo synthétique reste indépendante ; une autorisation OAuth seule ne prouve pas qu’une lecture API a réussi.

## Parcours

1. `GET /api/session` établit une session serveur opaque et fournit `csrf_token` ; cookie host-only `HttpOnly; SameSite=Lax; Path=/`, durée et registre bornés.
2. `POST /api/oauth/{google_calendar|gmail}/begin` prépare un consentement à une seule source, avec cookie, origine exacte et `X-Aeon-CSRF` requis.
3. Le callback Google Desktop `GET /?state=…&code=…` exige le cookie et la tentative correspondante. Les paramètres sensibles sont uniques, consommés une seule fois et effacés de l’URL par `303 /`. Aucun journal de query.
4. `GET /api/connections` distingue configuration, autorisation, lecture réussie et données incomplètes. `POST /api/oauth/{provider}/forget` efface seulement la connexion et les données locales de la source.
5. `POST /api/calendar/read` lit `primary` sur un horizon explicite maximal de sept jours, avec pagination bornée. `POST /api/gmail/read` effectue une recherche demandée explicitement, avec au plus dix extraits.

L’origine canonique est `http://127.0.0.1:<port>/` : l’application et le consentement Google doivent être ouverts dans le même navigateur. La configuration locale est lue une fois, environnement prioritaire sur `.env`, sans aucune exécution de son texte.

## Limites

Aucune écriture Calendar, aucun envoi Gmail, aucun événement issu des textes automatiquement autorisé. Les données importées ne sont pas substituées dans le scénario de démonstration. Un scénario réel restera incomplet tant que lieux et routes ne sont pas fournis. Tokens et caches restent en mémoire. Les tests utilisent des ports injectés, sans accès aux comptes Google.

## Validation observée

57 tests API passent : 22 tests du parcours HTTP d’autorisation/lecture, 18 tests de configuration sur fichiers temporaires et 17 tests existants de démonstration/plans. Ils couvrent cookies, CSRF, state, rejeu, paramètres dupliqués, expiration, oubli pendant pagination ou acquisition de token, limites de lecture, cache cohérent après erreur et absence de secrets dans les réponses/journaux. Les tests HTTP utilisent le vrai module OAuth avec un transport injecté et des clients de source hors réseau.

14 tests Node hors navigateur passent pour l’interface : lecture uniquement explicite, dates/fuseaux, timestamps Unix, erreurs, cache, états incomplets, oubli indépendant et textes échappés. `node --check apps/web/app.js` et `git diff --check` passent. La vérification navigateur a couvert la configuration absente, la simulation synthétique, des lectures sur ports injectés, l’oubli indépendant, le rétablissement explicite d’une session et l’affichage mobile à 375 pixels sans débordement. Un extrait contenant une balise script est affiché littéralement, sans élément script créé. Le consentement réel reste une validation distincte ; aucun compte Google n’a été consulté pour ces tests.

## Schéma JSON de l’interface

Toutes les erreurs API utilisent `{error:{code,message}}`, avec un message français fixe. Les réponses sont `no-store`. Les routes d’autorisation et de lecture n’acceptent que l’origine canonique `http://127.0.0.1:<port>` et la session du cookie ; elles n’utilisent jamais un identifiant de session fourni dans le JSON.

- `GET /api/session` retourne `{csrf_token,expires_in:3600,canonical_origin}`. Une nouvelle session est créée seulement sans cookie ; un cookie inconnu ou expiré est refusé. Cookie `aeon_session`, opaque, `HttpOnly; SameSite=Lax; Path=/; Max-Age=3600`, sans attribut Domain. Le registre conserve au plus 32 sessions, une heure au maximum, sans prolongation implicite.
- `GET /api/connections` retourne `{canonical_origin,providers:[...],routes:{configured,read_status:"not_requested"},oauth_result:null|{provider,status,code,message}}`. `providers` contient exactement `google_calendar` et `gmail`. Chaque élément a `{id,name,configuration:"ready"|"missing"|"invalid",authorized,scopes,expires_at,read:{status:"never"|"succeeded"|"incomplete"|"failed",last_read_at,count,complete,error:null|{code,message}},data:null|{...}}`. Une autorisation n’est pas une lecture. `oauth_result.status` est `connected` ou `error` ; aucun code Google brut n’y figure.
- `POST /api/oauth/{provider}/begin`, corps `{}`, retourne `{authorization_url,expires_in:300}`. Le navigateur ouvre cette URL de consentement, après l’action explicite de l’utilisateur, dans le même navigateur que l’adresse canonique. Une nouvelle tentative efface le cache de cette source et remplace son autorisation locale précédente.
- `POST /api/oauth/{provider}/forget`, corps `{}`, retourne `{forgotten:true,provider}`. Le client recharge ensuite `/api/connections`. Les autres sources restent intactes.
- `POST /api/calendar/read`, corps `{time_min:"ISO avec offset",time_max:"ISO avec offset",timezone:"Europe/Paris"}`. `timezone` est optionnel et vaut `Europe/Paris`. L’intervalle est positif et limité à sept jours. Réponse `{provider:"google_calendar",data:{synthetic:false,read_at,complete,pages_read,horizon:{start,end},timezone,events:[{id,title,planned_start,planned_end,classification,private,source}],excluded_count,deleted_count}}`. Lecture de `primary`, au plus trois pages ; aucun token de pagination ou de synchronisation n’est retourné au navigateur. Tous les événements importés restent fixes. Le cache est remplacé seulement après une lecture réussie ; une erreur conserve la dernière lecture et la signale.
- `POST /api/gmail/read`, corps `{query:"recherche explicitement saisie"}` ; chaîne non vide de 500 caractères maximum. Réponse `{provider:"gmail",data:{synthetic:false,read_at,complete,pages_read:1,query,messages:[{id,subject,from,date,excerpt,truncated,source}]}}`. Une page de liste, dix messages maximum, puis au plus dix lectures de message ; extrait borné à 1 000 caractères. Aucune interprétation ni création de contrainte. `complete=false` si d’autres messages ou pages existent, ou si un extrait est tronqué.

Les POST ci-dessus requièrent `Content-Type: application/json`, `Origin` exact et `X-Aeon-CSRF`. Le cookie est envoyé par le navigateur. Les champs supplémentaires, JSON dupliqués/non finis et paramètres de callback dupliqués sont refusés. Le callback retourne toujours `303 Location: /` après traitement, sans code dans la réponse ni journal.

## Configuration et démarrage

`scripts/dev` charge une fois les quatre clés décrites dans `.env.example`, depuis l’environnement puis le fichier `.env` du worktree comme valeur de repli. Les clés inconnues ne sont pas transmises aux intégrations. Le parseur accepte les commentaires sur leur propre ligne, `KEY=value` et les valeurs entourées de guillemets identiques ; il n’effectue aucune interpolation, expansion ni exécution. Le fichier est limité à 64 KiB. Une syntaxe incorrecte, une clé répétée, un défaut de lecture ou un UTF-8 invalide désactive la configuration Google ; la démonstration reste utilisable. Aucun détail de configuration n’est inclus dans les erreurs.

`create_server(...)` ne charge jamais implicitement le fichier ni l’environnement : les tests et autres consommateurs reçoivent une configuration vide par défaut. Les objets OAuth et les fabriques Calendar/Gmail sont injectables. Seul `main()` charge la configuration réelle. La redirection doit être exactement `http://127.0.0.1:<port>` ou cette adresse suivie de `/` ; `localhost` reste utilisable pour la démo, mais les routes de connexion exigent l’origine canonique.

Une session inconnue ou expirée produit `401` et efface le cookie local ; un nouveau `GET /api/session` explicite peut ensuite créer une session. Les expirations ne sont pas prolongées par consultation. Les POST ne sont jamais répétés automatiquement par l’interface. Le callback est la seule navigation cross-site admise avec le cookie SameSite=Lax ; les API refusent les origines tierces. Les routes API privées exigent toujours une session connue.

## Données et concurrence

`read_at`, `read.last_read_at` et `expires_at` sont des **secondes Unix**, pas des millisecondes JavaScript. `expires_in` décrit la durée restante de la session, au maximum 3 600 secondes. Une lecture Calendar conserve au plus 500 événements, avec titres limités à 500 caractères. Les exclusions du connecteur, la pagination interrompue, les plafonds et les textes tronqués entraînent `complete=false`. Le champ `private` signifie ici « événement personnel organisé par soi sans participant tiers », et ne décrit pas la visibilité privée/public Google.

Les verrous du registre ne sont jamais conservés pendant les appels Google. Une seule lecture est active par source et session. L’oubli ou une nouvelle connexion change la génération de cette source ; la lecture vérifie cette génération après l’obtention du token, avant chaque nouvel appel API et avant publication du cache. L’appel déjà en vol peut terminer, mais ses résultats ne repeuplent pas la source oubliée. L’oubli peut attendre la fin d’un échange OAuth déjà engagé ; il ne prétend pas annuler une requête réseau ni révoquer Google.

La lecture Gmail effectue au plus onze requêtes connecteur (une liste puis dix messages), et Calendar trois pages au maximum. Chaque transport conserve son timeout ; une série peut donc durer plus longtemps qu’une requête unique. L’interface affiche l’état en cours. Aucun texte Gmail n’est transformé automatiquement en deadline, aucun événement importé n’est déclaré flexible et aucun trajet n’est fabriqué. La présence d’une clé Routes n’atteste aucune connexion réussie.
