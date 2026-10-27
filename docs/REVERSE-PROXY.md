# Reverse proxy and authentication

Sentinel listens on `127.0.0.1:9765` and nothing else by default. That is deliberate: the
dashboard shows hostnames, ports, versions and error lines from your server. It also asks for its
own **dashboard login** (`server.login`: one username and password, created by
`./sentinel.sh install` or `./sentinel.sh set-password`, see
[CONFIG.md](CONFIG.md#the-dashboard-login-serverlogin)): the page opens on "Sign in to DSpace
Sentinel" wherever you reach it from. To look at it from your laptop you have three good options,
in order of preference.

## Option 1: SSH tunnel (no configuration at all)

```bash
ssh -L 9765:127.0.0.1:9765 admin@repo.example.org
# then open http://localhost:9765 and sign in
```

Nothing is exposed, nothing to maintain. Fine for one or two operators. The password only
travels inside the SSH connection.

A port on `localhost` is not automatically private to you: a web page you visit in another tab
can point its own DNS name at `127.0.0.1` (DNS rebinding) and have your browser call Sentinel
as if it were that site. Sentinel therefore answers only to host names it knows (IP addresses,
`localhost`, the machine's names, the hosts of the DSpace and nginx URLs in its config and
`server.allowed_hosts`; anything else gets HTTP 400), and it refuses writes that a browser marks
as cross-site (HTTP 403) or that carry a form instead of JSON (HTTP 415). The tunnel is safe to
leave open while you browse elsewhere.

## Option 2: nginx in front with HTTPS (recommended for a team)

Your DSpace server already runs nginx (or Apache) for the repository, usually with a
Let's Encrypt certificate. Serve Sentinel under a path of that HTTPS site: the dashboard login
then travels encrypted, and port 9765 stays closed.

With the dashboard login set, nginx's own basic auth is optional. The shipped snippet keeps it as
a second gate in front of the sign-in page (and the only one for a Sentinel without a login); to
rely on the login alone, skip step 1 and delete the two `auth_basic` lines from the snippet.

### 1. Create a password file (optional with the login)

```bash
sudo apt install apache2-utils            # provides htpasswd
sudo htpasswd -c /etc/nginx/.dspace-sentinel-htpasswd admin
sudo chmod 640 /etc/nginx/.dspace-sentinel-htpasswd && sudo chown root:www-data /etc/nginx/.dspace-sentinel-htpasswd
```

Add more users with `htpasswd /etc/nginx/.dspace-sentinel-htpasswd alice` (without `-c`). The
path is the one the example snippet and the Ansible role use; any path works as long as
`auth_basic_user_file` points at it.

### 2. Add the location

Save [`deploy/nginx/sentinel.conf.example`](../deploy/nginx/sentinel.conf.example) as
`/etc/nginx/snippets/dspace-sentinel.conf` and include it inside the `server { … }` block of
your repository site (typically `/etc/nginx/sites-available/repo.example.org`, the HTTPS one)
with `include /etc/nginx/snippets/dspace-sentinel.conf;`. This is what it contains:

```nginx
location = /sentinel {
    return 301 /sentinel/;
}

location /sentinel/ {
    auth_basic           "DSpace Sentinel";
    auth_basic_user_file /etc/nginx/.dspace-sentinel-htpasswd;

    proxy_pass         http://127.0.0.1:9765/;      # trailing slash strips the prefix
    proxy_http_version 1.1;
    proxy_set_header   Host               $host;
    proxy_set_header   X-Real-IP          $remote_addr;
    proxy_set_header   X-Forwarded-For    $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto  $scheme;
    proxy_set_header   X-Forwarded-Prefix /sentinel;
    proxy_set_header   Connection         "";
    proxy_redirect     off;

    # Server-sent events (/sentinel/api/stream) need an unbuffered, long-lived connection.
    proxy_buffering       off;
    proxy_cache           off;
    proxy_read_timeout    1h;
    proxy_send_timeout    1h;
    gzip                  off;
    client_max_body_size  1m;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Open `https://repo.example.org/sentinel/` (with the trailing slash). With `auth_basic` in the
snippet the browser asks for the basic-auth credentials once per session; then Sentinel's sign-in
page asks for the dashboard login.

The snippet's `X-Forwarded-Proto $scheme` and `X-Forwarded-For` headers matter for the login.
Sentinel runs uvicorn with proxy headers on, and uvicorn trusts them from a proxy on the same
machine (nginx connects from `127.0.0.1`): the request then counts as HTTPS, so the session cookie gets `Secure`,
and each browser's own address is used for the sign-in limits and the events log ("Signed in:
admin (192.0.2.10)"). nginx strips the `/sentinel/` prefix, so Sentinel sees its root path as
`/` and sets the cookie for `Path=/` of the repository's host; it is `HttpOnly` and
`SameSite=Strict`, and DSpace ignores it.

Why it works without extra settings: every URL in the UI and the API is **relative**
(`api/status`, `static/js/app.js`, `EventSource('api/stream')`), so a path prefix needs no
configuration on Sentinel's side. Sentinel also sends `X-Accel-Buffering: no` on the SSE
response, which nginx honours even if you forget `proxy_buffering off`.

The snippet forwards `Host $host`, so requests reach Sentinel under the repository's name.
That name is accepted when `dspace.public_url` (or `nginx.https_url`, `ssl.host`) names it, or
when it is the machine's hostname; discovery fills in `public_url` from `dspace.ui.url` when that
is an `https://` URL. Otherwise add the name to `server.allowed_hosts` in `config/sentinel.yaml`
and restart, or every request answers `400 {"error": "unknown host 'repo.example.org': …"}`.

### Subdomain instead of a path

```nginx
server {
    listen 443 ssl http2;
    server_name sentinel.example.org;
    ssl_certificate     /etc/letsencrypt/live/sentinel.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sentinel.example.org/privkey.pem;

    location / {
        auth_basic           "DSpace Sentinel";
        auth_basic_user_file /etc/nginx/.dspace-sentinel-htpasswd;
        proxy_pass         http://127.0.0.1:9765/;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Connection        "";
        proxy_buffering    off;
        proxy_read_timeout 1h;
    }
}
```

The same block is at the bottom of `deploy/nginx/sentinel.conf.example`, commented out. A
dedicated name is not one Sentinel knows, so add it to `config/sentinel.yaml` and restart:

```yaml
server:
  allowed_hosts: [sentinel.example.org]
```

With Ansible, `dspace_sentinel_nginx_snippet_enabled: true` writes the path-prefix snippet and
the htpasswd file for you ([ANSIBLE.md](ANSIBLE.md#optional-nginx-snippet)); the role has no
variable for `server.allowed_hosts`, so prefer the path prefix there.

### Restrict by network as well

```nginx
location /sentinel/ {
    allow 10.0.0.0/8;          # office / VPN
    allow 203.0.113.7;         # your home
    deny  all;
    auth_basic "DSpace Sentinel";
    …
}
```

### Apache httpd

```apache
<Location "/sentinel/">
    AuthType Basic
    AuthName "DSpace Sentinel"
    AuthUserFile /etc/apache2/sentinel.htpasswd
    Require valid-user
    ProxyPass        http://127.0.0.1:9765/ flushpackets=on timeout=3600
    ProxyPassReverse http://127.0.0.1:9765/
</Location>
```

`a2enmod proxy proxy_http` first. `flushpackets=on` keeps SSE flowing. Without
`ProxyPreserveHost On`, Apache sends `Host: 127.0.0.1:9765` (always accepted) and the browser's
host in `X-Forwarded-Host`, which Sentinel's cross-site check compares with the `Origin` header.

## Option 3: Sentinel listens on the network itself

If you cannot put a web server in front, Sentinel can listen on another address itself once the
dashboard login is set:

```yaml
server:
  host: 0.0.0.0          # or a specific interface address
  port: 9765
```

```bash
./sentinel.sh set-password       # if there is no login yet
./sentinel.sh restart
```

Every page then opens on the sign-in screen. This is **plain HTTP**: the password crosses the
network unencrypted unless you terminate TLS elsewhere, so only do it on a trusted network or a
VPN. `./sentinel.sh install` says so in its summary ("Sentinel binds to 0.0.0.0:9765 and asks for
the dashboard login. Put the nginx HTTPS recipe (deploy/nginx/sentinel.conf.example) in front so
the password never travels in plain text."), and so does the Security page's *Sentinel* check.

Without a login, `server.auth_token` also lets Sentinel listen there, as before: every request
except `/healthz` (and the sign-in routes `/api/session` and `/api/login`) then needs HTTP Basic
auth with **any user name** and the token as password, and the browser's own pop-up is the login
screen:

```bash
echo "SENTINEL_AUTH_TOKEN=$(openssl rand -hex 24)" >> config/sentinel.env && chmod 600 config/sentinel.env
./sentinel.sh restart
```

`server.*` cannot be changed through the API (`server.timezone`, the zone of the times in
messages, aside): edit `config/sentinel.yaml` or `config/sentinel.env` and restart to change the
address or the token; until the new token is used the API answers `401` with
`WWW-Authenticate: Basic realm="DSpace Sentinel"`. With a login set the token stays a token for
scripts (`Authorization: Bearer <token>`, or Basic as above), and a browser without a session gets
`401 {"error": "login required"}` without that header, so it shows the sign-in page instead of a
pop-up.

Sentinel refuses to start on a non-loopback address with neither a login nor a token: `serve`,
`start` and the systemd unit exit with code 4 and print "refusing to serve the API without
authentication on 0.0.0.0:9765: create the dashboard login with ./sentinel.sh set-password, set
server.auth_token (or SENTINEL_AUTH_TOKEN), …", and `./sentinel.sh install --service` stops
before installing such a unit. If a firewall or an authenticating proxy on another machine
already guards the port, opt out explicitly with `server.allow_unauthenticated: true` or
`SENTINEL_ALLOW_OPEN=1`. Opened by address (`http://192.0.2.10:9765`) Sentinel always answers;
opened by name, the name must be one it knows (see Option 1), so add it to
`server.allowed_hosts` if it is not.

You can combine options. nginx with `auth_basic` in front of the login asks twice: nginx's
password, then the sign-in page (Sentinel ignores the `Authorization` header nginx passes on,
unless it carries `server.auth_token`). nginx with `auth_basic` in front of a token without a
login: both checks see the same credentials, so use the token as the htpasswd password (Sentinel
ignores the user name), or drop one of the two. A script that goes through nginx's `auth_basic`
can only send one `Authorization` header: make its htpasswd password the token and use
`curl -u any:<token>`.

## Firewall reminder

Whatever you choose, keep 9765 closed from the outside:

```bash
sudo ufw status                    # 9765 should not be listed
sudo ss -ltnp | grep 9765          # should show 127.0.0.1:9765, not 0.0.0.0:9765
```

The **Ports** module warns you if Solr (8983) or PostgreSQL (5432) listen publicly; add
`{port: 9765, label: Sentinel, expect: local_only}` to `ports` to have it watch itself.

The **Security** module watches Sentinel too: its *Sentinel* check reads "listens on 0.0.0.0
with a login" (ok, with the hint "Put the nginx HTTPS recipe in front of Sentinel so the password
never travels in plain text.") or "listens on 0.0.0.0 with a password" (ok, a token and no login),
warns "listens on 0.0.0.0 without a password (server.allow_unauthenticated)" when Sentinel listens
on another address than 127.0.0.1 with neither, and its tile shows *Listens on*, *Login* and
*Token*. Its *Firewall* check says whether ufw, nftables or firewalld is active on this host (it
cannot see a firewall in your network).

## Checks

With the dashboard login and no `auth_basic`:

```bash
curl -I https://repo.example.org/sentinel/                     # 200: the page (it asks for the login)
curl -s https://repo.example.org/sentinel/api/session          # {"login_required": true, "authenticated": false, …}
curl -s https://repo.example.org/sentinel/api/meta             # 401 {"error": "login required"}
curl -s -H "Authorization: Bearer $SENTINEL_AUTH_TOKEN" https://repo.example.org/sentinel/api/meta       # JSON, when server.auth_token is set
curl -N -H "Authorization: Bearer $SENTINEL_AUTH_TOKEN" https://repo.example.org/sentinel/api/stream     # ": connected" then "event: status" at once
```

With nginx's `auth_basic` (its password equal to the token when scripts go through it):

```bash
curl -I  https://repo.example.org/sentinel/                    # 401 from nginx without credentials
curl -u admin https://repo.example.org/sentinel/api/meta       # JSON (with a login, only when the password is the token)
curl -N -u admin https://repo.example.org/sentinel/api/stream  # ": connected" then "event: status" at once
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| The page loads but never updates; browser console shows the EventSource reconnecting | the proxy buffers SSE: add `proxy_buffering off; proxy_read_timeout 1h;` (the UI silently falls back to polling every 15 s, so data still refreshes). |
| 404 for `static/js/app.js` behind a prefix | you opened `/sentinel` without the trailing slash, or `proxy_pass` lacks its trailing slash. |
| 502 Bad Gateway | Sentinel is not running: `./sentinel.sh status`, `./sentinel.sh logs`. |
| Basic auth prompt loops | with both nginx auth and `auth_token` set and no login, the browser sends one set of credentials to both: make the htpasswd password equal the token, or drop one of the two (or create the dashboard login with `./sentinel.sh set-password`, which needs no pop-up). |
| Everyone gets "Too many attempts" together, and the events log shows one address for all sign-ins | the proxy runs on another machine, so Sentinel sees its address: put the proxy on the same machine, or set `FORWARDED_ALLOW_IPS=<the proxy's address>` in `config/sentinel.env` (uvicorn then trusts that proxy's `X-Forwarded-For` and `X-Forwarded-Proto`) and restart. |
| `400 {"error": "unknown host '…'"}` | Sentinel does not know the name in `Host`: set `dspace.public_url` to the repository URL, or add the name to `server.allowed_hosts`, then restart. |
| `403 cross-site request refused` when saving through the proxy | the proxy changes `Host` without passing the browser's host on: forward `Host $host` (nginx) or keep the default `X-Forwarded-Host` (Apache). |
| Sentinel does not start after setting `host: 0.0.0.0` | it refuses an exposed port with neither a login nor `auth_token`: run `./sentinel.sh set-password` (or set a token, or `allow_unauthenticated: true` if a firewall guards it). `./sentinel.sh logs` shows the reason. |
| Mixed-content warnings | set `X-Forwarded-Proto $scheme`; Sentinel runs uvicorn with `proxy_headers=True`. |
| Report opens as a download instead of a tab | keep the `Content-Disposition` header untouched (`proxy_hide_header` must not list it). |
