# Congress Stock Tracker – Contexte projet

## Vue d'ensemble

Bot Telegram + tableau de bord web qui surveille en temps réel les déclarations de transactions boursières des membres du Congrès américain (Sénat + Chambre des représentants), conformément au STOCK Act.

**Stack** : Python 3.11+, FastAPI, aiosqlite, APScheduler, python-telegram-bot, yfinance, Jinja2, Tailwind CSS (CDN), Chart.js (CDN).

**Déploiement cible** : Render.com (plan gratuit). Procfile : `web: python main.py`.

---

## Architecture

```
main.py          → Point d'entrée ; lifespan FastAPI (init DB → 1er poll → Telegram → scheduler)
config.py        → Variables d'env (dotenv)
database.py      → Couche SQLite async (aiosqlite) – toutes les requêtes passent ici
fetcher.py       → Récupération des trades (House XML + Senate GitHub mirror)
enricher.py      → Enrichissement (yfinance secteur, Wikipedia, détection conflit d'intérêt)
performance.py   → Calcul rendement sur 365 j par politicien + percentile
scheduler.py     → APScheduler AsyncIO – boucle de polling + notification
notifier.py      → Formatage Markdown + envoi Telegram
dashboard.py     → FastAPI app (HTML + JSON API)
templates/
  index.html     → SPA : feed temps réel, leaderboard, graphiques, profils (Tailwind + Chart.js)
committees.json  → Mapping politicien → commissions (chargé via lru_cache)
```

---

## Sources de données

| Source | Chambre | URL | Clé |
|---|---|---|---|
| House PTR XML | Chambre | `disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.xml` | Aucune |
| Senate GitHub mirror | Sénat | `raw.githubusercontent.com/common-wealth/senate-stock-watcher-data/…` | Aucune |
| yfinance | Prix / secteur | API Yahoo Finance | Aucune |

> Capitol Trades et Quiver Quant (anciens fetchers) sont conservés comme alias no-op pour compatibilité mais ne sont plus utilisés.

---

## Base de données SQLite (trades.db)

### Tables

**`trades`** – une ligne par déclaration de transaction
- `id` TEXT PK (format : `house_<md5>`, `senate_<md5>`, `mock_<counter>_<date>`)
- `politician_id` TEXT (snake_case du nom, ex: `nancy_pelosi`)
- `notified` INTEGER (0 = en attente, 1 = notification envoyée)
- `raw_data` TEXT JSON (données brutes source)

**`politicians`** – profil enrichi (upsert à chaque nouveau trade)

**`ticker_cache`** – cache secteur yfinance par ticker (chargé une seule fois par ticker)

**`ticker_price_cache`** – cache prix historiques `(ticker, date)` pour le calcul de performance

### Indexes
- `idx_trades_date`, `idx_trades_politician`, `idx_trades_notified`, `idx_trades_ticker`

---

## Flux de données (poll cycle)

```
fetch_all_trades()
  ├── fetch_house_trades()   → House XML → 100 PTR récents
  └── fetch_senate_trades()  → GitHub JSON → 100 trades récents
         ↓
insert_trade()  [retourne True si nouveau]
         ↓
get_unnotified_trades()
         ↓
Pour chaque trade non notifié :
  enrich_trade()          → secteur (yfinance cache) + Wikipedia + owner + conflit commission
  get_politician_performance()  → rendement pondéré 365 j
  get_percentile()        → classement vs autres élus
  send_trade_notification()  → Telegram Markdown
  mark_notified()
```

---

## Variables d'environnement

| Variable | Requis | Défaut | Rôle |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Oui | — | Token BotFather |
| `TELEGRAM_CHAT_ID` | Oui | — | ID du chat destinataire |
| `QUIVER_API_KEY` | Non | — | Clé Quiver Quant (backup inutilisé) |
| `POLL_INTERVAL_MINUTES` | Non | 15 | Fréquence de polling |
| `PORT` | Non | 8000 | Port uvicorn |
| `DATABASE_PATH` | Non | `trades.db` | Chemin SQLite |
| `MOCK_DATA` | Non | false | Données synthétiques (tests locaux) |

---

## API JSON (dashboard.py)

| Route | Description |
|---|---|
| `GET /` | Dashboard HTML (SPA) |
| `GET /api/trades` | Feed paginé (`limit`, `offset`, `politician`, `ticker`, `trade_type`) |
| `GET /api/leaderboard` | Top N politiciens par nombre de trades |
| `GET /api/stats` | Totaux globaux (trades, achats, ventes, politiciens, tickers) |
| `GET /api/chart/daily` | Trades par jour sur N jours |
| `GET /api/politician/{id}` | Profil + historique trades |
| `GET /health` | Health-check (Render/Railway) |

---

## Mode MOCK_DATA

`MOCK_DATA=true python main.py` → génère 50 trades synthétiques avec 8 politiciens fictifs et 12 tickers connus (AAPL, NVDA, TSLA, etc.). Aucun appel HTTP. Utile pour tester le stack complet localement.

---

## Enrichissement des trades (enricher.py)

- **Secteur** : lookup yfinance via `yf.Ticker(ticker).info`, mis en cache dans `ticker_cache`
- **Conflit d'intérêt** : `SECTOR_COMMITTEE_MAP` mappe secteur → mots-clés de commissions → comparé aux commissions du politicien via `committees.json`
- **Wikipedia** : URL construite par `Nancy_Pelosi` pattern
- **Owner** : champ `raw.owner` → traduit en label français (`Conjoint(e)`, `Enfant`, etc.)

---

## Calcul de performance (performance.py)

- Période : 365 derniers jours
- Trades pris en compte : **achats uniquement** (`purchase` / `buy`)
- Prix d'achat : `yf.Ticker.history(start, end)` → `Close` le plus proche
- Prix courant : `yf.Ticker.history(period="5d")` → `Close` le plus récent
- Rendement = `(current - buy) / buy * 100`
- Pondération par montant (`AMOUNT_MIDPOINTS`) → rendement moyen pondéré
- Percentile : rang du politicien parmi tous ceux ayant des trades (100 = meilleur)
- Cache performances : 30 min (`_PERF_CACHE_MINUTES`)

---

## Notifications Telegram (notifier.py)

Format Markdown avec :
- Parti (🔵/🔴/🟡), chambre, état
- Commissions (2 premières + compteur)
- Ticker, secteur emoji, montant, dates
- Alerte conflit d'intérêt si détecté
- Bloc performance 365 j + percentile
- Liens Wikipedia + Capitol Trades

---

## Évolutions prévues (backlog README)

- Filtres par élu ou ticker spécifique dans les alertes
- Score de performance comparé aux cours réels (déjà partiellement implémenté)
- Export CSV / Excel
- Webhook Discord
- Analyse ML des patterns de trading avant annonces

---

## Lancement local

```bash
pip install -r requirements.txt
cp .env.example .env   # remplir TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID

# Test sans appels API
MOCK_DATA=true python main.py

# Production
python main.py
```

Dashboard disponible sur http://localhost:8000
