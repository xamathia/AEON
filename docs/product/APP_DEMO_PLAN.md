# Application de démonstration ÆON — plan

Objectif : rendre le moteur Sol utilisable depuis une interface locale et exposer clairement données, risques et hypothèses.

Architecture : serveur Python 3.9+ lié à 127.0.0.1, API JSON et assets HTML/CSS/JS locaux. Import tardif du moteur ; réponse 503 explicite s’il manque. Aucun calcul de probabilité dans le frontend. Référence : ENGINE_CONTRACT_V1.md et spécification ÆON approuvée.

## Direction visuelle

Observatoire temporel clair : une grande grille à deux voies compare l’intention et la projection. La bande d’incertitude constitue le geste visuel principal ; les autres éléments restent calmes. Palette : papier #f3f6fb, blanc #ffffff, encre #162f49, bleu #235df3, ambre #a76120, turquoise #137f78. Typographie système Avenir Next / sans-serif pour lecture et grands chiffres ; titres à gauche. Pas de données simulées présentées comme une connexion réelle. Interactions en français ; clavier, focus et réduction du mouvement respectés.

## Étapes

- [x] Contrat API et tests HTTP : moteur absent, vraie réponse injectée, erreurs JSON/tailles/origines, incident non cumulatif, assets limités.
- [x] Serveur : GET /api/status et /api/demo ; POST /api/simulate ; erreurs structurées et serveur local uniquement.
- [x] Interface : grille réel/prédit, probabilités calculées, détails d’événement, incident réversible, sources et hypothèses ; aucun faux résultat si moteur absent.
- [x] Intégration : scripts/dev, scripts/test avec découverte des tests moteur, README actualisé.
- [x] Validation locale : suite API/coordination, syntaxe JS et navigateur desktop/mobile ; revue de code réalisée et PR préparée pour les checks GitHub avant fusion.

Les fichiers de Sol (packages/aeon_engine/ et tests/engine/) sont exclus de cette tâche. La connexion Google, l’extraction LLM, les plans et les écritures Calendar constituent des tranches suivantes ; ce serveur n’expose aucune mutation externe.

Validation observée : navigateur desktop et mobile, état moteur absent (503), puis moteur Sol de la PR #7 dans un checkout de revue séparé via PYTHONPATH. Risque de la fixture 99,6 %, incident 100 %, arrivée médiane 18:13 puis 18:25 ; reset identique. Valeurs calculées, fixture non calibrée. Le contrôle HTTP end-to-end est exécuté automatiquement dès que le moteur est présent.
