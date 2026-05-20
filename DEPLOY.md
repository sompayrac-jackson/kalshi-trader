# Deploying to Digital Ocean

## Overview

```
GitHub repo (no secrets)  →  droplet  ←  .env file (secrets, never in git)
                                       ←  private_key.pem (uploaded manually, never in git)
```

Secrets are never committed. They are uploaded to the droplet separately.

---

## 1. Push code to GitHub (no secrets present)

Verify nothing sensitive is tracked before pushing:

```bash
# On your local machine — check for secrets before committing
git status
git diff --staged

# These files must NOT appear in git status:
#   .env, private_key.pem, *.jsonl, *.log
git push origin main
```

---

## 2. Set up the droplet

SSH in and clone the repo:

```bash
ssh root@YOUR_DROPLET_IP
apt update && apt install -y python3-pip nginx

cd /opt
git clone https://github.com/sompayrac-jackson/kalshi-trader.git kalshi_trader
cd kalshi_trader
pip3 install flask requests cryptography
```

---

## 3. Upload secrets (never via git)

From your **local machine**:

```bash
# Upload private key
scp private_key.pem root@YOUR_DROPLET_IP:/opt/kalshi_trader/private_key.pem

# Restrict permissions so only root can read it
ssh root@YOUR_DROPLET_IP "chmod 600 /opt/kalshi_trader/private_key.pem"
```

On the **droplet**, create the `.env` file:

```bash
cat > /opt/kalshi_trader/.env << 'EOF'
KALSHI_API_KEY=26fc222b-85f4-4a16-88e5-01e2aed2aa8e
ODDS_API_KEY=your-odds-api-key-here
PRIVATE_KEY_PATH=/opt/kalshi_trader/private_key.pem
DASHBOARD_USER=kalshi
DASHBOARD_PASS=choose-a-strong-password-here
EOF

chmod 600 /opt/kalshi_trader/.env
```

**Pick a real password for DASHBOARD_PASS** — anyone who authenticates can trigger LIVE orders.

---

## 4. Create a systemd service

```bash
cat > /etc/systemd/system/kalshi-dashboard.service << 'EOF'
[Unit]
Description=Kalshi Trader Dashboard
After=network.target

[Service]
User=root
WorkingDirectory=/opt/kalshi_trader
ExecStart=/usr/bin/python3 dashboard.py --port 5000
Restart=always
RestartSec=5
# Secrets are loaded from .env automatically by config.py

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kalshi-dashboard
systemctl start kalshi-dashboard
systemctl status kalshi-dashboard
```

---

## 5. Set up nginx as reverse proxy

```bash
cat > /etc/nginx/sites-available/kalshi << 'EOF'
server {
    listen 80;
    server_name YOUR_DROPLET_IP;

    # Hide Flask version header
    server_tokens off;

    location / {
        proxy_pass       http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Prevent the dashboard from being framed by other sites
        add_header X-Frame-Options DENY;
        add_header X-Content-Type-Options nosniff;
    }
}
EOF

ln -s /etc/nginx/sites-available/kalshi /etc/nginx/sites-enabled/
nginx -t
systemctl enable nginx
systemctl reload nginx
```

Open `http://YOUR_DROPLET_IP` — your browser will prompt for the username/password you set.

---

## 6. (Recommended) Add HTTPS with Let's Encrypt

Point a domain at your droplet IP first, then:

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d yourdomain.com
# Follow the prompts — certbot auto-updates the nginx config and sets up renewal
```

---

## Keeping the code updated

```bash
ssh root@YOUR_DROPLET_IP
cd /opt/kalshi_trader
git pull origin main
systemctl restart kalshi-dashboard
```

The `.env` and `private_key.pem` are not touched by `git pull` since they are gitignored.

---

## Useful commands

```bash
# Live log tail
journalctl -u kalshi-dashboard -f

# Restart after code changes
systemctl restart kalshi-dashboard

# Check nginx errors
journalctl -u nginx -f

# Verify .env and private key are NOT in git
git ls-files | grep -E "\.env|\.pem|\.jsonl"   # should print nothing
```
