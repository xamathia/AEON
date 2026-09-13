# Routage ouvert OSRM v1

Ce connecteur fournit à la démonstration ÆON un itinéraire routier sans clé et sans facturation Google. Il utilise les services publics Nominatim d’OpenStreetMap et OSRM de FOSSGIS avec une charge légère. Ce n’est ni un service avec SLA, ni une promesse de gratuité illimitée pour un produit d’entreprise.

## Interface

```python
from packages.aeon_connectors.open_routing import OpenRoutingClient

client = OpenRoutingClient(transport=None, clock=None, sleeper=None)
observation = client.compute_route(origin_address, destination_address)
```

Les deux arguments sont des adresses saisies explicitement par l’utilisateur : chaînes UTF-8 de 1 à 500 caractères après validation. Une URL, un caractère de contrôle, une chaîne vide ou une autre forme est refusé avant tout réseau. Aucun lieu n’est déduit d’un titre Calendar et aucun trajet inverse n’est créé.

Le résultat a exactement la forme utile au scénario canonique :

```json
{
  "duration_minutes": {"min": 8.0, "mode": 10.0, "max": 14.0},
  "observed_at": "2026-01-01T12:00:00Z",
  "origin_label": "correspondance Nominatim à vérifier",
  "destination_label": "correspondance Nominatim à vérifier",
  "source": {
    "provider": "osrm",
    "reference": "osrm:<empreinte opaque>",
    "synthetic": false,
    "assumption": "itinéraire OSRM sans trafic en direct; distribution triangulaire ÆON non calibrée (0.8/1/1.4); correspondances Nominatim à vérifier"
  }
}
```

La durée OSRM est une observation ponctuelle sans trafic en direct. ÆON en dérive honnêtement un prior triangulaire non calibré : minimum `0.8 × durée`, mode `durée`, maximum `1.4 × durée`, en minutes. Les coordonnées et adresses ne figurent pas dans la référence opaque ni dans les erreurs publiques.

## Requêtes et limites

Pour chaque adresse absente du cache local, le client appelle uniquement :

`GET https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=<adresse>`

Il appelle ensuite uniquement :

`GET https://routing.openstreetmap.de/routed-car/route/v1/driving/<lon>,<lat>;<lon>,<lat>?overview=false&steps=false`

Un appel métier effectue au plus deux géocodages et une route. Le cache géocodage FIFO contient au plus 64 adresses choisies, reste en mémoire dans l’instance et n’est ni persisté ni journalisé. Il ne sert pas à l’autocomplete, au batch ou au scraping.

Chaque requête est HTTPS, sans redirection ni retry, avec un timeout socket de 15 secondes et une réponse limitée à 1 MiB. Tous les clients du processus partagent une cadence maximale d’un départ réseau par seconde et par domaine. Le verrou ne couvre que la réservation d’un créneau ; l’attente a lieu hors verrou. Le User-Agent exact est `AEON-Hackathon/1.0 (+https://github.com/xamathia/hackathon-AEON)`.

Une réponse HTTP en erreur, vide, trop grande, non UTF-8, JSON malformée, non finie, sans correspondance/route, ou contenant une coordonnée/durée invalide produit `OpenRoutingError` avec un message fixe. Aucun corps distant, libellé ou adresse utilisateur n’est réfléchi.

## Provenance et compatibilité

Le provider canonique `osrm` est accepté par le moteur et le planner en plus de `google_calendar`, `gmail`, `google_routes` et `user`. `google_routes` n’est ni renommé ni supprimé. Une source OSRM reste orientée de l’origine vers la destination et conserve `synthetic: false` et son hypothèse dans les simulations et propositions.

## Attribution et usage public

Les correspondances géocodées doivent être présentées comme des résultats à vérifier. L’interface d’intégration doit afficher clairement :

- © OpenStreetMap contributors, données sous ODbL, avec lien d’attribution ;
- routage OSRM fourni par FOSSGIS ;
- un lien visible « fix the map ».

Politiques et documentation : [Nominatim Usage Policy](https://operations.osmfoundation.org/policies/nominatim/), [routing.openstreetmap.de](https://routing.openstreetmap.de/about.html), [OSRM Route service](https://project-osrm.org/docs/v5.24.0/api/#route-service).
