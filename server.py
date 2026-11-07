#!/usr/bin/env python3
"""
Serveur Debian/Ubuntu sur GitHub Actions : SSH + Bureau RDP (XFCE) + Web (Nginx).
Tunnels sans compte (Pinggy / Cloudflare), adresses envoyées sur Telegram.
Version adaptée du notebook Colab colab_debian_server_v2.ipynb.

Lancement (le runner n'est pas root) :  sudo -E python3 -u server.py
Toute la configuration passe par des variables d'environnement (voir server.yml).
"""
import os, re, sys, time, shutil, secrets, string, socket, signal, subprocess, html
import urllib.request, urllib.parse
from subprocess import DEVNULL, PIPE, STDOUT

# ======================== CONFIGURATION (variables d'environnement) ========================
def env_bool(name, default=True):
    v = os.environ.get(name, "").strip().lower()
    return default if v == "" else v in ("1", "true", "yes", "on")

SERVER_PASSWORD = os.environ.get("SERVER_PASSWORD", "")          # vide = généré et envoyé sur Telegram
USERNAME        = os.environ.get("SERVER_USERNAME", "serveruser")
ALLOW_ROOT_SSH  = env_bool("ALLOW_ROOT_SSH", True)
WEB_PORT        = int(os.environ.get("WEB_PORT", "80"))
ENABLE_SSH      = env_bool("ENABLE_SSH", True)
ENABLE_RDP      = env_bool("ENABLE_RDP", True)
ENABLE_WEB      = env_bool("ENABLE_WEB", True)
TG_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_USER         = os.environ.get("TELEGRAM_USER_ID", "").strip()
RUN_MINUTES     = int(os.environ.get("RUN_MINUTES", "345"))      # le job GitHub est limité à 360 min
RENEW_MINUTES   = int(os.environ.get("RENEW_MINUTES", "55"))     # Pinggy gratuit coupe à 60 min
# ===========================================================================================

LOG_DIR = "/tmp"
START   = time.time()
DEADLINE = START + RUN_MINUTES * 60
PROCS   = {}    # processus de tunnels en arrière-plan
TUNNELS = {}    # adresses publiques actuelles
STARTED = {}    # heure d'ouverture de chaque tunnel
PINGGY_PORTS = {}
if ENABLE_SSH: PINGGY_PORTS["ssh"] = 22
if ENABLE_RDP: PINGGY_PORTS["rdp"] = 3389


# ------------------------------- Utilitaires -------------------------------
def log(msg):
    print(msg, flush=True)

def sh(cmd, check=True, quiet=False):
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    p = subprocess.Popen(cmd, shell=True, executable="/bin/bash", stdout=PIPE, stderr=STDOUT, text=True, env=env)
    out = []
    for line in p.stdout:
        out.append(line)
        if not quiet:
            print(line, end="", flush=True)
    p.wait()
    if check and p.returncode != 0:
        raise RuntimeError(f"Commande échouée (code {p.returncode}) : {cmd}")
    return "".join(out)

def service(name, action="restart"):
    return subprocess.run(["service", name, action], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL).returncode == 0

def port_open(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0

def alive(name):
    p = PROCS.get(name)
    return p is not None and p.poll() is None

def stop_proc(name):
    p = PROCS.pop(name, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    TUNNELS.pop(name, None)
    STARTED.pop(name, None)

def _read_log(path):
    try:
        txt = open(path, errors="ignore").read()
    except FileNotFoundError:
        return ""
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", txt)

def utc_hm(ts):
    return time.strftime("%H:%M", time.gmtime(ts))


# ------------------------------- Telegram -------------------------------
def tg_send(text):
    """Envoie un message Telegram (HTML). Retourne True si OK. Ne logue jamais le token."""
    if not (TG_TOKEN and TG_USER):
        return False
    data = urllib.parse.urlencode({
        "chat_id": TG_USER, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        log(f"⚠️ Telegram : envoi impossible ({type(e).__name__}). Vérifiez le token, l'ID, et envoyez /start au bot.")
        return False

def build_message(reason, extra=None):
    user = "root" if ALLOW_ROOT_SSH else USERNAME
    L = [f"🖥️ <b>Serveur GitHub Actions</b> — {html.escape(reason)}"]
    if "ssh" in TUNNELS:
        h, p = TUNNELS["ssh"]
        L += ["", "🔑 <b>SSH</b>", f"<code>ssh {user}@{h} -p {p}</code>"]
    if "rdp" in TUNNELS:
        h, p = TUNNELS["rdp"]
        L += ["", "🖥️ <b>Bureau à distance</b> (mstsc)", f"Ordinateur : <code>{h}:{p}</code>",
              f"Utilisateur : <code>{USERNAME}</code> — session : Xorg"]
    if "web" in TUNNELS:
        L += ["", "🌐 <b>Web</b>", html.escape(TUNNELS["web"])]
    if extra:
        L += ["", html.escape(extra)]
    left = max(0, int((DEADLINE - time.time()) // 60))
    L += ["", f"⏱️ Arrêt prévu vers {utc_hm(DEADLINE)} UTC (dans ~{left} min). Les tunnels SSH/RDP changent toutes les ~{RENEW_MINUTES} min."]
    return "\n".join(L)

def notify(reason, extra=None):
    """Telegram si configuré ; sinon repli : affichage dans les logs (avec avertissement)."""
    msg = build_message(reason, extra)
    if tg_send(msg):
        log(f"📨 Telegram : message envoyé ({reason}).")
    else:
        log("⚠️ Telegram non disponible : adresses affichées dans les logs (visibles par tous si dépôt public).")
        log(re.sub(r"<[^>]+>", "", msg))


# ------------------------------- Tunnels -------------------------------
def start_pinggy(name, local_port, timeout=45):
    stop_proc(name)
    logf = f"{LOG_DIR}/{name}_tunnel.log"
    with open(logf, "w") as fh:
        PROCS[name] = subprocess.Popen(
            ["ssh", "-p", "443",
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
             "-R", f"0:localhost:{local_port}", "tcp@free.pinggy.io"],
            stdin=DEVNULL, stdout=fh, stderr=STDOUT)
    for _ in range(timeout):
        time.sleep(1)
        m = re.search(r"tcp://([A-Za-z0-9.-]+):(\d+)", _read_log(logf))
        if m:
            TUNNELS[name] = (m.group(1), m.group(2))
            STARTED[name] = time.time()
            return TUNNELS[name]
        if PROCS[name].poll() is not None:
            break
    log(f"❌ Tunnel Pinggy « {name} » impossible à ouvrir.")
    return None

def start_cloudflared(name, local_port, timeout=45):
    if not shutil.which("cloudflared"):
        log("[+] Installation de cloudflared...")
        sh("wget -q -O /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb"
           " && dpkg -i /tmp/cloudflared.deb > /dev/null && rm -f /tmp/cloudflared.deb")
    stop_proc(name)
    logf = f"{LOG_DIR}/{name}_tunnel.log"
    with open(logf, "w") as fh:
        PROCS[name] = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{local_port}"],
            stdin=DEVNULL, stdout=fh, stderr=STDOUT)
    for _ in range(timeout):
        time.sleep(1)
        m = re.search(r"https://[A-Za-z0-9-]+\.trycloudflare\.com", _read_log(logf))
        if m:
            TUNNELS[name] = m.group(0)
            STARTED[name] = time.time()
            return TUNNELS[name]
        if PROCS[name].poll() is not None:
            break
    log("❌ URL Cloudflare introuvable.")
    return None

def open_with_retry(fn, name, port, tries=3):
    for i in range(1, tries + 1):
        if fn(name, port):
            return True
        log(f"   nouvel essai {i}/{tries} pour « {name} »...")
        time.sleep(3)
    return False


# ------------------------------- Installation -------------------------------
def setup_base():
    log("[+] Système :")
    sh("head -n 2 /etc/os-release; uname -r; free -h | head -n 2; df -h / | tail -n 1")
    log("[+] Mise à jour d'APT...")
    sh("apt-get update -qq")
    log("[+] Installation : OpenSSH, Nginx et outils de base...")
    sh("apt-get install -y -qq openssh-server nginx sudo curl wget git unzip htop net-tools iproute2 tmux jq")
    service("apache2", "stop")   # libère le port 80 si Apache est présent sur l'image

def setup_users_and_ssh():
    global SERVER_PASSWORD
    generated = False
    if not SERVER_PASSWORD:
        alphabet = string.ascii_letters + string.digits
        SERVER_PASSWORD = "".join(secrets.choice(alphabet) for _ in range(20))
        generated = True
    if subprocess.run(["id", USERNAME], stdout=DEVNULL, stderr=DEVNULL).returncode != 0:
        sh(f"useradd -m -s /bin/bash -G sudo {USERNAME}")
    creds = f"{USERNAME}:{SERVER_PASSWORD}\n"
    if ALLOW_ROOT_SSH:
        creds += f"root:{SERVER_PASSWORD}\n"
    subprocess.run(["chpasswd"], input=creds, text=True, check=True)
    # sudo sans mot de passe pour l'utilisateur (pratique sur un serveur jetable)
    with open(f"/etc/sudoers.d/90-{USERNAME}", "w") as f:
        f.write(f"{USERNAME} ALL=(ALL) NOPASSWD:ALL\n")
    os.chmod(f"/etc/sudoers.d/90-{USERNAME}", 0o440)

    sshd_config = f"""Port 22
PermitRootLogin {"yes" if ALLOW_ROOT_SSH else "no"}
PasswordAuthentication yes
KbdInteractiveAuthentication no
UsePAM yes
X11Forwarding yes
PrintMotd no
ClientAliveInterval 30
ClientAliveCountMax 4
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
"""
    if os.path.exists("/etc/ssh/sshd_config") and not os.path.exists("/etc/ssh/sshd_config.bak"):
        shutil.copy("/etc/ssh/sshd_config", "/etc/ssh/sshd_config.bak")
    # Les fichiers drop-in (sshd_config.d) peuvent écraser nos réglages : on les neutralise
    sh("rm -f /etc/ssh/sshd_config.d/*.conf", check=False, quiet=True)
    with open("/etc/ssh/sshd_config", "w") as f:
        f.write(sshd_config)
    os.makedirs("/run/sshd", exist_ok=True)
    sh("ssh-keygen -A", quiet=True)
    sh("sshd -t")
    service("ssh", "restart")
    time.sleep(1)
    log("✅ SSH actif sur le port 22" if port_open(22) else "❌ SSH n'écoute pas sur le port 22")
    return generated

def setup_rdp():
    log("[+] Installation de XFCE4 et XRDP (quelques minutes)...")
    sh("apt-get install -y -qq xfce4 xfce4-terminal xrdp xorgxrdp dbus-x11 x11-xserver-utils")
    homes = [(USERNAME, f"/home/{USERNAME}")]
    if ALLOW_ROOT_SSH:
        homes.append(("root", "/root"))
    for user, home in homes:
        path = f"{home}/.xsession"
        with open(path, "w") as f:
            f.write("startxfce4\n")
        os.chmod(path, 0o755)
        sh(f"chown {user}:{user} {path}")
    sh("adduser xrdp ssl-cert > /dev/null 2>&1 || true", check=False, quiet=True)
    sh("sed -i 's/^max_bpp=32/max_bpp=16/' /etc/xrdp/xrdp.ini", check=False, quiet=True)
    service("dbus", "start")
    service("xrdp", "restart")
    time.sleep(2)
    log("✅ XRDP actif sur le port 3389" if port_open(3389) else "❌ XRDP n'écoute pas sur 3389")

def setup_web():
    with open("/var/www/html/index.html", "w") as f:
        f.write("<!doctype html><meta charset=utf-8><title>Serveur</title>"
                "<h1>Serveur GitHub Actions actif ✅</h1><p>Nginx fonctionne.</p>")
    service("nginx", "restart")
    log(f"✅ Nginx actif sur le port {WEB_PORT}" if port_open(WEB_PORT) else f"❌ Nginx n'écoute pas sur {WEB_PORT}")


# ------------------------------- Supervision -------------------------------
def shutdown(reason):
    for n in list(PROCS):
        stop_proc(n)
    tg_send(f"🛑 <b>Serveur GitHub Actions arrêté</b> — {html.escape(reason)}")
    log(f"🛑 Arrêt : {reason}")

def supervise():
    renew = RENEW_MINUTES * 60
    last_beat = time.time()
    warned = False
    while time.time() < DEADLINE:
        time.sleep(30)
        changed = []
        for name, port in PINGGY_PORTS.items():
            expired = (time.time() - STARTED.get(name, 0)) >= renew
            if not alive(name) or expired:
                why = "tunnel coupé" if not alive(name) else "renouvellement préventif"
                log(f"🔁 {name} : {why}, ouverture d'un nouveau tunnel...")
                if open_with_retry(start_pinggy, name, port, tries=2):
                    changed.append(name)
        if ENABLE_WEB and not alive("web"):
            log("🔁 web : tunnel Cloudflare coupé, relance...")
            if open_with_retry(start_cloudflared, "web", WEB_PORT, tries=2):
                changed.append("web")
        if changed:
            notify("nouvelle adresse (" + ", ".join(changed) + ")")
        if not warned and DEADLINE - time.time() < 10 * 60:
            warned = True
            tg_send("⚠️ <b>Le serveur s'arrête dans ~10 minutes</b> (limite de 6 h du job GitHub).")
        if time.time() - last_beat >= 300:
            last_beat = time.time()
            svc = {"SSH": port_open(22), "RDP": port_open(3389), "Web": port_open(WEB_PORT)}
            log(f"[{utc_hm(time.time())} UTC] " + " | ".join(f"{k} {'✅' if v else '⚪'}" for k, v in svc.items())
                + f" | reste ~{int((DEADLINE - time.time()) // 60)} min")


def main():
    if os.geteuid() != 0:
        sys.exit("❌ Ce script doit être lancé en root : sudo -E python3 -u server.py")
    if not (ENABLE_SSH or ENABLE_RDP or ENABLE_WEB):
        sys.exit("❌ Aucun service activé.")
    if not (TG_TOKEN and TG_USER):
        log("⚠️ TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID absents : les adresses seront affichées dans les logs.")

    def on_signal(signum, _frame):
        shutdown("job annulé ou interrompu")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    setup_base()
    generated = setup_users_and_ssh()
    if ENABLE_RDP:
        setup_rdp()
    if ENABLE_WEB:
        setup_web()

    for name, port in PINGGY_PORTS.items():
        open_with_retry(start_pinggy, name, port)
    if ENABLE_WEB:
        open_with_retry(start_cloudflared, "web", WEB_PORT)
    if not TUNNELS:
        shutdown("aucun tunnel n'a pu être ouvert")
        sys.exit(1)

    extra = f"🔐 Mot de passe généré : {SERVER_PASSWORD}" if generated else None
    notify("serveur prêt", extra)
    supervise()
    shutdown("durée maximale atteinte")


if __name__ == "__main__":
    main()
