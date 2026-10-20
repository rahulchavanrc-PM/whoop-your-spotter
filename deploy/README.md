# Deploying Whoop-your-Spotter to an always-on VM

These steps were written for an Oracle VM (Always Free) running Ubuntu, with `ubuntu` as the default user. Any always-on Linux machine with Python 3.11+ and systemd works the same way. The app opens no port; the only inbound rule you need is SSH.

Below, `<KEY>` is your SSH private key and `<VM_IP>` is the VM's public IP. Free-tier public IPs can change if the VM is stopped and started.

**Important: run one copy only.** WHOOP's refresh token is single-use and rotates on every refresh, and it lives in `.env` on the machine that runs the app. Two copies (say, a laptop and the VM) will invalidate each other's token, and each would hold its own `coach.db`. Finish local testing, then stop the local copy before starting the VM copy. Your Slack app also delivers each message to only one connected copy.

## 1. Connect and prepare the machine

```bash
ssh -i <KEY> ubuntu@<VM_IP>
sudo apt update && sudo apt install -y python3-venv python3-pip git sqlite3
```

The app uses about 300 MB with a real message. On a small machine, add a swap file as cheap insurance against a crash if something spikes:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab
```

## 2. Get the code and install

```bash
git clone https://github.com/rahulchavanrc-PM/whoop-your-spotter.git
cd whoop-your-spotter
python3 -m venv venv
./venv/bin/pip install --no-cache-dir -r requirements.lock
./venv/bin/pytest -q
```

Install from `requirements.lock`, not `requirements.txt`: the lock pins every package so the server and your laptop cannot drift.

Optional, no secrets needed: measure memory with a fake AI endpoint.

```bash
./venv/bin/python deploy/fake_llm.py &       # note its PID, stop it afterwards
./venv/bin/python deploy/memcheck.py --fake
```

## 3. Copy your secrets and memory across (from your laptop)

`.env` and `coach.db` are git-ignored on purpose; copy them directly, never through GitHub. Stop the local copy first (find its PID and kill that, then confirm it is gone), then:

```bash
cd whoop-your-spotter
python -c "import sqlite3; c=sqlite3.connect('coach.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
scp -i <KEY> .env coach.db ubuntu@<VM_IP>:~/whoop-your-spotter/
ssh -i <KEY> ubuntu@<VM_IP> 'chmod 600 ~/whoop-your-spotter/.env'
```

(Starting fresh with no history? Copy just `.env`.) From now on, the VM is the only machine that may refresh the WHOOP token.

## 4. Run it as a service

```bash
sudo cp ~/whoop-your-spotter/deploy/coach.service /etc/systemd/system/coach.service
sudo systemctl daemon-reload
sudo systemctl enable --now coach
systemctl status coach
```

`enable` makes it start again by itself after a reboot, and `Restart=always` brings it back after a crash. The unit assumes the code is in `/home/ubuntu/whoop-your-spotter`; edit `WorkingDirectory` and `ExecStart` if yours differs.

## 5. Watch it work

```bash
journalctl -u coach -f
```

Look for `coach is listening on Socket Mode`, then DM the app. Every 5-minute check logs exactly one line, so progress is countable:

```bash
journalctl -u coach | grep -c "tick ok"                              # healthy checks so far
journalctl -u coach | grep -E "tick DEGRADED|token refreshed|401"    # trouble and token rotations
```

## 6. Redeploying after a change

```bash
cd ~/whoop-your-spotter
git pull
./venv/bin/pip install -r requirements.lock   # only matters if the lock changed
./venv/bin/pytest -q
sudo systemctl restart coach
```

## 7. Nightly backup of the memory (recommended)

`coach.db` is the coach's only memory. The backup job uploads a consistent copy every night to a private, versioned Object Storage bucket, using a **pre-authenticated link that can write one object name and nothing else**: it cannot read, list or delete, and no cloud login or key ever sits on the VM. Versioning keeps each night's upload, so an overwrite never destroys an earlier copy.

1. In your cloud console, create a private bucket (Standard tier) and turn **Object Versioning** on.
2. In the bucket, open **Pre-Authenticated Requests** and create one: access type **Permit object writes**, target **Object**, object name `coach.db`, and an expiry date (note it; you will need to renew the link before then).
3. Copy the URL (it is shown once) into `BACKUP_PAR_URL` in the VM's `.env`. Treat it like a password.
4. Install the timer and run one backup to test:

```bash
sudo cp ~/whoop-your-spotter/deploy/coach-backup.service ~/whoop-your-spotter/deploy/coach-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coach-backup.timer
sudo systemctl start coach-backup
sudo journalctl -u coach-backup -n 5     # expect "backup ok: <bytes>, sha256=<hash>"
systemctl list-timers coach-backup.timer
```

After its first success the coach watches the backup: if no upload succeeds for more than 36 hours, it messages you once a day with the last error (the usual cause is an expired link).

**Restore** needs only your console login: open the bucket, the `coach.db` object, **Versions**, and download one. To use it, stop the coach, put the file at `~/whoop-your-spotter/coach.db` (delete any `coach.db-wal` and `coach.db-shm` first), and start the coach.

**Renew the link before its expiry:** create a new pre-authenticated request the same way, replace `BACKUP_PAR_URL` in `.env`, and run `sudo systemctl start coach-backup`.

## 8. Upgrading dependencies

Never run `pip install -U` or install from `requirements.txt` on the server. To change a package on purpose:

1. On your laptop, in a fresh environment: `python3.11 -m venv /tmp/up && /tmp/up/bin/pip install -U -r requirements.txt && /tmp/up/bin/pytest`.
2. If the tests pass, regenerate the lock from it (`/tmp/up/bin/pip freeze`), keep the header comment, commit and push.
3. On the server, installing can spike memory on a small machine. Stop the coach first: `sudo systemctl stop coach`, `git pull`, `pip install -r requirements.lock`, `pytest -q`, then `sudo systemctl start coach`.
4. Send a real message to confirm it still works.

The lock has no hashes; that is acceptable for a single-user project.

## Handy commands

```bash
systemctl status coach          # is it running?
journalctl -u coach -n 100      # last 100 log lines
free -m                         # memory and swap
sudo systemctl restart coach
sudo systemctl stop coach
```

## A note on idle VMs

Cloud providers may reclaim free-tier machines that look idle for long enough. This app is mostly waiting, so it can look idle. Check on the VM after the first week, and decide for yourself what, if anything, to do about it.
