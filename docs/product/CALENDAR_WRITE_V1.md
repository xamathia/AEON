# Écriture Calendar conditionnelle v1

Issue36, Astra. Import direct `from packages.aeon_connectors.calendar_write import GoogleCalendarWriter, CalendarWriteError` ; aucun changement des exports existants.

`GoogleCalendarWriter(access_token, *, authorized_event_ids, transport=None, timeout=15)` accepte une collection de 1 à5 IDs uniques explicitement autorisés. Seul `primary` est accessible. `get_event(calendar_id,event_id)` et `patch_times(calendar_id,event_id,*,planned_start,planned_end,if_match,send_updates="none")` retournent exactement `{calendar_id,external_event_id,etag,planned_start,planned_end,private,classification,organizer_self,attendees_count}` compatible PlanExecutor.

Un bloc personnel contrôlé signifie `organizer.self=true`, aucun participant et aucune omission de participants ; cela ne promet pas la visibilité Google `private`. Seuls événements confirmés, temporisés, autonomes et non récurrents sont acceptés. Aucune invitation, occurrence récurrente, journée entière ou événement annulé. La classification flexible vient exclusivement des IDs autorisés et de ces contrôles, jamais du titre.

Chaque PATCH vérifie d’abord le snapshot distant par GET, même sans PlanExecutor. L’ETag doit correspondre exactement à `if_match`, ensuite la précondition HTTP `If-Match` protège contre les changements concurrents. Corps strict `start.dateTime` et `end.dateTime` ; dates avec offset et durée positive validées avant toute I/O. Query `sendUpdates=none` uniquement. La réponse doit confirmer les instants demandés et une nouvelle version.

Port injectable : `request(method,url,*,headers,body,timeout,max_bytes)->(status:int,body:bytes)` ; toute exception peut représenter un envoi partiel. Transport standard HTTPS direct vers www.googleapis.com, aucune redirection, proxy implicite ou relance, réponse bornée1MiB. Timeout socket positif au plus15s, sans garantie globale DNS/réseau. Jeton non présent dans repr ou erreurs.

HTTP412 devient CalendarConflict au message fixe. Après appel PATCH, exception, HTTP5xx/408 ou réponse invalide deviennent CalendarUnknownOutcome au message fixe ; pas de compensation aveugle ni retry. Les autres refus HTTP définitifs deviennent CalendarWriteError fixe. Avant PATCH, panne GET ou entrée invalide ne peut pas produire unknown. PlanExecutor reste propriétaire de l’audit, des permissions et des règles de compensation/undo.

Sources officielles : [events.get](https://developers.google.com/workspace/calendar/api/v3/reference/events/get), [events.patch](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch). Tests exclusivement injectés, incluant execute/undo et ETag concurrent ; aucun token réel ni modification de calendrier. Consentement OAuth écriture et confirmation UI restent dans une intégration séparée.
