# Architecture d'infrastructure & estimation des coûts — Orchestrateur de suivi des missions (Azure)

Oct 8, 2026 · @Saouadogo Salifou

**Statut :** Proposition technique et financière **Références :** *Spécification technique — Orchestrateur de suivi des missions* (6 oct. 2026) ; *Architecture cible — Orchestrateur de suivi des missions* (7 oct. 2026)

Ce document traduit l'architecture cible en infrastructure Azure déployable et chiffre son coût mensuel pour deux cas d'inférence : GPU dédié dans Azure, ou API managée sans rétention de données. L'infrastructure est identique pour le MVP et la production ; seuls le nombre de réplicas et la taille des ressources évoluent.

⚠️Les coûts mentionné à titre estimation dans des cas d'usage normal, un sous ajustement peut etre appliqué pour réduire drastiquement les couts mais en ayant en tete aussi de la performance globale de la solution à moindre  coût ⚠️

## 1. Synthèse

Les deux cas partagent le même socle applicatif (\~895 €/mois HT) et ne diffèrent que par l'inférence LLM.

| Cas | Inférence | Coût mensuel estimé (HT) |
| --- | --- | --- |
| Option 1A — GPU dédié, modèle de classe 14B | 1× A10 24 Go (`NV36ads_A10_v5`) | \~3 550 € |
| Option 1B — GPU dédié, Qwen3.8-27B | 1× A100 80 Go (`NC24ads_A100_v4`) | \~3 940 € |
| Option 2 — API managée, Qwen3.8-27B | Facturation au token, rétention nulle | \~1 130 – 1 405 € |

L'Option 2 est recommandée pour démarrer le MVP : elle coûte environ 3 fois moins cher. L'Option 1 garde toutes les données dans le VNet. Le passage de l'une à l'autre se fait par configuration de LiteLLM, sans modifier le code applicatif.

Les coûts de messagerie (SMS, appels, e-mails) feront l'objet d'une estimation ultérieure.

## 2. Hypothèses de charge

Une enquête consomme \~30 000 à 50 000 tokens d'entrée : l'enquêteur est une boucle LangGraph de plusieurs tours, et chaque tour renvoie le prompt système, les schémas des tools MCP et l'historique accumulé.

| Paramètre | Hypothèse (bas – haut) | Justification |
| --- | --- | --- |
| Missions surveillées | 100 – 200 | Flotte cible |
| Enquêtes / mois | \~15 000 | 300 – 500 alertes ≥ `medium` par jour (F19) |
| Appels LLM par enquête | 4 – 8 | Lecture mission, événements, positions, règles, puis `submit_assessment` |
| Tokens d'entrée / enquête | 30 000 – 50 000 | Prompt + schémas de tools (\~5 000) renvoyés à chaque tour, contexte croissant |
| Tokens de sortie / enquête | 2 000 – 4 000 | Appels de tools + évaluation structurée ; mode raisonnement désactivé |
| Assistant opérateur + rapports | +0 à +20 % | F24 – F26 : questions, conversations, rapports de situation |
| **Total entrée / mois** | **\~450 M – 900 M** |  |
| **Total sortie / mois** | **\~30 M – 75 M** |  |

Trois leviers réduisent ce volume : le cache de préfixe (vLLM ou fournisseur), un sous-ensemble de tools exposé à l'enquêteur, et des faits clés pré-calculés de façon déterministe (F21). Le volume réel sera mesuré par Langfuse dès le MVP et le poste LLM recalculé sur cette base.

## 3. Topologie réseau Azure

Le VNet comporte quatre sous-réseaux : l'environnement Container Apps et PostgreSQL Flexible exigent chacun un sous-réseau délégué exclusif ; Redis, Key Vault, Blob Storage et le registre s'y raccordent par *private endpoints*.

&#91;embedded content: topologie réseau · 4 sous-réseaux, 1 point d'entrée, 1 sortie\]

Seul `ops_api` reçoit du trafic Internet (le frontend statique est servi à part) ; toute sortie vers le TMS, l'API LLM et le fournisseur télécom passe par la passerelle NAT.

| Sous-réseau | Délégation | Contenu | Remarque |
| --- | --- | --- | --- |
| `snet-aca` | `Microsoft.App/environments` | Un seul environnement ACA qui héberge les 7 applications | Environnement « workload profiles » (/27 minimum), profil Consumption seul : requis pour router la sortie via NAT |
| `snet-postgres` | `Microsoft.DBforPostgreSQL/flexibleServers` | PostgreSQL Flexible | Exclusif : aucune autre ressource ne peut y être placée |
| `snet-endpoints` | Aucune | *Private endpoints* Key Vault, Blob Storage, ACR, Redis | Zones DNS privées associées au VNet |
| `snet-vm` | Aucune | VM ClickHouse, VM vLLM (Option 1) | Sans IP publique ; administration par Azure Bastion à la demande |

La région de déploiement est **West Europe**. Les quotas de cœurs GPU de la famille retenue sont à demander à Microsoft avant le déploiement de l'Option 1.

## 4. Services déployables

Les sept services sont sept applications d'un même environnement ACA (profil *Consumption*), plus une VM ClickHouse et, en Option 1, une VM GPU. Les services suivent le découpage de l'architecture cible : l'Alert Service et le Notification Service vivent dans `monitoring_worker`, pas dans le serveur MCP.

### Vue d'ensemble des flux

&#91;embedded content: architecture applicative · services et protocoles\]

`monitoring_worker` et `agent_service` passent tous deux par le serveur MCP pour lire le TMS, et `agent_service` passe par LiteLLM pour joindre le modèle, quelle que soit l'option. Non représentés : LiteLLM et Langfuse utilisent leurs propres bases PostgreSQL et Azure Managed Redis ; toutes les applications envoient leurs traces OTLP à Azure Monitor et lisent leurs secrets dans Key Vault par identité managée.

| Service | Hébergement | vCPU / RAM | Réplicas (min – max) | Ingress | Rôle |
| --- | --- | --- | --- | --- | --- |
| `ops_api` | ACA | 1 / 2 Go | 1 – 5 | Externe, Entra ID | API REST et diffusion temps réel des alertes |
| `monitoring_worker` | ACA | 1 / 2 Go | 1 – 1 | Aucun | Surveillance, règles, alertes, notifications |
| `tms_mcp_server` | ACA | 1 / 2 Go | 1 – 3 | Interne | Seul accès au TMS |
| `agent_service` | ACA | 2 / 4 Go | 1 – 3 | Interne | Enquêteur et assistant LangGraph |
| `litellm_proxy` | ACA | 2 / 4 Go | 1 – 2 | Interne | Passerelle LLM, repli, quotas |
| `langfuse-web` | ACA | 2 / 4 Go | 1 – 1 | Interne + console via Entra ID | Interface et API d'ingestion des traces |
| `langfuse-worker` | ACA | 2 / 4 Go | 1 – 1 | Aucun | Ingestion des traces vers ClickHouse |
| Frontend web | Static Web Apps | — | — | Externe | Interface des opérateurs |
| ClickHouse | VM `D2s_v5` + disque managé | 2 / 8 Go | 1 | Privé | Stockage analytique des traces Langfuse |
| vLLM (Option 1) | VM GPU, voir §6 | — | 1 | Privé, port 8001 | Inférence, file et *batching* gérés par vLLM |

**Dimensionnement :** `langfuse-web` et `langfuse-worker` suivent le minimum recommandé par l'éditeur, 2 vCPU et 4 Go par conteneur ([Langfuse](https://langfuse.com/self-hosting/deployment/infrastructure/containers)). `agent_service` et `litellm_proxy` passent à 2 vCPU / 4 Go pour absorber l'assistant en streaming et les enquêtes simultanées ; la recommandation LiteLLM de 4 vCPU / 8 Go vise \~1 000 requêtes par seconde, très au-delà de notre charge. Les services d'entrée-sortie (`ops_api`, `monitoring_worker`, `tms_mcp_server`) restent à 1 vCPU / 2 Go : leur charge augmente en ajoutant des réplicas, pas en grossissant chacune.

### Concurrence

- **`monitoring_worker`** : instance unique protégée par un verrou consultatif PostgreSQL (`pg_try_advisory_lock`). Le verrou empêche aussi une double évaluation pendant un déploiement.
- **`tms_mcp_server`** : MCP Streamable HTTP en mode *stateless* pour permettre plusieurs réplicas. Le quota du TMS est appliqué globalement, et `call_driver` porte une clé d'idempotence.
- **`agent_service`** : les enquêtes sont tirées d'une file PostgreSQL, 2 enquêtes simultanées par instance.
- **Notifications** : envoi via une table *outbox* écrite dans la même transaction que l'alerte, puis consommée de façon asynchrone ; un envoi lent ne bloque jamais le cycle de surveillance (F17).

## 5. Données et état

PostgreSQL porte toute l'atomicité métier : déduplication, cooldown, instance unique du moniteur et file d'enquêtes. Redis n'est présent que comme dépendance technique de Langfuse, et Azure Blob Storage stocke les objets.

### PostgreSQL Flexible Server

Un seul serveur, une base `orchestrator` et un schéma unique partagé par tous les services de l'orchestrateur. Langfuse et LiteLLM gardent chacun leur propre base, car ils gèrent eux-mêmes leurs migrations.

| Tables (schéma `orchestrator`) | Écrites par | Contenu |
| --- | --- | --- |
| `processed_events`, `monitoring_state`, `rule_evaluations` | `monitoring_worker` | Événements traités, état de surveillance, évaluations de règles |
| `alerts`, `notification_outbox`, `audit_log` | `monitoring_worker` | Alertes, notifications à envoyer et leur statut, audit append-only |
| `investigations`, `investigation_queue`, `driver_calls` | `agent_service` | File et résultats des enquêtes, appels chauffeur |
| `checkpoints` | `agent_service` | *Checkpoints* LangGraph de l'assistant |

Chaque table a un module propriétaire pour l'écriture, par convention de code ; les lectures croisées sont libres. Il n'y a ni cloisonnement par schéma ni droits SQL distincts par service.

**Mécanismes d'atomicité :**

- **Déduplication** : index unique partiel sur `(deduplication_key)` pour les alertes ouvertes, et `INSERT … ON CONFLICT` ; le cooldown est vérifié dans la même transaction, en temps TMS (F11).
- **Instance unique du moniteur** : `pg_try_advisory_lock` tenu par la connexion du worker ; si le worker s'arrête, le verrou se libère.
- **File d'enquêtes** : table de tâches consommée par `SELECT … FOR UPDATE SKIP LOCKED`, suffisante pour \~500 enquêtes par jour.

**Dimensionnement :** General Purpose `D2ds_v5` (2 vCPU, 8 Go), 128 Go, sauvegardes 14 jours avec restauration à un instant donné. La haute disponibilité zonale s'active à la montée en charge, sans changement d'architecture.

### Redis

Langfuse exige Redis pour sa file d'ingestion et son cache ; LiteLLM s'en sert aussi pour partager ses quotas entre réplicas. Aucune donnée métier n'y est stockée. Le service retenu est Azure Managed Redis (SKU Balanced `B0`), Azure Cache for Redis étant en voie de retrait ([annonce](https://techcommunity.microsoft.com/blog/azure-managed-redis/-/4458721)).

### Stockage des objets

Azure Blob Storage, compte privé raccordé par *private endpoint* : événements bruts Langfuse avant ingestion ([architecture Langfuse](https://langfuse.com/self-hosting)), exports et rapports de situation.

## 6. Inférence LLM

L'Option 1 s'appuie sur une VM GPU dédiée dans le VNet ; l'Option 2 sur une API managée appelée via LiteLLM. Prix Linux, paiement à l'usage, région West Europe, 1 $ ≈ 0,87 €.

### Option 1 : GPU dédié

| VM | GPU | Modèle | $/h | €/mois (24/7) |
| --- | --- | --- | --- | --- |
| `Standard_NV36ads_A10_v5` | 1× A10 24 Go | Classe 14B en BF16 ou FP8 | \~4,16 | \~2 640 |
| `Standard_NC24ads_A100_v4` | 1× A100 80 Go | Qwen3.8-27B en FP8 ou BF16, contexte long | \~4,78 | \~3 030 |

Sources : [NV36ads A10 v5](https://www.devzero.io/instances/azure/Standard_NV36ads_A10_v5), [NC24ads A100 v4](https://www.azurespeed.com/AzureVmPricing/Standard_NC24ads_A100_v4).

Qwen3.8-27B en FP8 occupe \~28 Go de poids. 48 Go de VRAM suffisent ; 32 Go ne laissent presque rien pour le cache KV et les activations. Sur Azure, aucune VM ne propose un GPU unique de 48 Go : cette capacité passe par 2× A10 (`NV72ads_A10_v5`, \~8,48 $/h, \~5 380 €/mois en West Europe — [tarifs](https://www.azurespeed.com/AzureVmPricing/Standard_NV72ads_A10_v5)), soit presque deux fois le prix de la A100 80 Go, qui reste le meilleur choix.

**Configuration vLLM :**

- Les A10 et A100 sont des GPU Ampere : les poids FP8 y sont chargés en mode *weight-only*, avec un gain mémoire mais sans calcul FP8 natif.
- L'appel d'outils exige `--enable-auto-tool-choice` et le *parser* adapté au modèle ; `submit_assessment` passe par le décodage contraint (JSON Schema).
- Le cache de préfixe (`--enable-prefix-caching`) est indispensable : prompt système et schémas de tools sont identiques d'un tour à l'autre.

### Option 2 : API managée

Qwen3.8-27B est proposé entre \~0,40 et 0,58 $ par million de tokens d'entrée et \~2,00 à 3,45 $ par million en sortie selon le fournisseur ([comparatif](https://artificialanalysis.ai/models/qwen3-8-27b/providers)). Avec les volumes du §2, le poste LLM coûte **\~235 à 510 €/mois**. Le cache d'entrée, facturé environ 5 fois moins cher chez certains fournisseurs, réduit encore ce montant.

Le fournisseur est retenu sur des critères contractuels : hébergement dans l'UE, accord de traitement des données, rétention nulle écrite au contrat, pas d'entraînement sur les données. Un repli vers un second fournisseur est configuré dans LiteLLM.

### Modèles

- **27B** : Qwen3.8-27B (août 2026, poids ouverts), dans les deux options.
- **Classe 14B** (Option 1A) : modèle récent à appel d'outils natif, à choisir lors du banc d'essai.
- **Mode raisonnement réglé au minimum** pour l'enquêteur : Qwen3.8-27B raisonne au niveau maximal par défaut, et ces tokens sont facturés comme sortie ([détail](https://www.qubrid.com/blog-news/qwen38-27b-pricing-api-cost-per-1m-tokens)).

## 7. Observabilité, sécurité et identités

Langfuse auto-hébergé comprend deux conteneurs (web, worker) et s'appuie sur PostgreSQL, Redis, ClickHouse et Azure Blob Storage ([architecture Langfuse](https://langfuse.com/self-hosting)). ClickHouse tourne sur une petite VM à disque managé ; un stockage Azure Files sous ACA n'est pas adapté à une base analytique.

### Observabilité

| Signal | Destination | Remarque |
| --- | --- | --- |
| Traces LLM (prompts, tokens, coûts, latences) | Langfuse, via callbacks LiteLLM | Contient des données de mission : même périmètre de confidentialité que PostgreSQL |
| Traces applicatives OpenTelemetry | Application Insights (ou Langfuse pour les spans agent) | Corrélation par `alert_id` et `investigation_id` |
| Logs conteneurs | Log Analytics (activé par défaut sur ACA) | Facturé à l'ingestion : niveau `INFO`, échantillonnage, rétention 30 jours |
| Métriques métier | Tableau de bord `ops_api` + alertes Azure Monitor | Durée de cycle, cycles en échec, alertes supprimées, file d'enquêtes |

Une alerte Azure Monitor doit se déclencher si aucun cycle de surveillance n'a réussi depuis 3 intervalles : c'est la panne la plus grave du système, et elle est silencieuse.

### Sécurité et identités

- **Utilisateurs** : Microsoft Entra ID (OAuth2/OIDC) pour le frontend et `ops_api` ; RBAC applicatif aligné sur les rôles TMS (`ops_agent`, `ops_supervisor`…).
- **Console Langfuse** : authentification intégrée ACA (Entra ID) restreinte à un groupe.
- **Services** : identités managées pour tirer les images du registre, lire Key Vault et se connecter à PostgreSQL (authentification Entra) ; aucun secret dans les variables d'environnement.
- **Secrets tiers** (identifiants TMS, clés LLM et télécom) : Key Vault, référencés par ACA.
- **Exposition publique** : seuls le frontend et `ops_api`.
- **Sortie Internet** : passerelle NAT avec IP fixe, à communiquer à l'éditeur du TMS si son API filtre par adresse IP.

## 8. Estimation des coûts mensuels

Le socle applicatif commun aux deux options coûte **\~895 €/mois** ; l'écart entre options vient uniquement du poste LLM. Prix HT, paiement à l'usage, West Europe, 1 $ ≈ 0,87 €. Les lignes marquées « approx. » sont à confirmer avec la calculatrice Azure.

### Socle applicatif

| Poste | Configuration | €/mois |
| --- | --- | --- |
| Container Apps (Consumption) | 7 applications, 11 vCPU / 22 Go en continu, autoscaling | \~485 (220 – 750) |
| PostgreSQL Flexible Server | `D2ds_v5`, 128 Go, sauvegardes 14 jours | \~150 |
| ClickHouse (VM + disque) | `D2s_v5` | \~80 (approx.) |
| Azure Managed Redis | `B0` | \~25 (approx.) |
| Réseau | NAT + 4 *private endpoints* | \~60 (approx.) |
| Registre, Key Vault, Blob Storage | ACR Premium (requis pour un *private endpoint*) | \~55 |
| Logs et traces Azure Monitor | \~15 Go/mois | \~40 (approx.) |
| **Total socle** |  | **\~895** |

Container Apps : 0,000024 $ par vCPU-seconde et 0,000003 $ par Gio-seconde en actif ; le vCPU tombe à 0,000003 $ quand la réplica est inactive ([grille ACA](https://www.devzero.io/blog/azure-container-apps-pricing)). La fourchette va de 100 % inactif à 100 % actif ; le moniteur et le serveur MCP, sollicités toutes les 10 s, restent actifs en quasi-permanence.

### Total par option

| Option | Socle (€/mois) | Poste LLM (€/mois) | Total (€/mois) |
| --- | --- | --- | --- |
| 1A — GPU dédié 14B, 1× A10, 24/7 | \~895 | \~2 640 + disque \~15 | **\~3 550** |
| 1B — GPU dédié Qwen3.8-27B, 1× A100, 24/7 | \~895 | \~3 030 + disque \~15 | **\~3 940** |
| 2 — API managée, Qwen3.8-27B | \~895 | 235 – 510, à l'usage | **\~1 130 – 1 405** |

Un arrêt programmé du GPU hors des heures d'exploitation réduit le poste GPU au prorata (12 h × 22 jours ouvrés : −64 %). La montée en charge (réplicas supplémentaires, haute disponibilité PostgreSQL) se fait avec le même outillage ; une réservation 1 an réduit ensuite les postes VM et PostgreSQL.

### Estimation future

- **Messagerie** (SMS, appels, e-mails) : fera l'objet d'une estimation ultérieure.

## 9. Matrice de décision et recommandations

Pour le MVP, l'Option 2 avec Qwen3.8-27B offre le meilleur rapport coût / mise en œuvre : environ 3 fois moins chère, sans exploitation GPU. L'Option 1 est la voie retenue si l'inférence doit rester dans Azure.

| Critère | Option 1 — GPU dédié Azure | Option 2 — API managée sans rétention |
| --- | --- | --- |
| Confidentialité | Données dans le VNet ; conforme au « LLM local par défaut » de la spécification | Contractuelle : hébergement UE, rétention nulle, pas d'entraînement |
| Coût mensuel | \~3 550 – 3 940 €, fixe | \~1 130 – 1 405 €, proportionnel à l'usage |
| Exploitation | Pilotes NVIDIA, versions vLLM, quota GPU, supervision VRAM | Aucune côté inférence ; dépendance au SLA du fournisseur |
| Disponibilité | Une seule VM : pas de redondance sans doubler le coût | Repli LiteLLM vers un second fournisseur en cas d'erreurs 429/5xx |
| Maîtrise du modèle | Version figée, quantification choisie | Version et quantification décidées par le fournisseur |
| Observabilité | Identique : LiteLLM → Langfuse et OpenTelemetry | Identique |

### Recommandations

1. **MVP** : Option 2 avec Qwen3.8-27B, chez un fournisseur hébergé dans l'UE avec rétention nulle contractuelle ; second fournisseur configuré en repli dans LiteLLM.
2. **Inférence dans Azure** : Option 1B sur `NC24ads_A100_v4`, en 24/7.
3. **Mesurer dès le MVP** le volume réel de tokens par enquête dans Langfuse, puis recalculer le poste LLM.
4. **Atomicité métier dans PostgreSQL** : déduplication, cooldown, instance unique et file d'enquêtes ; Redis limité à Langfuse et LiteLLM.
5. **Région West Europe** ; demander les quotas GPU dès le choix de l'Option 1.

## Sources

Prix consultés les 7 et 8 octobre 2026.

- [Standard\_NV36ads\_A10\_v5 — tarifs par région (DevZero)](https://www.devzero.io/instances/azure/Standard_NV36ads_A10_v5)
- [Standard\_NC24ads\_A100\_v4 — tarifs Linux par région (azurespeed)](https://www.azurespeed.com/AzureVmPricing/Standard_NC24ads_A100_v4)
- [Azure Container Apps — grille Consumption (DevZero)](https://www.devzero.io/blog/azure-container-apps-pricing)
- [Azure Container Apps — page de tarification Microsoft](https://azure.microsoft.com/en-us/pricing/details/container-apps/)
- [PostgreSQL Flexible Server — prix D2ds\_v5 par région (Bytebase)](https://www.bytebase.com/dbcost/azure-flexible/instance/D2ds_v5/)
- [PostgreSQL Flexible Server — options de calcul (Microsoft Learn)](https://learn.microsoft.com/azure/postgresql/compute-storage/concepts-compute)
- [Azure Managed Redis — gamme de prix (Dragonfly)](https://www.dragonflydb.io/guides/redis-on-azure-service-options-pricing-pros-and-cons)
- [Retrait d'Azure Cache for Redis (Microsoft Tech Community)](https://techcommunity.microsoft.com/blog/azure-managed-redis/-/4458721)
- [Langfuse — architecture d'auto-hébergement](https://langfuse.com/self-hosting)
- [Qwen3.8-27B — fournisseurs et prix API (Artificial Analysis)](https://artificialanalysis.ai/models/qwen3-8-27b/providers)
- [Qwen3.8-27B — tarification et raisonnement (Qubrid)](https://www.qubrid.com/blog-news/qwen38-27b-pricing-api-cost-per-1m-tokens)
