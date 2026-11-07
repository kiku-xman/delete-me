# 🖥️ Serveur Debian sur GitHub Actions

SSH + Bureau à distance (XFCE/RDP) + Web (Nginx), exposés par des tunnels sans compte
(Pinggy pour SSH/RDP, Cloudflare pour le web). Les **nouvelles adresses sont envoyées sur Telegram**
à chaque ouverture ou renouvellement de tunnel.

## Structure

```
.
├── .github/workflows/server.yml   # workflow (lancement manuel)
├── server.py                      # installation + tunnels + boucle de renouvellement + Telegram
└── README.md
```

## Mise en place

1. **Créer le dépôt** et y pousser ces fichiers (public = minutes illimitées ; privé = ~2000 min/mois).
2. **Créer le bot Telegram** : parlez à [@BotFather](https://t.me/BotFather), `/newbot`, copiez le token.
3. **Trouver votre ID Telegram** : parlez à [@userinfobot](https://t.me/userinfobot).
4. **Envoyer `/start` à votre nouveau bot** (sinon il ne peut pas vous écrire).
5. Dans le dépôt : *Settings → Secrets and variables → Actions → New repository secret* :

| Secret | Valeur |
|---|---|
| `SERVER_PASSWORD` | mot de passe du serveur (long et fort) |
| `TELEGRAM_BOT_TOKEN` | token donné par BotFather |
| `TELEGRAM_USER_ID` | votre ID numérique |

6. *Actions → Serveur Debian → Run workflow*, choisissez les services, puis **Run**.
7. Après quelques minutes (XFCE est long à installer), le message Telegram arrive avec :
   - `ssh root@... -p ...`
   - l'adresse `hôte:port` pour `mstsc` (utilisateur `serveruser`, session Xorg)
   - l'URL `https://xxxx.trycloudflare.com`

## Comportement

- Le tunnel Pinggy gratuit expire à ~60 min : le script le **rouvre toutes les 55 min** (ou dès qu'il tombe) et envoie la **nouvelle adresse** sur Telegram. Les sessions SSH/RDP ouvertes sont coupées à ce moment-là.
- Le tunnel Cloudflare n'expire pas ; il est relancé s'il tombe.
- Le script s'arrête proprement à **345 min** (limite GitHub : 360 min) et prévient 10 min avant.
- `auto_restart` : à la fin, le workflow se relance seul (boucle continue). Annulez le run en cours pour arrêter.
- Annuler le job depuis l'onglet Actions arrête le serveur et envoie un message d'arrêt.
- Les adresses ne sont **pas écrites dans les logs** si Telegram fonctionne. Sans Telegram, elles s'affichent dans les logs (publics si dépôt public).
- Si `SERVER_PASSWORD` est vide, un mot de passe est généré et envoyé uniquement sur Telegram.

## Variables optionnelles (server.yml → `env:`)

`SERVER_USERNAME` (défaut `serveruser`), `ALLOW_ROOT_SSH` (`true`), `WEB_PORT` (`80`),
`RENEW_MINUTES` (`55`), `RUN_MINUTES` (`345`), `HEARTBEAT_MIN_SECONDS` (`30`) et `HEARTBEAT_MAX_SECONDS` (`60`) : un log de statut est affiché à intervalle aléatoire entre ces deux valeurs, avec un message différent (charge, RAM, disque, connexions, âge des tunnels...). Ce sont de vraies mesures, pas de faux messages.

## ⚠️ Avertissements

- Les conditions d'utilisation de GitHub limitent Actions aux usages liés au projet (CI/CD, tests, déploiement). Utiliser un runner comme serveur distant est une zone grise pouvant entraîner des restrictions du compte : testez sur un dépôt secondaire.
- Un SSH exposé sur Internet avec mot de passe attire des tentatives de connexion : utilisez un mot de passe très long.
