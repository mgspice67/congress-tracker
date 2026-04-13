# 🏛️ Congress Stock Tracker

Surveillance en temps réel des transactions boursières des sénateurs et représentants américains.  
Alertes **Telegram** + tableau de bord web.

---

## ✨ Fonctionnalités

| Feature | Détail |
|---|---|
| 🔔 Notifications Telegram | Alerte dès qu'un élu déclare un trade |
| 📋 Feed en temps réel | Toutes les transactions avec filtres |
| 🏆 Classement | Les élus les plus actifs en bourse |
| 📊 Graphiques | Tendances achats/ventes sur 30 jours |
| 👤 Profils | Détail de chaque élu + son historique |
| 🗃️ Base de données | Historique complet persisté localement |

---

## 🚀 Installation locale (5 minutes)

### 1. Prérequis
- Python 3.11+
- Un compte Telegram

### 2. Installer les dépendances
```bash
git clone <ton-repo>
cd congress-tracker
python -m venv venv
source venv/bin/activate     # Windows : venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Créer ton bot Telegram

1. Ouvre Telegram → cherche **@BotFather**
2. Tape `/newbot`, donne un nom (ex: "My Congress Tracker")
3. Copie le **token** (ex: `7123456789:AAF...`)
4. Envoie n'importe quel message à ton nouveau bot
5. Ouvre dans ton navigateur :  
   `https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates`  
   → cherche `"chat":{"id": XXXXXXX}` → copie ce numéro

### 4. Configurer
```bash
cp .env.example .env
```
Ouvre `.env` et remplis :
```
TELEGRAM_BOT_TOKEN=7123456789:AAF...
TELEGRAM_CHAT_ID=123456789
```

### 5. Lancer
```bash
# Test avec données fictives (pas d'appel API)
MOCK_DATA=true python main.py

# Production (données réelles)
python main.py
```

Ouvre http://localhost:8000 → dashboard disponible immédiatement ✅

---

## ☁️ Déploiement sur Render.com (gratuit, 0 € / mois)

> Render permet d'héberger une appli Python gratuitement avec un lien HTTPS public.

### Étapes

1. **Crée un compte** sur https://render.com (gratuit)

2. **Push ton code sur GitHub** :
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   # Crée un repo sur github.com puis :
   git remote add origin https://github.com/TON_USER/congress-tracker.git
   git push -u origin main
   ```

3. **Nouveau service sur Render** :
   - Dashboard Render → *New* → *Web Service*
   - Connecte ton repo GitHub
   - Runtime : **Python 3**
   - Build Command : `pip install -r requirements.txt`
   - Start Command : `python main.py`

4. **Variables d'environnement** (dans l'onglet *Environment*) :
   ```
   TELEGRAM_BOT_TOKEN   = ton_token
   TELEGRAM_CHAT_ID     = ton_chat_id
   POLL_INTERVAL_MINUTES = 15
   ```

5. **Deploy** → en 2 minutes ton URL est disponible (ex: `https://congress-tracker-xxxx.onrender.com`)

> ⚠️ Sur le plan gratuit Render, le service se met en veille après 15 min d'inactivité.  
> Pour rester actif en permanence, ajoute un service de ping externe (UptimeRobot – gratuit) qui appelle `/health` toutes les 5 min.

---

## 📡 Sources de données

### Capitol Trades (primaire – aucune clé requise)
Le bot interroge l'API publique de [capitoltrades.com](https://capitoltrades.com) qui agrège les déclarations officielles.

### Quiver Quantitative (backup)
Si Capitol Trades ne répond pas, le bot bascule sur [Quiver Quantitative](https://www.quiverquant.com/).  
Clé gratuite disponible après inscription sur leur site.  
Ajoute `QUIVER_API_KEY=ta_clé` dans `.env`.

### Délai réglementaire
Le **STOCK Act** oblige les élus à déclarer leurs transactions sous **45 jours**.  
En pratique, beaucoup déclarent sous 15–30 jours. Le bot ne peut donc pas être "instantané" — c'est une contrainte légale, pas technique.

---

## 📱 Pourquoi pas Signal ?

Signal n'a pas d'API officielle pour les bots. La seule solution (`signal-cli`) nécessite :
- Un numéro de téléphone dédié
- Java 17+
- Une procédure d'enregistrement manuelle complexe

Telegram offre exactement la même expérience mobile (notifications push natives) avec une API officielle gratuite.

---

## 🗂️ Structure du projet

```
congress-tracker/
├── main.py          → Point d'entrée, lifecycle de l'app
├── config.py        → Variables d'environnement
├── database.py      → Couche SQLite (aiosqlite)
├── fetcher.py       → Appels API Capitol Trades + Quiver
├── notifier.py      → Formatage + envoi Telegram
├── scheduler.py     → Polling toutes les X minutes
├── dashboard.py     → Serveur FastAPI + routes JSON
├── templates/
│   └── index.html   → Dashboard web (Tailwind + Chart.js)
├── requirements.txt
├── Procfile         → Déploiement Render/Railway
└── .env.example     → Template de configuration
```

---

## 🔧 Variables d'environnement

| Variable | Requis | Défaut | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Token BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Ton ID Telegram |
| `QUIVER_API_KEY` | Non | — | Clé Quiver Quant (backup) |
| `POLL_INTERVAL_MINUTES` | Non | 15 | Fréquence de polling |
| `PORT` | Non | 8000 | Port du serveur web |
| `DATABASE_PATH` | Non | trades.db | Chemin de la BDD SQLite |
| `MOCK_DATA` | Non | false | Données fictives pour test |

---

## 💡 Évolutions possibles

- [ ] Alertes sur des élus spécifiques (ex: "ne me notifier que pour Pelosi")
- [ ] Alertes sur des tickers spécifiques (ex: "alerte si quelqu'un achète NVDA")
- [ ] Score de performance : comparer les trades d'un élu aux cours réels
- [ ] Export CSV / Excel
- [ ] Webhook Discord en parallèle de Telegram
- [ ] Analyse ML des patterns (avant quelles annonces achètent-ils ?)
