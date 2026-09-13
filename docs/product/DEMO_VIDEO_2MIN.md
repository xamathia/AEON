# Conducteur ÆON — 115 secondes

Deux variantes sont proposées : **A, démonstration synthétique réalisable**, et **B, vidéo de remise conditionnelle**. Elles durent chacune 115 secondes, soit cinq secondes de marge sous la limite de deux minutes. Ce document ne constitue ni un enregistrement ni une validation de compte Google.

La version A montre la démo synthétique livrée. Le panneau de prévision réelle permet déjà Calendar + trajets explicites, avec Routes facultatif. La version B exige cependant trois lectures réelles et leur contribution au scénario : Gmail reste à raccorder au calcul. Elle ne peut donc pas être tournée telle quelle aujourd’hui. Une autorisation OAuth, une clé configurée ou un test avec transport injecté ne remplace aucune de ces preuves.

## Préparer la capture

- Suivre le [README](../../README.md) pour démarrer l’application et ouvrir `http://127.0.0.1:8787` dans un navigateur classique. Garder le port cohérent avec la redirection OAuth si les connexions sont utilisées.
- Pour A, afficher la journée de référence au trafic normal et garder le badge **Démo synthétique** visible. Aucun identifiant Google ni accès privé n’est nécessaire.
- Préparer cadrage, taille du texte et déplacements dans la page. Écarter de la capture les identifiants, codes OAuth, tokens et contenus personnels qui ne sont pas destinés à être montrés. Les consentements se font hors enregistrement, séparément par source.
- Lire les chiffres réellement affichés lors de la prise. Les mentions entre crochets ci-dessous sont des emplacements à remplacer par les résultats de cette prise, jamais des pourcentages à coder ou à promettre.
- Répéter avec chronomètre, en incluant les attentes et les clics. Les créneaux sont des budgets de montage ; ils ne garantissent aucune latence de l’application ou de Google. Si un calcul dépasse son créneau, réduire les pauses ou refaire la prise, sans faire passer une ancienne réponse pour le calcul en cours.

## A — Démonstration synthétique réalisable maintenant

Durée totale : **115 secondes**. Montrer uniquement les événements et hypothèses synthétiques de la fixture.

| Temps | Écran et geste réalisables | Voix proposée |
| --- | --- | --- |
| 0–8 s | Titre, badge **Démo synthétique**, calendrier de référence | « Un agenda décrit ce qui est prévu. ÆON explore ce qui peut déborder. Ici, toutes les données sont synthétiques. » |
| 8–23 s | Montrer la réunion, le bloc flexible, le dîner et les signaux ; aucune connexion privée | « La journée combine une réunion, un bloc personnel et un dîner. Une contrainte d’arrivée et une distribution de trajet sont déclarées dans le scénario. Ce sont des hypothèses de démonstration. » |
| 23–42 s | **Analyser ma journée** ; cadrer le Shadow Calendar et le risque du dîner | « ÆON calcule mille futurs sans appel LLM. Voici le risque affiché : [risque]. L’arrivée médiane et la fourchette montrent les conséquences possibles de ces hypothèses. » |
| 42–64 s | **Chercher des alternatives** ; montrer les cartes et le nombre de candidats évalués | « Le moteur recherche des déplacements du seul bloc autorisé. Je compare l’horaire avant et après, le décalage et le risque restant. Les valeurs viennent des simulations, pas d’un texte généré. » |
| 64–84 s | **Prévisualiser** une carte ; montrer le bandeau et le déplacement ; **Fermer la prévisualisation** | « Cette option change le calendrier affiché et son risque projeté. C’est une prévisualisation, sans écriture Calendar. Je ferme : la référence revient immédiatement, sans nouveau calcul. » |
| 84–100 s | **Simuler un incident trafic** ; montrer le risque recalculé et l’invalidation des anciennes propositions | « J’injecte maintenant douze minutes de plus dans les durées de trajet. Le risque est recalculé et les anciennes propositions sont retirées. On peut rechercher de nouvelles alternatives. » |
| 100–115 s | Cadrer les limites de recherche et les mesures de calcul | « La recherche reste bornée, sans garantie d’optimum global. Les probabilités ne sont pas calibrées sur la vie réelle. Ce parcours prouve le calcul et la prévisualisation, pas trois lectures Google réelles. » |

Si aucune amélioration n’est trouvée dans la prise, montrer cet état et le dire. Ne pas remplacer ce résultat par une carte inventée. Pour le parcours avec prévisualisation, refaire une prise sur la fixture canonique disponible, après avoir vérifié qu’elle produit effectivement des options.

## B — Version de remise, conditionnée aux trois sources réelles

Cette variante reste **à valider avant enregistrement**. Les deux consentements OAuth peuvent être obtenus séparément ; ils ne prouvent pas une lecture. Le module d’actions est livré, mais il n’existe aucun parcours applicatif d’exécution à montrer.

Tous les prérequis suivants doivent être satisfaits :

1. Calendar et Gmail sont autorisés dans le même navigateur que l’application, avec les consentements de l’utilisateur concerné. Une lecture Calendar complète, non vide et de 100 événements maximum ainsi qu’une recherche Gmail ont effectivement réussi, sur des données choisies pour la démonstration. Leurs limites sont connues.
2. Une requête **Routes réelle** a réussi avec la configuration du projet adaptée, deux adresses explicitement fournies, un départ futur et l’autorisation nécessaire. Le bouton existe ; une clé présente, une API activée ou une réponse injectée en test ne prouve aucune lecture réelle. Ce conducteur n’atteste pas qu’une requête Routes a eu lieu.
3. Le scénario associe réellement les trois sources avec leurs provenances. Calendar et les trajets sont déjà assemblés par l’application ; **Gmail ne fournit encore aucune contrainte au calcul**. Sa contribution doit être implémentée et vérifiée avant cette version de remise. Un extrait lu ne vaut pas une deadline automatiquement comprise, ni une autorisation d’action.
4. La prévision montrée utilise ce scénario réel, sans substituer la fixture. Les lieux, trajets et contraintes manquants sont signalés ; si le scénario reste incomplet, aucun résultat n’est inventé. Les distributions restent des hypothèses non calibrées.
5. Une répétition complète tient dans 115 secondes. Le conducteur ci-dessous ne montre aucun plan réel : le planner et la prévisualisation restent limités à la démo. Pour ajouter des alternatives réelles à la vidéo, il faut d’abord livrer et vérifier ce parcours, ses autorisations et ses résultats, puis remplacer des créneaux sans dépasser 115 secondes.

Durée cible une fois ces prérequis validés : **115 secondes**. Aucun consentement, secret ni attente de configuration Google n’est filmé.

| Temps | Présentation conditionnelle | Voix utilisable après validation |
| --- | --- | --- |
| 0–8 s | Présenter le problème et le scénario réel choisi | « ÆON explore la fragilité d’une journée à partir de trois sources autorisées. Voici le scénario utilisé pour cette démonstration. » |
| 8–28 s | Montrer les lectures Calendar, Gmail et le trajet Routes effectivement obtenus, avec provenance et état | « Ces événements ont été lus dans Calendar, cet extrait dans Gmail et ce trajet obtenu avec Routes. Les lectures ont réussi. Leurs limites restent visibles. » |
| 28–45 s | Montrer les paires, départs et hypothèses ; contribution Gmail au scénario à livrer avant cette prise | « Les trajets relient les événements consécutifs, à partir des lieux fournis. La contribution du message est explicite et vérifiée. Il n’autorise aucune action. Les incertitudes sont déclarées. » |
| 45–67 s | **Calculer la prévision réelle** sur ce scénario et cadrer le tableau | « Le calcul explore mille futurs sans appel LLM. Le risque obtenu est [risque réel affiché], sous ces hypothèses. La fourchette décrit leur incertitude, pas une garantie d’arrivée. » |
| 67–91 s | Examiner un événement, son début prévu, son arrivée médiane, p10–p90 et les hypothèses | « Pour [événement], le début prévu est [horaire] et l’arrivée médiane [horaire calculé]. L’intervalle affiché vient de la simulation. Les rendez-vous restent fixes ; aucune alternative réelle n’est proposée ici. » |
| 91–107 s | Montrer le budget Routes, puis **Effacer trajets et prévision** | « J’efface les trajets et le résultat local. L’import Calendar reste disponible. Le budget Routes ne repart pas à zéro. Aucun agenda Google n’est modifié. » |
| 107–115 s | Conclure sur la provenance et les limites | « Trois lectures réelles et un calcul explicable. Les probabilités restent dépendantes des hypothèses ; aucun message n’est envoyé. » |

Si un prérequis manque, conserver A comme démonstration technique et annoncer clairement la limite de remise. Le panneau réel peut aussi être montré en nommant ses seules entrées effectives, Calendar et trajets, sans le présenter comme une intégration des trois sources. Ne pas renommer une fixture « réelle », utiliser le seul statut **Autorisé** comme preuve de lecture, ni recycler les risques synthétiques dans B.

## Relecture avant export

- Vidéo mesurée ≤115 secondes, générique et transitions compris ; la limite absolue de remise est de 120 secondes.
- Sources synthétiques et observations réelles identifiables ; chiffres lisibles et issus du scénario effectivement montré.
- Aucune promesse de coût nul, d’économie monétaire ou de minutes gagnées sans mesure correspondante. Les millisecondes de calcul ne sont pas le coût complet du service.
- Aucune écriture Calendar, annulation exécutée ou interprétation LLM revendiquée dans le parcours livré. **Oublier** une source signifie effacement local, pas révocation Google.
- Conditions de calibration, recherche bornée et différence entre prévisualiser et écrire conservées dans la narration finale.

Références : [prévision réelle](APP_LIVE_V1.md), [connexions locales](APP_AUTH_V1.md), [OAuth par source](GOOGLE_AUTH_V1.md), [API de démonstration et plans](APP_API_V1.md), [contrat moteur](ENGINE_CONTRACT_V1.md), [spécification produit](AEON_SPECIFICATIONS_COMPLETES.md).
