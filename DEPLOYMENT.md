# SmartGold AI Bridge — VPS Deployment Guide

Step-by-step instructions to deploy the AI bridge on a fresh Ubuntu
22.04 / 24.04 VPS (any provider: Contabo, Hetzner, DigitalOcean, AWS EC2,
Exness dedicated, etc.). Total setup time: ~15 minutes.

---

## 0. Prerequisites

- **VPS**: Ubuntu 22.04+ with ≥ 1 GB RAM, 1 vCPU, 10 GB disk
- **Domain**: e.g. `sga.example.com` — DNS A record pointing at the VPS
  public IP (needed for automatic HTTPS via Caddy)
- **MiniMax API key**: from <https://www.minimax.io/> (MiniMax Open Platform)
- **Telegram bot** *(optional)*:
  - Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
  - Talk to [@userinfobot](https://t.me/userinfobot) → copy your chat id
- **NewsAPI key** *(optional)*: free tier at <https://newsapi.org/>

---

## 1. Prepare the server

SSH in as root (or a sudo user), then:

```bash
# update + core utilities
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-plugin git curl ufw

# firewall: allow ssh + https, deny rest
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

# enable docker at boot
sudo systemctl enable --now docker
```

Verify: `docker compose version` should print `Docker Compose version v2.x`.

---

## 2. Install Caddy (HTTPS reverse proxy)

Caddy auto-provisions Let's Encrypt certificates — no manual cert mgmt.

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

---

## 3. Clone the repository

```bash
sudo useradd -m -s /bin/bash smartgold
sudo -u smartgold -H bash -c '
  cd ~ && git clone https://github.com/fataakromulmuttaqin/smart_gold_Analyzer.git
  cd smart_gold_Analyzer && git checkout master
'
cd /home/smartgold/smart_gold_Analyzer/ai_bridge
```

---

## 4. Configure `.env`

```bash
sudo -u smartgold cp .env.example .env
sudo -u smartgold nano .env    # or vim / your favourite editor
```

Fill in at minimum:

```bash
# Generate: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
WEBHOOK_SECRET=PUT_YOUR_GENERATED_SECRET_HERE

MINIMAX_API_KEY=your_minimax_key_here
MINIMAX_MODEL=MiniMax-M2

# Optional but recommended
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
NEWSAPI_KEY=abcdef...

# Risk tuning
MIN_CONFIDENCE=0.60
SIGNAL_COOLDOWN_SECONDS=60
```

**Save this `WEBHOOK_SECRET` — you'll paste it into TradingView too.**

---

## 5. Build and run the bridge

```bash
cd /home/smartgold/smart_gold_Analyzer/ai_bridge
sudo -u smartgold mkdir -p data
sudo docker compose -f docker/docker-compose.yml up -d --build
sudo docker compose -f docker/docker-compose.yml logs -f --tail 50
```

You should see something like:

```
smartgold-ai-bridge | SmartGold AI Bridge starting (env=production, model=MiniMax-M2, mock=False)
smartgold-ai-bridge | SignalLog schema ready at /app/data/signals.db
smartgold-ai-bridge | Application startup complete.
smartgold-ai-bridge | Uvicorn running on http://0.0.0.0:8080
```

Ctrl-C to exit the logs (the container keeps running).

---

## 6. Configure Caddy

```bash
# Edit the provided Caddyfile with your domain
sudo cp /home/smartgold/smart_gold_Analyzer/ai_bridge/docker/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/sga.example.com/YOUR_DOMAIN_HERE/' /etc/caddy/Caddyfile

sudo systemctl reload caddy
sudo journalctl -u caddy -f     # watch for "certificate obtained successfully"
```

Ctrl-C when you see the TLS cert being issued.

---

## 7. Verify the deployment

From your laptop:

```bash
# Health check
curl -sS https://YOUR_DOMAIN/health | python3 -m json.tool

# Full smoke test (from your local copy of the repo)
cd smart_gold_Analyzer/ai_bridge
export WEBHOOK_SECRET="<same as in .env on VPS>"
python3 scripts/smoke_test.py --url https://YOUR_DOMAIN
```

Expected output ends with `[SMOKE TEST PASSED]`.

Then open **`https://YOUR_DOMAIN/`** (or `/ui/`) in your browser — the
dashboard should load with the summary cards, filters, and signals table
(empty until TradingView starts firing alerts). It auto-refreshes every
30 seconds.

If Telegram is configured, you should also receive a message in your bot
(only when decision is `execute` or `reduce`; mock mode returns `skip`
so nothing is sent by default — set `LLM_MOCK_MODE=false` and provide a
real MiniMax key to get real decisions).

---

## 8. Wire up TradingView

1. In TradingView, load `pinescript/smart_gold_analyzer_v2_ai.pine` into
   the **Pine Editor**, add to chart.
2. Open indicator settings → **🤖 AI Bridge (Webhook)** group:
   - Enable AI bridge webhook alerts = **on**
   - Webhook Secret = *paste the same* `WEBHOOK_SECRET` *from `.env`*
3. Click the **🔔** icon on the chart → **Create alert**.
4. **Condition**: your indicator → **Any alert() function call**.
5. **Notifications** tab:
   - ☑ **Webhook URL** → `https://YOUR_DOMAIN/webhook/tradingview`
   - Leave **Message** empty.
6. Save. You're live.

See [`pinescript/ALERT_PAYLOAD.md`](pinescript/ALERT_PAYLOAD.md) for the
exact JSON schema TradingView sends.

---

## 9. Day-2 operations

### Dashboard

Visit `https://YOUR_DOMAIN/` — you'll see the SmartGold dashboard with:
- Summary cards (total, execute/reduce/skip counts, avg confidence, notified)
- A filterable, paginated signals table (action, symbol, time window)
- Click any row for the full detail (LLM reasoning, macro snapshot, raw payload)

The dashboard auto-polls the API every 30 s; it's powered by three
read-only endpoints you can also script against:

```
GET /api/stats?hours=24
GET /api/signals?limit=50&action=execute&symbol=XAUUSD
GET /api/signals/{id}
```

> ⚠️ The dashboard is **unauthenticated** by default. If your VPS is
> public, protect it with Caddy basic auth — the stanza is commented in
> `docker/Caddyfile`. Generate a hash with `caddy hash-password` and
> uncomment the `@dashboard` + `basic_auth` block.

### Logs

```bash
# AI bridge (container)
sudo docker compose -f docker/docker-compose.yml logs -f --tail 100

# Caddy / HTTPS
sudo journalctl -u caddy -f
```

### Inspect the audit log

```bash
sudo -u smartgold sqlite3 data/signals.db \
  "SELECT received_at, signal, decision_action, decision_conf FROM signals ORDER BY id DESC LIMIT 20;"
```

### Updating

```bash
cd /home/smartgold/smart_gold_Analyzer
sudo -u smartgold git pull
cd ai_bridge
sudo docker compose -f docker/docker-compose.yml up -d --build
```

### Restart / stop

```bash
sudo docker compose -f docker/docker-compose.yml restart
sudo docker compose -f docker/docker-compose.yml down
```

### Tuning risk policy (no rebuild needed)

Edit `.env`, change `MIN_CONFIDENCE` / `SIGNAL_COOLDOWN_SECONDS`, then:

```bash
sudo docker compose -f docker/docker-compose.yml up -d
```

---

## 10. Troubleshooting

| Symptom                         | Likely cause                                           | Fix                                                                              |
|---------------------------------|--------------------------------------------------------|----------------------------------------------------------------------------------|
| `curl /health` → Connection refused | Container not running                              | `docker compose ps` → `docker compose up -d`                                     |
| TV alert fires but 401 returned | `secret` mismatch                                      | Copy `WEBHOOK_SECRET` from `.env` into indicator settings exactly                |
| 422 Unprocessable Entity        | Pine script edited, broke JSON                         | Revert `pinescript/smart_gold_analyzer_v2_ai.pine` to repo version              |
| 429 Too Many Requests           | Cooldown                                               | Expected; lower `SIGNAL_COOLDOWN_SECONDS` if you want more throughput           |
| MiniMax 401 in logs             | Bad / revoked API key                                  | Re-issue key at MiniMax console, update `.env`, `docker compose up -d`          |
| MiniMax timeout                 | Network to `api.minimax.io` slow                       | Increase `MINIMAX_TIMEOUT` (default 30s)                                        |
| Every decision = `skip`         | Confidence below `MIN_CONFIDENCE`                      | Lower threshold (e.g. 0.5) OR review prompts; start by checking the `reasoning` |
| Caddy fails to get cert         | DNS not pointing at VPS / firewall blocks 80          | `dig YOUR_DOMAIN`, check `ufw status`, retry `sudo systemctl reload caddy`      |
| Telegram message not delivered  | Wrong chat id, bot not started                         | In Telegram, open the bot and send `/start`; verify `TELEGRAM_CHAT_ID`          |

### Safe mode (disable real LLM calls)

Set `LLM_MOCK_MODE=true` in `.env` and `docker compose up -d`. The bridge
will stop calling MiniMax and return a canned `skip` — useful for
testing the plumbing end-to-end without burning tokens.

---

## 11. Cost & capacity estimate

- MiniMax-M2 pricing (approx — check current rates):
  - Input ~$0.30/M tokens, Output ~$1.20/M tokens
  - Each decision ≈ 1,200 input + 200 output tokens ≈ $0.0006
  - 500 signals/month → well under $1/month
- VPS: Contabo / Hetzner cheapest tier (~$5/mo) is more than enough.
- SQLite grows roughly 2 KB per signal → negligible.

---

## 12. Security checklist

- [x] Firewall: only 22/80/443 open
- [x] AI bridge binds to `127.0.0.1` only (not exposed publicly)
- [x] HTTPS enforced via Caddy + HSTS header
- [x] Webhook uses a long random shared secret
- [x] Container runs as non-root user
- [x] `/docs` endpoint blocked by Caddy path whitelist
- [ ] **You should**: rotate `WEBHOOK_SECRET` every few months
- [ ] **You should**: keep `apt upgrade` and `docker compose pull` current

---

## Need help?

Open an issue on the repo:
<https://github.com/fataakromulmuttaqin/smart_gold_Analyzer/issues>
