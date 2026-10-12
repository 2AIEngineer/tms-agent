# tms-agent — Orchestrateur de suivi des missions

Agent IA qui surveille en continu des missions de transport à travers n'importe
quel TMS : détection déterministe par règles métier YAML, enquête et explication
par LLM, escalade vers les bons niveaux de management.

Les règles décident, le LLM explique. La détection ne dépend jamais du LLM :
sans lui, l'agent continue d'alerter, seule l'analyse est sautée.

Documentation : [specifications.md](docs/specifications.md) ·
[architecture](docs/arch_best_practics.md) ·
[infrastructure](docs/architecture_infra.md) · [structure](docs/structure.md)

---

## Démarrer

```bash
uv sync                      # dépendances Python
scripts/infra.sh up          # infra locale (voir infra/README.md)
scripts/infra.sh check       # vérifier que tout répond
scripts/test.sh              # suite de tests (unitaires + intégration)
scripts/test.sh -m "not integration"   # sans PostgreSQL
```

Les tests marqués `live_tms` s'exécutent contre le TMS simulé s'il est en
écoute, et sont ignorés sinon :

```bash
cd x_tms/server
uv run --project . python -m uvicorn tms.api.app:app --port 8000
```

Les tests d'intégration tournent sur `tms_agent_test_db`, créée et migrée par
`scripts/infra.sh up`. Leurs tables sont vidées entre chaque test : jamais la
base de développement.

`scripts/test.sh` plutôt que `pytest` directement : l'installation ROS de ce
poste injecte ses plugins pytest via le `PYTHONPATH` global, et le script les
isole.

### Lancer la chaîne complète en local

Quatre processus, dans quatre terminaux. Les deux premiers appartiennent au TMS
simulé, les deux suivants à l'orchestrateur.

```bash
# 1. l'API du TMS simulé (doc interactive : http://127.0.0.1:8000/v1/docs)
scripts/tms.sh serve

# 2. l'interface du TMS, qui affiche les camions en temps réel
#    Vite écoute sur 5173, ou le port suivant s'il est occupé : lire l'URL affichée
scripts/tms.sh client

# 3. le serveur MCP : seule porte vers le TMS
uv run python -m services.mcp_server.server

# 4. le worker de surveillance : détection + expédition des notifications
uv run python -m services.monitoring.main

# 5. le service agent : enquêtes IA sur les alertes (facultatif)
uv run python -m services.agents.main

# 6. l'API d'exploitation : alertes, assistant, flux temps réel (facultatif)
uv run python -m services.ops_api.main
```

Les deux derniers sont facultatifs : la détection et les notifications
fonctionnent sans eux.

Puis, dans un terminal de plus, démarrer une mission — sans quoi le TMS est
vide et il n'y a rien à surveiller :

```bash
scripts/tms.sh demo deviation      # une anomalie, mouvement observable (x3)
scripts/tms.sh demo all            # les quatre missions en parallèle (x10)
```

`demo all` remet la base à zéro, lance les quatre scénarios ensemble et annonce
ce qu'il faut guetter :

```
==> Alertes attendues (T = création de la mission, horloge simulée)
  T+63   deviation_persistent     écart > 5 km pendant 20 min        (~2 min réelles)
  T+55   critical_incident        message critique du chauffeur      (~2 min réelles)
  T+162  immobilization_anomaly   véhicule arrêté, aucun événement…  (~12 min réelles)
 warn Démarrer le worker de surveillance *avant* ces échéances : les fenêtres sont étroites
  l'écart se referme vers T+75 : l'alerte passera d'elle-même en 'resolved' (clôture automatique)
```

Un seul saut d'horloge pour quatre missions : elle est commune à la simulation,
et les anomalies tombent à T+45, T+55 et T+60. Sauter 40 minutes place le départ
juste avant la première sans franchir les suivantes — le saut de 150 minutes du
scénario d'immobilisation seul aurait fait manquer les deux autres, déjà
passées. La vitesse passe à x10 parce que la dernière alerte attendue, à T+180,
demanderait sinon trois quarts d'heure de temps réel ; `--speed` la remplace.

Le worker tient un verrou consultatif PostgreSQL : une seule instance surveille
à la fois, et le verrou se libère de lui-même si le processus meurt.

> `uv run tms serve`, indiqué dans le README de `x_tms`, ne fonctionne pas :
> son `pyproject.toml` ne déclare pas de `[project.scripts]`, donc la commande
> `tms` n'est pas installée. `scripts/tms.sh` passe par `python -m tms.cli` et
> `python -m uvicorn`, qui fonctionnent.

---

## Faire vivre des missions dans le TMS simulé

Tout passe par `scripts/tms.sh`, qui évite de changer de répertoire et de
retenir l'invocation exacte :

```bash
scripts/tms.sh status              # ce que contient le TMS, maintenant
scripts/tms.sh reset               # vide missions, événements, positions, horloge
scripts/tms.sh serve               # API du TMS (port 8000)
scripts/tms.sh client              # interface web
scripts/tms.sh scenarios           # liste les scénarios
scripts/tms.sh start [options…]    # démarre des missions (options transmises)
scripts/tms.sh demo <scénario>     # reset + départ juste avant l'anomalie
```

Le chemin de `x_tms` est réglable par `X_TMS_PATH`, pour le jour où il sera
extrait dans son propre dépôt.

### Commencer par un TMS vide

**Un TMS au repos ne doit rien exposer.** Si `status` montre des missions sans
qu'aucune simulation ne tourne, ce sont des restes d'une exécution précédente,
et ils se mélangeront aux missions que vous démarrerez ensuite :

```bash
scripts/tms.sh reset
scripts/tms.sh status
#   missions         : 0  (aucune)
#   heure du TMS     : …  (écart réel : -0 min)
```

Deux restes possibles, que `status` signale explicitement :

| Symptôme | Cause | Effet sur l'orchestrateur |
| :--- | :--- | :--- |
| mission `in_progress` sans simulation active | simulation tuée au lieu d'être interrompue par Ctrl+C | une mission figée est surveillée indéfiniment |
| horloge du TMS en avance sur le temps réel | **l'heure simulée ne recule jamais** : après une exécution accélérée, elle reste là où la simulation l'a laissée | tous les âges et ETA sont faux ; nous avons déjà vu un « retard » de 4 287 minutes |

`reset` corrige les deux : il supprime missions, événements, positions et
itinéraires, remet les véhicules à `idle` et **supprime l'horloge simulée**,
ce qui la ramène à l'heure réelle. Utilisateurs et véhicules sont conservés.

> `reset` refuse de s'exécuter si une simulation tourne encore : la remise à
> zéro supprimerait ses missions sous ses pieds, elle planterait, et en sortant
> elle réancrerait l'horloge en avance — soit une base vide *avec* une horloge
> faussée. Utiliser `reset -f` pour l'interrompre proprement d'abord, ou Ctrl+C
> dans son terminal.

### Démarrer, puis vérifier

`start` transmet ses options au simulateur. Les commandes ci-dessous se lancent
depuis la racine du projet.

### Les quatre scénarios

| Scénario | Trajet | Distance | Transit | Anomalie provoquée |
| :--- | :--- | ---: | ---: | :--- |
| `nominal` | Casablanca → Rabat | 87 km | 1 h 10 | aucune |
| `deviation` | Casablanca → Marrakech | 229 km | 3 h 05 | écart jusqu'à 7,5 km de T+45 à T+75 |
| `immobilization` | Tanger → Fès | 248 km | 3 h 20 | véhicule à l'arrêt de T+60 à T+240 |
| `incident` | Casablanca → El Jadida | 86 km | 1 h 10 | message chauffeur critique à T+55 |

`nominal`, `deviation` et `immobilization` sont lancés par défaut ; `incident`
doit être demandé explicitement.

**T+0 est le début de la mission, pas le départ du camion.** Le camion rejoint
le point de chargement (10 min), charge (32 min), puis part : le transit ne
commence qu'à **T+42**. C'est ce décalage qui rend `--skip-minutes` nécessaire
pour observer une anomalie sans attendre.

### Une seule mission

```bash
scripts/tms.sh start -s deviation
```

### Toutes les missions en simultané

```bash
# les trois scénarios par défaut
scripts/tms.sh start

# les quatre, en repartant d'une base propre
scripts/tms.sh start -s nominal -s deviation -s immobilization -s incident --reset
```

### En temps réel, pour voir les camions bouger

`--speed 1` est le temps réel, `--tick-seconds` l'intervalle entre deux points
GPS. L'interface interroge l'API toutes les 2 secondes : une position toutes
les 5 secondes donne un mouvement fluide.

```bash
scripts/tms.sh start -s nominal --speed 1 --tick-seconds 5
```

Au temps réel, une mission dure plusieurs heures. Pour un mouvement visible
mais une démonstration de quelques minutes, accélérer modérément :

```bash
# 1 minute simulée ≈ 5 secondes réelles : les camions avancent à vue d'œil
scripts/tms.sh start --speed 12 --tick-seconds 10
```

### Déclencher une anomalie sans attendre

`--skip-minutes N` rejoue instantanément les N premières minutes, puis continue
en temps simulé. Les valeurs ci-dessous amènent juste avant le déclenchement de
la règle correspondante :

`demo` fait le reset, cale le saut et démarre, pour les quatre scénarios :

```bash
scripts/tms.sh demo deviation        # écart dès T+45, règle à 5 km pendant 20 min
scripts/tms.sh demo immobilization   # arrêt dès T+60, règle à 2 h sans événement
scripts/tms.sh demo incident         # message critique à T+55, escalade jusqu'à la direction
scripts/tms.sh demo nominal          # aucune anomalie : sert de témoin
scripts/tms.sh demo all              # les quatre en parallèle, x10 (voir plus haut)
```

Les instants sont **mesurés** sur des runs complets, non déduits des seuils :
l'écart met quelques minutes à franchir la tolérance, et l'immobilisation
déclenche à T+162 et non T+180 — la règle compte l'âge du dernier *événement de
mission*, et le dernier est le départ du chargement à T+42, le transit n'en
émettant aucun.

Chaque `demo` annonce les alertes attendues, leur instant simulé et le délai
réel correspondant à la vitesse choisie — une démonstration où l'on ne sait pas
quoi guetter ressemble à une démonstration qui ne marche pas. Les options sont
transmises à x_tms et ne sont **pas** doublées : `demo all --speed 30` remplace
la vitesse par défaut au lieu de s'y ajouter.

`demo deviation` est vérifiée de bout en bout : la dérive atteint 7,96 km, la
règle déclenche une alerte `high`, et le chauffeur (appel), l'agent de suivi
(in-app) et le superviseur (SMS) sont notifiés.

L'équivalent explicite, si vous voulez régler le saut vous-même :

```bash
scripts/tms.sh start -s deviation --speed 3 --tick-seconds 10 --skip-minutes 50 --reset
```

### Autres options utiles

| Option | Effet |
| :--- | :--- |
| `--reset` | supprime les missions existantes avant de démarrer |
| `--fast-forward` | joue toute la simulation instantanément (historique complet, aucun mouvement à observer) |
| `--routing osrm` | itinéraires sur routes réelles au lieu du tracé statique (nécessite un accès à OSRM) |
| `--seed N` | simulation reproductible |
| `-v` | affiche la progression de chaque mission |

```bash
scripts/tms.sh scenarios   # liste les scénarios
scripts/tms.sh status      # état courant, et avertissements sur les restes
```

### Qualité

```bash
uv run ruff check . && uv run ruff format .
env -u PYTHONPATH uv run mypy shared/ services/
```

---

## Brancher un TMS

Le connecteur définit **ce qu'il sait lire** — `list_missions`, `get_route`,
`deviation.current_offset_km`. Un **dialecte** répond, pour un TMS donné, à une
seule question posée champ par champ : *« quelle est la correspondance de cela
chez lui ? »*

Aucun chemin d'URL ni aucun nom de champ du TMS n'apparaît dans le code. Un TMS
qui expose `/resources/transports/{id}/way` et nomme sa mission `mss_id` se
branche en écrivant son dialecte, sans ligne de Python.

```bash
uv run python -m services.mcp_server.cli list                   # dialectes livrés
uv run python -m services.mcp_server.cli validate mon_tms.yaml  # pendant l'écriture
uv run python -m services.mcp_server.cli report fleetlink       # rapport de capacité
```

### Ce qui est déclaratif, et ce qui ne l'est pas

| Relève de la **donnée** → dialecte | Relève de la **logique** → `TmsProfile` |
| :--- | :--- |
| chemins d'endpoints et paramètres | calculer un écart que le TMS ne fournit pas |
| noms et imbrication des champs | réduire une tournée multi-arrêts à une origine et une destination |
| valeurs d'énumération (plusieurs des leurs → une des nôtres) | signer une requête, rafraîchir un jeton |
| style de pagination, enveloppes de réponse | normaliser une réponse hors de toute norme |
| mode d'authentification | |

Pour un TMS conforme au contrat, le profil est vide et le dialecte suffit. La
trappe de code existe parce qu'une config capable de tout ce qui est à droite
serait un langage de programmation sans typage ni tests, dont les erreurs
n'apparaîtraient qu'en production.

### Le dialecte est validé au chargement

Une opération sans correspondance, un champ obligatoire non mappé, un statut de
mission dont la traduction manque : la connexion est **refusée**, avec le nom de
la clé à corriger. Même principe que les règles métier, refusées au dépôt du
fichier plutôt que découvertes au milieu d'un cycle.

### Le rapport de capacité

Quand un TMS ne sait pas rendre une information, l'absence d'alerte ressemble en
tout point à une flotte sans anomalie. Le rapport empêche cette confusion :

```
Rapport de capacité — FleetLink (TMS fictif de validation) (fleetlink)
Verdict : dégradé

  [NON] Détecter un écart à l'itinéraire  (manque : get_deviation)

Règles qui ne déclencheront jamais sur ce TMS :
  - deviation_persistent (high) : champs indisponibles deviation.current_offset_km, …

À demander à l'éditeur du TMS :
  - Détecter un écart à l'itinéraire — règles concernées : deviation_persistent
```

La dernière section est formulée en besoin métier et non en nom d'endpoint :
c'est le document à mettre sur la table lors d'un raccordement.

### Deux dialectes livrés

| Dialecte | Rôle |
| :--- | :--- |
| `x_tms` | TMS de référence. Ses noms coïncident avec les nôtres, le pivot ayant été dessiné en le regardant. |
| `fleetlink` | TMS fictif pris en contre-pied sur tout : préfixe d'URL, noms d'endpoints, noms de champs, imbrication, enveloppes, pagination par numéro de page, authentification par en-tête, statuts en majuscules — et une ressource absente. |

`fleetlink` n'existe que pour éprouver le mécanisme : les mêmes situations
métier y sont rejouées, sans qu'une ligne de code ait été écrite pour lui. Un
test compare même les deux pivots produits, pour que le reste du système ne
puisse pas distinguer les deux TMS.

### Raccorder plusieurs TMS

Les connexions se déclarent dans un fichier, désigné par `TMS_CONNECTIONS_FILE` :

```yaml
connections:
  - tms_id: client_a
    dialect: x_tms
    base_url: http://tms-a.interne/v1
  - tms_id: client_b
    dialect: x_tms           # même logiciel, autre client
    base_url: http://tms-b.interne/v1
  - tms_id: acme
    dialect: fleetlink
    base_url: https://api.acme-tms.com/v2
    api_key_env: ACME_TMS_KEY
    max_requests_per_second: 5   # quota propre à ce TMS
```

Sans ce fichier, la connexion unique est décrite par `TMS_ID`, `TMS_DIALECT` et
`TMS_BASE_URL` — le cas du POC, où `tms_id` reste facultatif partout.

Tous les outils MCP exigent un `tms_id`, **même lorsqu'un seul TMS est
raccordé** : il n'y a jamais de TMS implicite. Un appelant habitué à ne pas
nommer sa cible deviendrait un défaut le jour du second raccordement, et ce
défaut-là est silencieux — il surveillerait un client et pas l'autre.

Son corollaire est l'outil `list_tms`, qui donne les TMS valides et leurs
capacités. Le worker s'en sert pour lancer une boucle par TMS ; il ne lit pas la
configuration des connexions, qui appartient au serveur MCP. Ajouter un TMS ne
demande donc pas de redéployer le worker, seulement de le redémarrer.

#### Pourquoi `tms_id` est une dimension et non une étiquette

`mission_id` et `event_id` viennent du TMS : ils ne sont uniques que **dans** ce
TMS. Deux défauts de correction en découlaient, et les deux étaient silencieux —
l'absence d'alerte ressemble en tout point à une flotte sans anomalie :

| Mécanisme | Avant | Après |
| :--- | :--- | :--- |
| Déduplication des alertes | index unique sur `(deduplication_key)` → l'alerte du second TMS supprimée comme doublon | `(tms_id, deduplication_key)` |
| Idempotence des événements | clé primaire sur `(event_id)` → l'événement du second TMS pris pour déjà traité | `(tms_id, event_id)` |

La provenance est une **colonne**, pas un préfixe à ajouter dans les gabarits de
règles : les règles livrées n'ont pas changé d'une ligne.

Le verrou consultatif porte lui aussi sur `tms_id:clé`, pour que deux TMS
n'attendent pas l'un l'autre et que la portée du verrou coïncide avec celle de
l'unicité. Les règles peuvent viser un TMS précis via `tms.id`, ce qui permet un
seuil par client sans dupliquer le fichier de règle.

#### Comment le worker tient 20 TMS

**L'unité de travail est le TMS, pas le cycle.** Le worker lance une boucle de
surveillance indépendante par TMS, toutes en parallèle dans le même processus —
elles attendent le réseau, elles ne calculent pas.

En série, un cycle ne tiendrait pas : avec cent missions par TMS et six lectures
simultanées, un TMS coûte quelques secondes, et vingt TMS les additionneraient
bien au-delà de l'intervalle de dix secondes. Une alerte critique sur le
vingtième client arriverait en retard par la seule faute des dix-neuf premiers.

Une boucle par TMS donne trois choses qu'un cycle global ne donne pas :

| Propriété | Conséquence |
| :--- | :--- |
| backoff par TMS | un client en panne recule seul, sans ralentir les autres (F5) |
| battement de cœur par TMS (`last_cycle:<tms_id>`) | un client silencieux est détectable même quand les autres tournent |
| verrou par TMS | c'est le mécanisme de répartition entre instances |

#### Répartition entre instances, sans coordinateur

Chaque TMS est protégé par son propre verrou consultatif PostgreSQL. Un worker
tente d'en prendre jusqu'à `MAX_TMS_PER_WORKER` et surveille ce qu'il obtient.
**Le plafond est ce qui transforme le verrou en répartiteur** — sans lui, la
première instance prendrait tout.

```
# instance 1, plafond 1
TMS client_nord pris en charge (verrou 870981991)
Plafond atteint (1 TMS) : client_sud laissé à une autre instance

# instance 2, plafond 1
TMS client_nord déjà surveillé par une autre instance
TMS client_sud pris en charge (verrou 187901774)
```

Les verrous étant tenus par la connexion, un worker qui meurt les libère et une
autre instance reprend ses TMS :

```
# instance 3, lancée après la mort de l'instance 1
TMS client_nord pris en charge (verrou 870981991)
TMS client_sud déjà surveillé par une autre instance
```

Aucun registre d'affectation, aucun coordinateur. Pour passer de un à vingt TMS,
on ajoute des réplicas et on règle le plafond — le code ne change pas.

**Limite connue :** un worker en cours d'exécution ne rebalaye pas. Un TMS
orphelin est repris au démarrage d'un réplica, ce que la plateforme automatise
en redémarrant un conteneur mort ; en revanche, un TMS **nouvellement ajouté**
aux connexions n'est pris en charge qu'après redémarrage d'un worker. Un
balayage périodique lèverait les deux cas.

**Un conteneur par TMS** serait du gaspillage pour vingt clients — vingt
applications à déployer et à payer à l'arrêt — mais reste possible pour isoler
un client exigeant : lancer le worker avec `MONITORING_TMS_ALLOWLIST=ce_client`.

L'expédition des notifications reste une boucle unique pour tous les TMS : la
file est commune et chaque demande porte sa provenance.

#### Deux clients sur le même logiciel de TMS

C'est le piège le moins visible : deux connexions peuvent partager un dialecte
et doivent pourtant avoir des identités distinctes. La provenance d'un
instantané vient donc de la **connexion** et non du dialecte — la dériver du
dialecte étiquetterait les deux clients de la même façon, et la déduplication
les confondrait de nouveau.

---

## L'enquête IA

Les règles décident, le LLM explique. Une enquête qui échoue laisse l'alerte
intacte et notifiée : seule l'explication manque.

```bash
uv run python -m services.agents.main     # consommateur d'enquêtes
```

Ce service n'a **pas besoin de verrou** : réclamer une enquête est une
insertion `ON CONFLICT (alert_id) DO NOTHING`, donc atomique. Plusieurs
instances se répartissent le travail sans configuration ni coordinateur.

Il n'y a pas non plus de table de file d'attente : la file *est* la table des
alertes filtrée par « aucune enquête rattachée ».

### Ce que le modèle ne peut pas faire

| Garde-fou | Pourquoi |
| :--- | :--- |
| Il ne renseigne aucun identifiant | les lui confier lui permettrait d'attacher son analyse à une autre alerte ; l'opérateur lirait une explication qui ne correspond pas |
| Il ne peut rendre qu'une `InvestigationSubmission` | schéma dérivé du contrat pivot, en décodage contraint |
| Six outils MCP seulement, en liste blanche | un outil ajouté au serveur MCP ne doit pas atterrir dans sa fenêtre de contexte par oubli |
| Nombre de tours plafonné, conclusion forcée au dernier | un modèle qui ne conclut pas consommerait indéfiniment des jetons |
| Il ne décide rien | il recommande ; il n'émet, ne notifie et ne clôt aucune alerte (F26) |

Les faits déterministes (F21) sont calculés **avant** tout appel au modèle :
âges, écarts, retards. Deux raisons — lui épargner des lectures, et surtout lui
donner un socle qu'il ne peut pas inventer. Un LLM à qui l'on demande « depuis
combien de temps le véhicule est-il arrêté ? » répond parfois de travers avec
assurance.

### Ce que l'enquêteur lit en plus du TMS

Deux lectures que la spécification lui accorde (F21) et qui ne relèvent pas du
TMS — le serveur MCP est la frontière du TMS, pas celle de notre base. Ce sont
donc des outils **locaux** du service agent, comme `call_driver` :

| Outil | Ce qu'il apporte, et qu'aucune lecture du TMS ne donne |
| :--- | :--- |
| `get_agent_history` | la **récurrence** : alertes passées et leur clôture, enquêtes précédentes, appels, et surtout les **décisions d'opérateur**. Une dérive de 7 km est anodine si cette mission dérive là tous les jours, et préoccupante si c'est la première fois |
| `evaluate_rules` | la condition **tient-elle encore ?** `this_rule_still_matches` distingue une situation qui dure, une situation résorbée (la clôture automatique va suivre) et une alerte qui semble infondée — motif légitime de `false_positive_suspicion` |

`evaluate_rules` appelle le moteur **tel quel**, celui-là même qui a créé
l'alerte. Le réimplémenter pour « simplifier » produirait deux vérités
divergentes, et c'est l'alerte qui perdrait sa valeur.

Les deux coûtent ensemble ~250 jetons de schéma, contre 1 196 pour
`submit_assessment` : ils tiennent dans le budget décrit plus bas.

Vérifié sur une enquête réelle (modèle de repli, 14 979 jetons) : le modèle a
appelé les deux de lui-même, et ses preuves citent
`get_agent_history (x_tms, msn_…)` et `evaluate_rules (x_tms)`.

### Appeler le chauffeur

C'est la **seule action du système vers le monde extérieur** : tout le reste lit
le TMS ou écrit dans notre base. Un téléphone sonne chez quelqu'un qui conduit,
et c'est un LLM qui l'a décidé — d'où quatre barrières, chacune contre une façon
précise de nuire.

| Garde-fou | Ce qu'il empêche |
| :--- | :--- |
| Clé d'idempotence `alert:<id>:<chauffeur>` | une enquête rejouée, ou reprise après interruption, ne rappelle pas |
| Quota par mission (3) | une mission qui dérive longtemps ne déclenche pas une cascade d'appels |
| Quota (2/h) et cooldown (15 min) par chauffeur | un chauffeur n'est pas harcelé, même si plusieurs de ses missions dérivent |
| Motif obligatoire (20 caractères) et gravité minimale `high` | un appel sans raison écrite est refusé ; en deçà du seuil, une notification suffit |

Deux propriétés valent d'être soulignées :

- **un appel refusé est enregistré avec son motif.** Le taire laisserait croire
  qu'aucun appel n'a été tenté ;
- **les quotas se comptent sur l'heure du TMS**, comme les cooldowns d'alerte :
  en simulation accélérée, un quota horaire doit suivre le temps du TMS et non
  celui de la machine.

L'unicité de la clé d'idempotence est **partielle** — restreinte aux tentatives
réellement remises au fournisseur. Totale, elle tendrait un piège : un appel
refusé par un cooldown écrirait sa ligne, et la tentative suivante, légitime une
fois le cooldown écoulé, serait refusée pour « déjà appelé ». Le chauffeur ne
serait jamais joint.

```bash
DRIVER_CALL_ENABLED=false     # retire l'outil au lieu de refuser les appels :
                              # un outil absent ne tente pas le modèle et
                              # n'occupe pas sa fenêtre de contexte
```

> **Écart assumé avec la spécification.** Le document d'architecture range
> `call_driver` parmi les outils MCP ; il est ici un outil **local du service
> agent**. Le serveur MCP est l'unique frontière avec le TMS, et le TMS est en
> lecture seule : un appel téléphonique n'est donc pas une opération TMS. Sa
> trace appartient au domaine de l'agent, et faire écrire le serveur MCP dans la
> base de l'orchestrateur mêlerait deux domaines que l'architecture sépare. Le
> numéro, lui, reste lu dans le TMS par MCP : l'annuaire appartient au client.

Le modèle ne fournit que le **motif**. Ni le numéro, ni l'identifiant du
chauffeur, ni la mission : tout cela est résolu depuis l'enquête en cours. Un
modèle qui pourrait nommer son destinataire pourrait appeler le chauffeur d'une
autre mission — et le quota porterait sur la mauvaise personne.

En développement, l'appel est servi par le canal console : les garde-fous et la
trace sont réels, seul le fournisseur de téléphonie manque. Le brancher ne
touchera qu'une ligne de `services/agents/main.py`.

### Un quota d'inférence n'est pas un défaut de notre côté

Trouvé en faisant tourner la pile complète : le palier gratuit du fournisseur
a répondu **429** (7 000 jetons par minute dépassés). Ce code était classé
« incident passager », donc l'enquête était marquée **en échec** et consommait
une tentative. Trois 429 de suite — une minute de saturation — privaient
l'alerte d'analyse **définitivement**, et rien ne le signalait : l'alerte, elle,
existait bien.

Deux corrections, par application de notre propre doctrine (« sauté » = le LLM
n'était pas là ; « en échec » = quelque chose ne va pas chez nous) :

- un 429 est désormais une **indisponibilité**, et l'enquête est *sautée* ;
- une enquête sautée est **reprise** dès que le modèle revient. C'était le vrai
  défaut : la file d'enquêtes est la table des alertes filtrée par « aucune
  enquête rattachée », et une ligne `skipped` suffisait à écarter l'alerte pour
  toujours. La reprise est bornée par `max_attempts` et ne concerne que les
  alertes **encore ouvertes** — analyser une alerte close depuis une heure
  n'apprend plus rien.

### Tenir dans la fenêtre du modèle

Rencontré en exécution réelle, au troisième tour d'une enquête :

> *This model's maximum context length is 4096 tokens. However […] your prompt
> contains at least 4097 input tokens.* — puis le repli cloud refusant à son
> tour, la requête dépassant son quota par minute. **L'enquête était perdue.**

Mesure d'abord, correction ensuite. Le coût **fixe** d'une enquête, avant tout
historique :

| Poste | Jetons |
| :--- | ---: |
| Prompt système (tous outils décrits) | 1 110 |
| Schéma `submit_assessment` | 1 136 |
| Schémas des 6 outils MCP | 1 091 |
| Schémas des 3 outils locaux | 417 |
| **Socle, avant le premier résultat** | **~3 750** |

Pour une fenêtre de 4 096 jetons, le constat est sans appel : le socle la
remplit presque, et aucune compaction d'historique n'y change quoi que ce soit.
Trois mesures, dans l'ordre d'efficacité :

**Au dernier tour, seul l'outil de conclusion est envoyé.** `tool_choice`
l'impose de toute façon ; les schémas de lecture, eux, pèsent 1 508 jetons
inutiles — précisément là où la fenêtre est le plus sollicitée. C'est ce qui
fait tenir la conclusion.

**L'historique est compacté avant l'envoi**
([budget.py](services/agents/integrations/budget.py)), selon trois principes :

- **ce qui ne peut pas être reconstitué est gardé** : prompt système et faits
  déterministes ne sont jamais touchés ;
- **les résultats d'outils les plus anciens partent les premiers.** Un
  instantané lu il y a trois tours a déjà produit son effet ; le dernier est ce
  sur quoi le modèle travaille ;
- **ce qui est retiré est annoncé.** Un résultat remplacé par un silence
  inviterait le modèle à conclure de l'absence ; remplacé par « retiré pour
  tenir dans la fenêtre », il sait qu'il peut relire.

Un message d'outil est **vidé**, non supprimé : un appel d'outil sans son
résultat fait rejeter la conversation entière par l'API. Quand vider ne suffit
plus, ce sont des tours **complets** qui partent ensemble.

**Un dépassement est reconnu pour ce qu'il est.** Le fournisseur refuse en
`400`, code que l'on classait « défaut définitif » — et l'enquête était perdue
alors qu'un historique compacté passe. La qualification se fait sur le message,
seul à distinguer « trop long » de « requête malformée », et reconnaît aussi la
formulation du repli (*request too large, reduce your message size*). Une
seconde tentative resserrée suit, et une seule.

L'estimateur est **calibré sur une mesure** : 12 000 caractères pour 3 166
jetons facturés, soit 3,79 caractères par jeton. Il retient 3,5 — il estime
donc 8 % au-dessus du réel, car sous-estimer coûte une requête refusée tandis
que surestimer coûte du contexte que le modèle aurait su employer.

Vérifié contre le modèle local, sur l'historique qui échouait :

```
historique brut   : ~7 751 jetons estimés   (fenêtre : 4 096)
après compaction  : ~3 657 jetons, 4 messages (contre 14)
accepté           : 2 876 jetons réels en entrée, 284 en sortie
                    → submit_assessment complet
```

```bash
AGENT_MAX_INPUT_TOKENS=3800   # fenêtre du modèle moins la place de sa réponse
```

En production, la fenêtre est bien plus large et ce plafond doit suivre. Si le
socle le dépasse, le journal le dit en nommant les deux leviers : élargir la
fenêtre, ou réduire la liste d'outils.

**Le prompt ne décrit que les outils réellement offerts.** Décrire un outil
absent invite le modèle à l'appeler — un tour perdu, qui sur une fenêtre étroite
peut être l'enquête entière. Sans les trois outils locaux, le prompt passe de
1 110 à 681 jetons.

### Le budget en jetons, mesuré

Mesuré sur une enquête réelle, le coût d'un tour se décompose ainsi :

| Poste | Jetons |
| :--- | ---: |
| Prompt système | 661 |
| Contexte initial (faits déterministes) | 428 |
| Schémas des 6 outils MCP | 1 091 |
| Schéma `submit_assessment` | 1 196 |
| **Tour 1** | **~3 400** |

Tout cela est **renvoyé à chaque tour** avec l'historique accumulé : une enquête
de trois tours avec appels d'outils dépasse 10 000 jetons d'entrée, ce qui
rejoint l'estimation de 30 000 à 50 000 du document d'architecture.

Deux leviers ont été appliqués, et ce ne sont pas des micro-optimisations :

- **liste blanche de six outils** au lieu des onze du serveur MCP ;
- **schéma compacté** — références `$defs` aplaties, `title` et `default`
  retirés : 1 641 → 1 196 jetons, sans perdre une contrainte.

> **Le modèle local se choisit par la VRAM restante, pas par sa taille.** Sur
> la RTX 4070 (8 Go), Qwen2.5-7B-AWQ ne tenait qu'à 4 096 jetons — en dessous
> du socle d'une enquête (~3 750 jetons), d'où les refus pour fenêtre dépassée.
>
> Qwen3.5 règle le problème par son architecture : **une couche d'attention
> complète sur quatre**, le reste en attention linéaire, donc un cache KV
> environ quatre fois plus petit à contexte égal. Reste à faire entrer les
> poids. Mesuré :
>
> | Variante | Poids | Tient dans 8 Go ? |
> | :--- | ---: | :--- |
> | `Qwen/Qwen3.5-4B` (bf16) | 9,3 Go | non |
> | `RedHatAI/Qwen3.5-4B-FP8-dynamic` | 7,8 Go | non — l'encodeur visuel et l'attention linéaire ne sont pas quantifiés |
> | `cyankiwi/Qwen3.5-4B-AWQ-4bit` | 4,0 Go | oui, mais à télécharger |
> | **`Qwen/Qwen3.5-2B` (bf16)** | **4,3 Go** | **oui** |
>
> D'où le choix du 2B officiel. Les poids ne suffisent pourtant pas à conclure :
> au premier démarrage, vLLM a mesuré **0,16 Gio disponibles pour le cache KV**
> et plafonné à 10 336 jetons. Trois réglages, tous justifiés par notre usage
> réel, dégagent la place :
>
> - `--gpu-memory-utilization=0.95` — la carte ne pilote pas l'affichage ;
> - `--kv-cache-dtype=fp8` — deux fois plus de contexte, pour une perte de
>   précision négligeable sur des clés et valeurs d'attention ;
> - `--max-num-seqs=8` au lieu de 256 — nous servons deux enquêtes en parallèle
>   et un assistant, pas une file de production.
>
> Résultat mesuré : **1,03 Gio de cache KV, soit 147 898 jetons** — la fenêtre
> de 32 768 passe avec 4,5 requêtes simultanées.
>
> La cible Azure (A10 24 Go ou A100 80 Go) permet le 4B, voire le contexte
> complet de 262 K.

### Le plafond de sortie manquait, et la fenêtre l'a révélé

Une conclusion d'enquête prenait **six minutes**, parfois plus de dix. Les
mesures ont écarté les coupables évidents l'un après l'autre :

| Génération | Débit |
| :--- | ---: |
| Libre | 59 jetons/s |
| Avec les schémas d'outils, choix libre | 56 jetons/s |
| Notre schéma `submit_assessment`, entrée courte | 58 jetons/s |
| **Le même, entrée longue** | **1,8 jeton/s** |

Ni le modèle, ni la mémoire, ni la grammaire : vLLM rapportait 59 jetons/s
pendant les six minutes. Le modèle **générait simplement sans fin**, car nous
n'envoyions **aucun `max_tokens`** — il pouvait remplir la fenêtre entière,
soit 32 768 jetons, soit neuf minutes à ce débit. Les durées observées le
disaient : 125 s et 366 s, c'est-à-dire 120 s de délai LiteLLM plus une, puis
deux reprises.

Sur une fenêtre de 4 096 jetons, le défaut restait invisible : il n'y avait pas
de place pour s'emballer. En l'élargissant, il est devenu la première cause de
lenteur. Le plafond est désormais **toujours envoyé**
(`AGENT_MAX_OUTPUT_TOKENS`, 1 200 par défaut, mesuré contre une évaluation
complète à ~700 jetons) : la même conclusion revient en **20 secondes**, et une
génération coupée n'est pas perdue — la boucle rend la soumission non conforme
au modèle, qui la corrige au tour suivant.

Deux réglages de service accompagnent le décodage contraint : `backend:
xgrammar` plutôt que le choix automatique, et `disable_any_whitespace: true` —
autoriser les espaces libres multiplie les chemins que la grammaire doit
évaluer à chaque jeton, pour un JSON que nous analysons de toute façon.

### Ce que le changement donne, mesuré

Même question, même machine, avant et après :

| | Qwen2.5-7B-AWQ, 4 096 | Qwen3.5-2B, 32 768 |
| :--- | :--- | :--- |
| « Y a-t-il un écart ? Donne le chiffre. » | invente « 12,3 km » sans lire | lit la flotte puis l'instantané, répond **7,95 km** |
| Tours possibles | un seul | **3 tours, 9 211 jetons d'entrée** |
| Conclusion structurée | hors fenêtre | **conforme en 20 s** |

### Un défaut de décodage contraint, trouvé et corrigé

Le modèle local, avec un schéma imbriqué par `$defs`/`$ref`, **ne produisait
aucun appel d'outil** : il répondait en prose malgré un `tool_choice` imposé. Le
même schéma aplati fonctionne du premier coup. Ce n'est pas propre à ce modèle —
plusieurs fournisseurs restreignent les références dans leurs modes contraints.
D'où l'aplatissement systématique, qui reste mécanique : le schéma demeure
dérivé du contrat.

---

## L'assistant opérateur

Le second endroit où un LLM intervient, et son cadre diffère de celui de
l'enquêteur sur un point décisif : **il parle à un humain qui attend une
réponse**, là où l'enquêteur produit une structure lue par une interface.

```bash
# question ponctuelle
uv run python -m services.agents.assistant.cli --tms x_tms "Où en est msn_1 ?"

# rapport de situation
uv run python -m services.agents.assistant.cli --mode report --tms x_tms

# conversation (historique relu à chaque tour)
uv run python -m services.agents.assistant.cli --chat --tms x_tms

# avec le droit d'agir, accordé demande par demande
uv run python -m services.agents.assistant.cli --allow-actions --tms x_tms "Clos l'alerte <id> : détour autorisé"
```

Cette ligne de commande sert à l'éprouver contre l'infrastructure réelle ;
l'assistant lui-même est une bibliothèque, appelée demain par l'Ops API.

### Trois modes, trois formats

| Mode | Pour quoi | Format attendu |
| :--- | :--- | :--- |
| `ask` | une question ponctuelle | trois phrases au plus, la réponse d'abord |
| `chat` | une conversation suivie | bref, sans redemander ce qui a été dit |
| `report` | un état de la situation | synthèse, puis une ligne par mission à surveiller |

Le mode ne change pas les capacités : il change le prompt, la longueur attendue
et l'historique relu. Un même opérateur peut poursuivre en `chat` ce qu'il a
ouvert en `ask`.

### Ce qu'il peut faire, et ce qu'il ne peut pas

| Garde-fou | Pourquoi |
| :--- | :--- |
| Il n'émet aucune alerte et ne notifie personne (F26) | alerter est le travail des règles ; un LLM qui pourrait notifier rendrait la détection non déterministe |
| Trois actions seulement : clore une alerte, appeler un chauffeur, consigner une décision | liste fermée, trois gestes qu'un opérateur accomplirait lui-même |
| Les outils d'action ne lui sont **présentés** que si l'opérateur autorise d'agir | un outil absent ne peut pas être appelé par zèle, et n'occupe pas la fenêtre de contexte |
| Clore une alerte et appeler un chauffeur sont **préparés, puis validés** | le modèle n'exécute jamais un geste qui sort du système ; l'opérateur valide ce qu'il lit (voir plus bas) |
| Chaque action exige un motif écrit, et est journalisée avec son demandeur (F27) | « l'assistant a clos l'alerte » ne suffit pas : il faut savoir qui le lui a demandé |
| Une clôture est enregistrée en `resolution_reason = operator` | la condition pouvait tenir encore ; la confondre avec `condition_cleared` tromperait la relecture |
| Les quotas d'appel sont partagés avec l'enquêteur | un chauffeur déjà appelé par une enquête ne doit pas l'être une seconde fois |
| Il n'écrit jamais dans le TMS | le serveur MCP est en lecture seule |

### Le rapport de situation à heure fixe

Un agent de suivi ne pose pas de question à 8 h du matin : il veut trouver
l'état de sa flotte en arrivant. Le service agent déclenche donc le mode
`report` aux heures choisies par l'exploitation.

```bash
ASSISTANT_REPORT_ENABLED=true
ASSISTANT_REPORT_TIMES=["08:00","14:00"]
ASSISTANT_REPORT_TIMEZONE=Europe/Paris     # « 8 h » reste 8 h pour l'agent de suivi
```

Trois décisions :

- **le rapport ne notifie personne.** Il est produit, conservé comme une
  conversation et lu via `GET /assistant/reports`. L'assistant n'émet ni alerte
  ni notification (F26), et un rapport poussé par SMS finirait par passer, dans
  l'esprit de ses destinataires, pour un substitut aux alertes ;
- **un créneau n'est produit qu'une fois**, même avec plusieurs instances du
  service agent : un verrou consultatif sérialise les candidats, et le dernier
  créneau produit est enregistré en base. Le verrou seul ne suffirait pas — une
  instance redémarrée à 8 h 02 reprendrait aussitôt le créneau ;
- **un créneau manqué n'est pas rattrapé indéfiniment.** Un service arrêté
  toute la nuit ne produit pas au démarrage les quatre rapports qu'il a ratés :
  seul le plus récent, et s'il a moins de `ASSISTANT_REPORT_CATCH_UP_MINUTES`.

Produit sur la pile réelle, avec une mission qui dérivait :

> **Point de situation — 01:03 (heure TMS)**
> Une mission en cours ; une demande une attention.
> - **MIS-2026-00001** — écart à l'itinéraire de **7,68 km**, en cours depuis
>   ≈ 8 min, sévérité *medium*. Le véhicule roule à 48,9 km/h et l'ETA est en
>   avance de 5,4 min. L'écart est donc réel mais récent et sans impact sur le
>   délai pour l'instant : à surveiller, pas à escalader.

### La validation humaine des actions

Un assistant qui peut clore une alerte ou faire sonner le téléphone d'un
chauffeur doit être encadré par autre chose que la bonne volonté du modèle.
Le dispositif tient en une phrase : **le modèle prépare, l'opérateur valide, le
système exécute ce qui a été montré.**

```
POST /assistant {"message": "Clos l'alerte 7f2a…", "allow_actions": true}
  → status: awaiting_confirmation
  → pending_actions[0].summary :
      « Clore l'alerte "Retard ETA significatif" (mission msn_4242…, règle eta_delay) »

GET  /assistant/pending                      ce qui attend cet opérateur
POST /assistant/actions/{id}/confirm          exécute les arguments ENREGISTRÉS
POST /assistant/actions/{id}/reject           n'exécute rien, et le trace
```

**Pourquoi la validation porte sur une ligne, et non sur un tour.** Autoriser
« agir » pour un tour de conversation laissait le modèle replanifier
librement : l'opérateur confirmait « clore l'alerte A » et rien, dans la
structure, ne garantissait que ce serait A. Ici la confirmation désigne un
identifiant, et l'exécution relit les arguments figés en base. Un test le
vérifie avec un modèle hostile qui, à la reprise, tente de clore une autre
alerte : il n'y parvient pas.

| Propriété | Mécanisme | Ce qu'elle empêche |
| :--- | :--- | :--- |
| Usage unique | `UPDATE … WHERE status = 'proposed' RETURNING` | deux onglets, deux clics, deux appels au même chauffeur |
| Péremption (15 min) | `expires_at` vérifié à la validation **et** à la lecture | un accord de ce matin exécuté cet après-midi |
| Mandat | seul le destinataire de la proposition tranche | un collègue, même mieux habilité, qui valide à sa place |
| Survie aux pannes | état `executing` + bail de 5 min, soldé par un balayage | une interface affichant « en cours » sur un geste que plus personne ne tient |
| Idempotence de l'appel | la clé d'idempotence **est** l'identifiant de la validation | un rejeu qui refait sonner le téléphone |
| Traçabilité (F27) | proposition, décision et exécution datées et attribuées | « l'assistant a clos l'alerte », sans savoir qui l'a voulu |

**La description est construite par le code**, à partir des arguments et de la
base — titre de l'alerte, mission, règle, nom et numéro du chauffeur. La prose
du modèle est utile mais ne fait pas foi : l'opérateur doit lire ce qui va
réellement s'exécuter, pas une reformulation.

**Consigner une décision ne demande pas de validation.** Le partage n'est pas
une commodité : clore une alerte la retire de l'écran des collègues, appeler
fait sonner le téléphone de quelqu'un qui conduit — les deux sortent du
système. Consigner n'écrit qu'une ligne de journal que l'opérateur vient de
dicter, et une friction inutile finit par être cliquée sans lire.

**Les plans en plusieurs gestes fonctionnent.** « Appelle le chauffeur, puis
clos l'alerte si le détour est justifié » : le premier geste est proposé, validé,
exécuté ; son résultat réel est versé dans la conversation ; le modèle est
rappelé et propose le second, qui demande sa propre validation. Aucun état ne
vit en mémoire entre les deux — **la conversation est l'état**, et elle est en
base.

Vérifié de bout en bout sur la pile réelle, sur une alerte créée par le worker
de surveillance : proposition rendue avec sa description, alerte encore ouverte
avant validation, close en `resolution_reason = operator` après, rejeu de la
même validation refusé sans rien exécuter, journal d'audit portant
`alert_created / monitoring_worker` puis `alert_closed_by_operator / dev_operator`.

### Pourquoi pas LangGraph

C'est le modèle d'exécution de LangGraph — `interrupt` puis `resume` — mais
porté par des lignes que l'interface peut **interroger**. C'est la raison du
choix, et elle est structurelle : « qu'attend-on de moi ? » est une question
qu'un opérateur pose en ouvrant son écran, pas seulement dans la continuité
d'une requête HTTP. Un état de graphe sérialisé ne répond pas à cette question,
et il faudrait de toute façon une table pour le faire — après quoi le
*checkpointer* n'ajoute plus que la reprise d'un tour d'inférence en vol, au
prix d'un second pilote de base (psycopg à côté d'asyncpg).

Ce choix reste réversible et son critère est écrit : si un besoin de
ramification avec reprise par branche apparaît — un plan qui s'ouvre en
parallèle sur deux cents missions, chaque branche reprise indépendamment —
LangGraph remplacerait `_converse` sans toucher aux contrats, aux outils, aux
actions ni à l'historique.

### Voir l'assistant travailler

Un opérateur qui pose une question attend parfois une minute : le modèle lit le
TMS, revient, relit. Sans retour visible, il croit l'interface figée et repose
sa question. Le flux rend le travail observable **pendant** qu'il se fait.

```bash
curl -N -X POST http://127.0.0.1:8020/assistant/stream \
  -H 'content-type: application/json' \
  -d '{"message":"Y a-t-il un écart d itinéraire ? Donne le chiffre."}'
```

```
event: started          { conversation_id, mode, allow_actions }
event: round_started    { round: 1, max_rounds: 6 }
event: tool_started     { tool: "get_active_missions", role: "read", arguments }
event: tool_completed   { ok: true, duration_ms: 51, preview }
event: round_started    { round: 2, max_rounds: 6 }
event: tool_started     { tool: "get_mission_snapshot", role: "read", arguments }
event: tool_completed   { ok: true, duration_ms: 55, preview }
: keepalive
event: text_delta       { text: "Oui" } … 66 fragments
event: done             { answer: … rounds: 3, input_tokens: 8807 }
```

Trois routes diffusées, qui doublent les routes existantes sans les remplacer :
`POST /assistant/stream`, `POST /assistant/actions/{id}/confirm/stream`,
`POST /assistant/actions/{id}/reject/stream`.

> À consommer avec `fetch` et un lecteur de flux, **pas** avec `EventSource` :
> celui-ci ne sait faire que des requêtes GET, et la question de l'opérateur
> voyage dans le corps.

**Un seul chemin de code.** `respond()` *consomme* `stream()` au lieu de refaire
le travail. Deux implémentations se mettraient tôt ou tard à divergerー l'une
persistant les lectures d'outil, l'autre l'oubliant ; l'une respectant la
validation humaine, l'autre non. Un test ne s'en remet pas à la lecture du
code : il compare l'état de la base après les deux voies, et exige qu'il soit
identique.

**Aucune « pensée » fabriquée.** Un modèle servi par une API compatible OpenAI
n'expose pas son raisonnement, sauf s'il émet du `reasoning_content` — alors
nous le relayons (`reasoning_delta`), et c'est tout. Inventer un commentaire
plausible à sa place serait mentir à l'opérateur sur ce que la machine fait.
Ce qui tient lieu de raisonnement visible, ce sont les **étapes** : quels
outils, dans quel ordre, avec quel résultat — et une lecture (`role: "read"`)
n'est pas affichée comme une action (`role: "action"`), parce que la seule
question qui compte pour un opérateur est : *est-ce que quelque chose va se
produire ?*

**L'écriture en base devient incrémentale.** La question est enregistrée avant
le premier appel au modèle, chaque lecture au moment où elle revient. Un onglet
fermé au milieu laisse donc une conversation fidèle à ce qui a réellement eu
lieu — là où une écriture unique en fin de tour aurait tout perdu, y compris la
question. Une action préparée survit de la même façon : elle attend dans
`GET /assistant/pending`, et rien n'a été exécuté.

### Deux pièges du protocole de diffusion

Les ignorer donne un flux qui marche en développement et casse en production.

**Les appels d'outils arrivent fragmentés** — le nom dans un fragment, les
arguments dans plusieurs autres, parfois coupés au milieu d'une chaîne JSON.
Ils sont accumulés **par `index`** et non par ordre d'arrivée : un modèle qui
demande deux outils d'un coup intercale leurs fragments, et concaténer dans
l'ordre de réception mélangerait leurs arguments. Un test reproduit exactement
ce cas.

**La consommation de jetons n'arrive qu'à la fin**, et seulement si on la
demande (`stream_options: {include_usage: true}`). Sans cela, le poste de coût
principal du système deviendrait invisible dès qu'on diffuse — mesuré sur le
flux réel ci-dessus : 8 807 jetons d'entrée pour trois tours.

Côté acheminement, une subtilité vaut d'être dite : attendre l'événement
suivant avec `wait_for` annulerait, au délai écoulé, l'étape que le générateur
avait commencée — une lecture du TMS perdue à chaque battement de cœur. La
tâche est donc conservée d'une attente à l'autre, et un test le vérifie en
observant qu'un travail long va jusqu'à son terme.

### L'historique

F25 demande qu'une conversation garde son historique après un redémarrage.
Deux tables (`conversations`, `conversation_messages`) y suffisent. Trois
raisons de ne pas employer le *checkpointer* de LangGraph, en complément de
celle donnée plus haut :

- **cet historique est lu par des humains.** L'Ops API doit pouvoir afficher
  une conversation, un auditeur relire ce que l'assistant a répondu avant une
  décision. Un état de graphe sérialisé ne se montre pas et ne s'interroge pas
  en SQL ;
- **un seul pilote de base.** Le *checkpointer* officiel s'appuie sur psycopg ;
  tout le reste parle asyncpg via SQLAlchemy Core ;
- **la reprise dont nous avons besoin est déjà portée.** Un plan interrompu par
  une validation reprend depuis la conversation et la ligne d'action validée,
  toutes deux en base et toutes deux lisibles. Reste la reprise d'un *tour
  d'inférence* tué en vol : nos lectures étant idempotentes et la seule
  écriture passant par une validation à usage unique, rejouer le tour donne le
  même résultat que le reprendre — pour un dixième de la complexité.

Ce qui est conservé va plus loin que les tours de parole : **les lectures MCP y
figurent** (`role = 'tool'`), de sorte qu'une réponse reste vérifiable après
coup. Le rang d'un message est attribué par la base, avec un index unique sur
`(conversation_id, seq)` : deux fenêtres du même opérateur ne peuvent pas
produire deux messages indiscernables.

### Un défaut trouvé sur données réelles : tronquer par longueur

Interrogé sur l'écart d'itinéraire d'une mission réelle, le modèle a répondu —
correctement — qu'il ne pouvait pas savoir : *« le snapshot est tronqué avant le
bloc `deviation` »*. Notre troncature à 2 500 caractères avait coupé exactement
le champ qui portait la réponse.

Mesure, plutôt que supposition :

| Champ de l'instantané | Caractères |
| :--- | ---: |
| `planned_route` | 11 026 |
| `events` | 638 |
| `deviation` | 301 |
| `eta` | 195 |

L'itinéraire planifié pesait trente-six fois l'écart à cet itinéraire, et le
chassait hors de la fenêtre. D'où une **réduction par le sens** avant toute
troncature ([reducers.py](services/agents/integrations/reducers.py)) : les
champs de décision passent entiers, les longs tableaux sont résumés, et ce qui
est retiré est **annoncé** (`events_omitted`, `waypoint_count`). Un modèle qui
lit « 23 événements plus anciens non inclus » sait quoi demander ; un modèle qui
lit un JSON coupé au milieu d'un mot ne sait rien.

Résultat sur la même mission : `get_route` passe de 11 Ko à 428 caractères, et
l'instantané complet tient en 2 895 — sans troncature. La troncature par
longueur reste en dernier filet, et la réponse signale désormais une coupure
due à la limite du modèle plutôt que d'afficher une phrase interrompue.

> **Un défaut que cette réduction a introduit, et qui a coûté une enquête.** Les
> annotations d'omission (`events_omitted`, le résumé de l'itinéraire) étaient
> écrites **dans** l'instantané. Les contrats pivot interdisant tout champ
> inattendu, l'instantané réduit ne se validait plus — et le même objet étant lu
> par le modèle *et* par notre code, l'enquêteur repartait « sur les seuls
> faits », la réévaluation des règles devenait impossible et un appel au
> chauffeur ne trouvait plus son destinataire. Le journal le disait pourtant à
> chaque fois : *« instantané non conforme »*. Les omissions vivent désormais
> **à côté** de l'instantané, dans l'enveloppe de la réponse : le modèle les lit
> dans la même charge utile, et le contrat tient. Un test valide désormais
> l'instantané réduit contre son contrat.

> **Limite du modèle de développement.** Sur la même question, le modèle local
> d'alors — Qwen2.5-7B-AWQ, fenêtre de 4 096 — inventait un chiffre
> (« 12,3 km ») sans lire l'instantané ; le modèle de repli
> (Qwen3 27B chez Groq) répond *« écart de 5,16 km, depuis 26,2 minutes,
> sévérité medium, ETA non affecté »* en deux lectures — vérifié contre le TMS.
> La différence n'est pas dans les prompts : un modèle de 7 milliards de
> paramètres choisit mal ses outils et comble les trous. L'assistant est donc à
> évaluer sur la cible, pas sur le poste de développement.

---

## L'API d'exploitation

La seule porte publique du système. Elle ne décide de rien : elle lit, et
transmet les demandes d'action aux services qui en sont propriétaires, avec
leurs garde-fous.

```bash
uv run python -m services.ops_api.main        # http://127.0.0.1:8020
# documentation interactive : http://127.0.0.1:8020/docs
```

| Route | Ce qu'elle rend |
| :--- | :--- |
| `GET /health` | sonde de vivacité, **sans jeton** — l'exiger ferait redémarrer le conteneur en boucle |
| `GET /alerts` | alertes filtrées (statut, gravité minimale, mission, TMS, date), les plus graves et les plus fraîches d'abord |
| `GET /alerts/summary` | compte des alertes ouvertes par TMS et par gravité : le bandeau d'exploitation |
| `GET /alerts/{id}` | l'alerte, son analyse IA, ses notifications, ses appels et son audit — en une réponse |
| `GET /missions` | missions à surveiller, via le serveur MCP |
| `GET /missions/{tms}/{mission}` | état complet d'une mission, dans notre vocabulaire |
| `POST /assistant` | question, conversation ou rapport (F24-F26) |
| `POST /assistant/stream` | la même demande, diffusée au fil de l'eau : étapes, outils, fragments de réponse (SSE) |
| `GET /assistant/pending` | actions préparées qui attendent la validation de l'opérateur |
| `POST /assistant/actions/{id}/confirm` | valide une action : exécute les arguments enregistrés, le modèle ne repasse pas |
| `POST /assistant/actions/{id}/reject` | refuse une action ; rien n'est exécuté, le refus est tracé |
| `POST /assistant/actions/{id}/confirm/stream`, `…/reject/stream` | les mêmes, diffusées (SSE) |
| `GET /assistant/reports` | rapports de situation produits à heure fixe (F24) |
| `GET /assistant/conversations/{id}` | historique d'une conversation (F25) |
| `GET /stream/alerts` | flux temps réel des nouvelles alertes (SSE) |
| `GET /monitoring` | dernier cycle par TMS et file d'enquêtes : ce qui rend une panne silencieuse visible |

### Authentification et rôles

Jeton Microsoft Entra ID (OAuth2/OIDC), vérifié sur quatre points — signature
contre le JWKS du tenant avec **RS256 imposé**, audience, émetteur, expiration.
Chacun correspond à une attaque : sans contrainte d'algorithme un jeton HMAC
passerait, et sans contrôle d'audience n'importe quelle application du tenant
appellerait cette API avec le jeton de son propre utilisateur.

Les rôles sont **ceux du TMS** : un agent de suivi n'a pas à recevoir un rôle
supplémentaire chez nous pour y faire ce qu'il fait déjà chez lui. Deux
permissions seulement, parce que la granularité par endpoint produit des
matrices que personne ne relit :

| Rôle | Lire | Agir |
| :--- | :---: | :---: |
| `ops_agent`, `ops_supervisor`, `transport_manager` | ✓ | ✓ |
| `operations_director`, `general_management`, `ops_viewer` | ✓ | |

Un rôle inconnu ne reçoit **aucune** permission : un annuaire mal configuré
doit fermer la porte, pas l'ouvrir.

```bash
OPS_API_AUTH_DISABLED=true    # poste de développement : aucun tenant Entra
```
Ce mode accepte toute requête sous une identité fictive. Il est bruyant à
dessein : chaque requête le journalise, et `GET /monitoring` le déclare dans sa
réponse.

### Deux décisions à expliquer

**Le périmètre de l'appelant prime sur ses filtres.** Un opérateur ne peut pas
élargir sa vue en passant un `tms_id` auquel il n'a pas accès, et une alerte
hors périmètre rend **404** et non 403 — un 403 révélerait son existence.

> Un défaut trouvé en écrivant les tests : l'intersection « périmètre de
> l'appelant ∩ filtre demandé » pouvait être vide, et un `tms_ids` vide signifie
> « aucune restriction » dans le dépôt. Un opérateur restreint à `x_tms` qui
> demandait `?tms_id=fleetlink` voyait donc **tout**. L'ensemble vide est
> désormais un marqueur explicite, et la route s'arrête dessus.

**Un sondage plutôt que `LISTEN`/`NOTIFY`** pour le flux temps réel. `NOTIFY` ne
délivre qu'aux sessions connectées à cet instant : une reconnexion de
l'interface, un redémarrage de l'API, un réseau qui hoquette, et l'alerte n'est
jamais rejouée. Un curseur en base rattrape tout ce qui s'est produit pendant la
coupure — propriété décisive quand l'objet manqué est une alerte critique. Le
curseur porte sur `created_at`, l'horloge du serveur, et non sur `detected_at`,
l'heure du TMS : en simulation accélérée, un curseur posé sur l'heure TMS
sauterait des alertes.

### Dégradation plutôt qu'arrêt

Vérifié sur la pile réelle, serveur MCP éteint :

```
GET  /alerts     -> 200   (les alertes sont en base)
GET  /missions   -> 503   « Serveur MCP injoignable »
POST /assistant  -> 503   « Assistant indisponible »
GET  /stream/alerts -> event: ready
```

Les alertes restent consultables, qui est le cas d'usage le plus fréquent. Deux
autres défauts ont été trouvés en interrogeant la pile complète : la liste des
TMS était lue sous la clé `id` au lieu de `tms_id` — l'API interrogeait un TMS
nommé « None » — et un opérateur sans périmètre déclaré laissait le modèle
**deviner** un `tms_id` (il a inventé `tms1`). Les TMS raccordés lui sont
désormais nommés.

---

## État d'avancement

| Composant | État |
| :--- | :--- |
| Infrastructure locale (`infra/`, `scripts/`) | opérationnelle |
| Contrats pivot (`shared/mapped_dto/`) | fait |
| Moteur de règles (`services/monitoring/rules/`) | fait |
| Base de données (`database/`, migrations Alembic) | fait |
| Cycle de vie des alertes (`services/monitoring/alerts/`) | fait |
| Boucle de surveillance (`scheduler.py`, `main.py`) | fait |
| Serveur MCP — connecteur, mappers, outils (`services/mcp_server/`) | fait |
| Dialectes de TMS + rapport de capacité | fait |
| Multi-TMS simultané (dimension `tms_id`) | fait |
| Envoi des notifications (`services/monitoring/notifications/`) | fait |
| Ops API (`services/ops_api/`) — REST, SSE, Entra ID + RBAC | fait |
| Contrats d'enquête + file atomique (`services/agents/`) | fait |
| Boucle d'enquête LLM (LiteLLM + outils MCP) | fait |
| Service agent, consommateur d'enquêtes | fait |
| `call_driver` sous garde-fous (F21) | fait |
| Assistant conversationnel (F24-F26) | fait |
| Validation humaine des actions (proposer / valider / reprendre) | fait |
| Flux temps réel du raisonnement et des outils (SSE) | fait |
| Historique de l'agent + réévaluation des règles pour l'enquêteur (F21) | fait |
| Rapport de situation à heure fixe (F24) | fait |
| Réduction par le sens des résultats d'outils | fait |

---

