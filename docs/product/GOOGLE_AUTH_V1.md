# Autorisation Google locale — contrat v1

Cette tranche prépare un module OAuth pour l’application locale. Elle ne connecte pas encore de compte Google : le branchement du callback HTTP et de l’interface appartient à la tâche d’intégration suivante. Aucune clé ni identité réelle n’est incluse.

## Flux retenu

Client OAuth Google de type Desktop, redirection vers `http://127.0.0.1:8787/` et ouverture du consentement dans un navigateur système. Le module crée un état aléatoire lié à la session locale et un challenge PKCE S256, puis échange le code côté serveur. Les tentatives expirent après cinq minutes et sont consommées une seule fois. Les tokens restent en mémoire ; redémarrer le serveur nécessite une nouvelle connexion.

Chaque source possède son instance `GoogleOAuth` et son consentement séparé : `provider="google_calendar"` demande uniquement `calendar.events.readonly`, et `provider="gmail"` demande uniquement `gmail.readonly`. Le consentement utilisateur Google est requis. L’autorisation de développer automatiquement entre IA ne donne pas accès à un compte Google et ne permet pas d’écrire dans un calendrier.

## Plan d’implémentation

1. Publier ce contrat et la draft PR ; réserver `packages/aeon_oauth/`, `tests/oauth/`, ce document, `.env.example` et `scripts/test`.
2. Écrire les tests de state, PKCE, scopes, renouvellement, isolation et erreurs de transport, puis implémenter le module stdlib.
3. Ajouter les variables documentées et la découverte des tests au runner existant.
4. Revue indépendante, suites complètes et fusion automatique après CI. Aucun test live annoncé avant autorisation réellement effectuée.

La signature des fonctions et les critères d’acceptation figurent dans l’Issue #16. Les autres clients Google de Sol demeurent dans leur propre paquet, sans modification dans cette tâche.

## Références vérifiées le 13 septembre 2026

- [OAuth pour applications Desktop](https://developers.google.com/identity/protocols/oauth2/native-app) : PKCE, état et échange/refresh côté serveur.
- [Scopes Gmail](https://developers.google.com/workspace/gmail/api/auth/scopes) : lecture des corps via `gmail.readonly`.
- [Scopes Calendar](https://developers.google.com/workspace/calendar/api/auth) : lecture des événements via `calendar.events.readonly`.

## Préparer la configuration de test

Dans votre projet Google, activer Calendar API, Gmail API et Routes API. Configurer l’écran de consentement et ajouter les comptes de démonstration aux utilisateurs de test si l’application est en mode test. Créer un client OAuth de type **Desktop**. Conserver son client ID et, s’il est fourni, son client secret dans la configuration locale. Utiliser un navigateur système pour le consentement ; les vues intégrées peuvent être refusées par Google.

Le fichier `.env.example` décrit les variables prévues. Les valeurs réelles vont uniquement dans un fichier `.env` local ignoré par Git ou dans l’environnement du processus. **Le serveur de démonstration ne charge pas encore ces variables** : cette PR fournit le module d’autorisation, et la tâche d’intégration raccordera la configuration, les routes HTTP et les boutons. Ne pas confondre préparation de configuration et connexion effectuée.

| Variable | Usage |
| --- | --- |
| `AEON_GOOGLE_CLIENT_ID` | Identifiant du client Desktop Google. |
| `AEON_GOOGLE_CLIENT_SECRET` | Secret éventuel transmis uniquement au endpoint token. |
| `AEON_GOOGLE_REDIRECT_URI` | Boucle locale, par défaut `http://127.0.0.1:8787/` ; doit correspondre au serveur qui recevra le code. |
| `AEON_GOOGLE_ROUTES_API_KEY` | Clé serveur pour Routes API, distincte des tokens OAuth. |

La clé Routes est fournie au connecteur Routes côté serveur. Elle n’apparaît jamais dans le JavaScript, les URLs de navigation ou les rapports. Configurer ses restrictions adaptées au projet ; cette tâche ne crée pas de clé ni n’active de facturation.

## Branchement attendu par l’application

Créer une instance `GoogleOAuth` par source avec le même client Desktop et une redirection locale, en passant explicitement `provider`. Le module n’ouvre pas de navigateur et ne décide pas des connexions de l’utilisateur. Le serveur attribuera un identifiant de session cryptographique dans un cookie HttpOnly, SameSite=Lax, puis transmettra cette même session à `begin`, au callback et aux appels internes.

`begin(session_id)` retourne l’URL de consentement et la durée de validité de la tentative. Le callback doit retrouver la source ayant initié la tentative, vérifier les paramètres uniques, puis appeler `complete` avec la session du cookie et le state reçu. Les codes d’autorisation restent côté serveur. Ne pas accepter une session fournie librement dans la query, ne pas renvoyer un token dans une réponse navigateur, et ne pas journaliser la query du callback.

`status` et la réponse de `complete` décrivent **l’autorisation OAuth**, sans effectuer d’appel Calendar/Gmail. Une application doit distinguer cette autorisation d’une lecture API réussie. `access_token` est réservé au branchement interne du connecteur ; il renouvelle un token arrivant à expiration lorsque possible. `forget` efface la session locale et ses tokens ; il ne prétend pas révoquer le consentement Google. La révocation Google et les écritures Calendar restent des tranches séparées.

## Limites de cette tranche

Tokens uniquement en mémoire, sans persistance ni coffre de production ; la connexion doit être renouvelée après redémarrage. Le module est destiné à une seule machine et borne le nombre de sessions à 32 par source. Aucune API ne reçoit ici de texte Gmail, ni de données Calendar. Les tests sont hors réseau avec réponses injectées : ils vérifient les contrôles et le protocole, pas l’activation de votre projet Google.

## Validation observée

44 tests OAuth hors réseau passent sur Python 3.9.6 : 28 cas de session/jeton et 16 cas de transport. Deux revues indépendantes ne relèvent plus de point bloquant. Une régression de concurrence vérifie que deux appels attendant le même renouvellement partagent aussi son échec transitoire, tandis qu’un appel ultérieur peut réessayer. Les tracebacks formatés sont contrôlés pour empêcher la fuite des messages externes.

Le runner commun découvre désormais cette suite lorsqu’il trouve `packages/aeon_oauth/`. Aucune connexion de compte Google ni lecture API réelle n’a été effectuée pour ces validations.
