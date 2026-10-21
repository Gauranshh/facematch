# Launching FaceMatch as a public platform

FaceMatch is a Python server with a database and photo storage, so it can't run on GitHub Pages, Netlify or Vercel
(those only host static pages). You need a small server of your own. GitHub is just where the code lives.

## What you need
* A server (VPS) running Linux with Docker — about **2 vCPU / 4 GB RAM / 40+ GB disk** is a sensible start
  (face analysis uses the CPU; more users = more CPU and disk). Hetzner, DigitalOcean, Vultr, AWS Lightsail and Oracle Cloud's free tier all work.
* A domain name (e.g. `photos.yourname.com`) with an **A record** pointing at the server's IP address.

## Steps
```bash
# on the server
git clone https://github.com/Gauranshh/facematch.git && cd facematch
cp .env.example .env && nano .env            # DOMAIN, ADMIN_USER, ADMIN_PASSWORD
docker compose -f docker-compose.platform.yml up -d
```
Caddy fetches an HTTPS certificate automatically. Open `https://YOUR_DOMAIN`, sign in as the admin, and in **Admin → Platform**
make sure *Let anyone create an account* is on. People then see a **Create an account** link on the sign-in page.

Updating later: `git pull && docker compose -f docker-compose.platform.yml up -d --build`

## Before you invite the public
1. **Edit the privacy notice** (`static/privacy.html`): add your contact address and any rights that apply where you operate.
2. **Biometric law.** Face prints are biometric data. GDPR (EU/UK), Illinois BIPA, and others can require explicit consent and a retention policy.
   Sign-up already records consent; get legal advice if you operate commercially or at scale.
3. **Back up** the `facematch-data` Docker volume (accounts + photos). The app also writes daily index backups inside it.
4. **Storage limits.** The default is 2 GB per person (Admin → Platform). Raise or lower it to match your disk.
5. Admins can technically open every file on the server. The privacy notice says so; keep admin access to people you trust.

## Cost and scale
Indexing is the heavy part: roughly 1–5 photos/second per CPU core. A small VPS handles a handful of active users; for hundreds,
use a bigger machine (or more CPU cores) — photos are queued and analysed in the background, searches stay instant.

## Install as a phone app
Visit the site on a phone → **Install** (Android Chrome/Edge: browser menu, or the account menu's *Install as an app*) or
**Share → Add to Home Screen** (iPhone). It opens full-screen with its own icon, like a native app.
A store-listed native app (Google Play / App Store) would need a wrapper such as Capacitor; the installable web app covers most of that need.
