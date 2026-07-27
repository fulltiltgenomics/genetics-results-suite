# Nginx + OAuth2 Proxy Setup for Genetics Results Suite

> **superseded — historical record, do not follow for a new deployment.** this is the original
> single-VM setup: nginx on the host, TLS from certbot/Let's Encrypt, oauth2-proxy under systemd.
> the suite is now deployed to GKE with `./scripts/deploy.sh`, which applies the manifests in
> `k8s/` and generates the Ingress plus a Google `ManagedCertificate` from the `domains` list; the
> `auth_request` nginx layer runs in-cluster as `k8s/deployments/auth-gateway.yaml`. see
> [project-spec.md](project-spec.md) and the repository README for the current architecture.
> one piece of this era is still live: the `finngenie` → `genegenie` 301 redirect, now rendered
> into the in-cluster auth-gateway by `deploy.sh` — see
> [genegenie-migration.md](genegenie-migration.md).

Replicating the dev.finngen.fi reverse-proxy configuration on a fresh Google VM.

## Architecture

```
Internet → :443 (nginx, TLS) → oauth2-proxy (:4180) for auth
                               ├─ /            → genetics-results-browser  (:3000)
                               ├─ /api/        → genetics-results-api      (:2000)
                               └─ /chat/       → genetics-mcp-server       (:4000, SSE)
```

All routes require Google OAuth2 login. Nginx terminates TLS with Let's Encrypt certs.

## 1. Install Nginx

```bash
sudo apt update && sudo apt install -y nginx
sudo systemctl enable nginx
```

## 2. Install Certbot and Get TLS Certificate

Replace `YOUR_DOMAIN` with the actual hostname throughout.

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d YOUR_DOMAIN
```

Certbot will create certs under `/etc/letsencrypt/live/YOUR_DOMAIN/` and set up auto-renewal.

## 3. Install oauth2-proxy

```bash
OAUTH2_PROXY_VERSION=7.7.1
wget https://github.com/oauth2-proxy/oauth2-proxy/releases/download/v${OAUTH2_PROXY_VERSION}/oauth2-proxy-v${OAUTH2_PROXY_VERSION}.linux-amd64.tar.gz
tar xzf oauth2-proxy-v${OAUTH2_PROXY_VERSION}.linux-amd64.tar.gz
sudo cp oauth2-proxy-v${OAUTH2_PROXY_VERSION}.linux-amd64/oauth2-proxy /usr/local/bin/
```

## 4. Configure oauth2-proxy

Create `/etc/oauth2-proxy/config.cfg`:

```ini
provider = "google"
client_id = "<GOOGLE_OAUTH_CLIENT_ID>"
client_secret = "<GOOGLE_OAUTH_CLIENT_SECRET>"
cookie_secret = "<RANDOM_32_BYTE_BASE64>"

email_domains = ["finngen.fi"]
authenticated_emails_file = "/etc/oauth2-proxy/whitelist.txt"

http_address = "127.0.0.1:4180"
redirect_url = "https://YOUR_DOMAIN/oauth2/callback"

set_xauthrequest = true
upstreams = ["static://200"]

cookie_secure = true
cookie_httponly = true
cookie_samesite = "lax"
cookie_name = "_oauth2_proxy"
cookie_expire = "168h"
silence_ping_logging = true
```

Generate a cookie secret:

```bash
python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
```

Create the whitelist file with one email per line for non-finngen.fi users:

```bash
sudo mkdir -p /etc/oauth2-proxy
sudo touch /etc/oauth2-proxy/whitelist.txt
```

## 5. Create oauth2-proxy Systemd Service

Create `/etc/systemd/system/oauth2-proxy.service`:

```ini
[Unit]
Description=oauth2-proxy
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/oauth2-proxy --config=/etc/oauth2-proxy/config.cfg
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oauth2-proxy
```

## 6. Configure Nginx Virtual Host

Remove the default site and create `/etc/nginx/sites-available/genetics-results-browser`:

```bash
sudo rm /etc/nginx/sites-enabled/default
```

```nginx
server {
    server_name YOUR_DOMAIN;

    # must be set at server level, not per-location: the auth_request subrequest is evaluated
    # against its own location's limit, so a location-level value is still defeated by the 1m
    # default inheriting into /oauth2/auth (chat messages with a base64 image attached exceed 1m,
    # and auth_request turns the resulting 413 into an opaque 500)
    client_max_body_size 50M;

    # oauth2-proxy endpoints
    location /oauth2/ {
        proxy_pass http://127.0.0.1:4180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /oauth2/auth {
        internal;
        proxy_pass http://127.0.0.1:4180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Content-Length "";
        proxy_pass_request_body off;
    }

    location @oauth2_login {
        return 302 https://$host/oauth2/start?rd=$request_uri;
    }

    # static assets (no auth needed, served directly)
    location /static/ {
        alias /var/www/new/genetics-results-browser/static/;
        expires 30d;
        access_log off;
        gzip_static on;
    }

    # genetics-results-api
    location /api/ {
        auth_request /oauth2/auth;
        auth_request_set $email $upstream_http_x_auth_request_email;
        proxy_set_header X-Goog-Authenticated-User-Email "accounts.google.com:$email";
        error_page 401 = @oauth2_login;

        proxy_pass http://localhost:2000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
    }

    # genetics-mcp-server (chat) — SSE streaming
    location /chat/ {
        auth_request /oauth2/auth;
        auth_request_set $email $upstream_http_x_auth_request_email;
        proxy_set_header X-Goog-Authenticated-User-Email "accounts.google.com:$email";
        error_page 401 = @oauth2_login;

        proxy_pass http://localhost:4000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
    }

    # genetics-results-browser frontend
    location / {
        auth_request /oauth2/auth;
        auth_request_set $email $upstream_http_x_auth_request_email;
        proxy_set_header X-Goog-Authenticated-User-Email "accounts.google.com:$email";
        error_page 401 = @oauth2_login;

        proxy_pass http://localhost:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # TLS — certbot will fill these in, or copy from below
    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/YOUR_DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/YOUR_DOMAIN/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

# HTTP → HTTPS redirect
server {
    listen 80;
    server_name YOUR_DOMAIN;
    return 301 https://$host$request_uri;
}
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/genetics-results-browser /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 7. GCP Firewall

Ensure the VM's network allows inbound TCP on ports 80 and 443. In the GCP console or via gcloud:

```bash
gcloud compute firewall-rules create allow-http-https \
  --allow tcp:80,tcp:443 \
  --target-tags=<YOUR_VM_NETWORK_TAG> \
  --description="Allow HTTP and HTTPS"
```

## 8. DNS

Point `YOUR_DOMAIN` to the VM's external IP with an A record.

## Service Ports Summary

| Service                    | Port | Notes              |
|----------------------------|------|--------------------|
| genetics-results-browser   | 3000 | Next.js frontend   |
| genetics-results-api       | 2000 | Python API          |
| genetics-mcp-server (chat) | 4000 | FastAPI + SSE       |
| oauth2-proxy               | 4180 | localhost only      |
