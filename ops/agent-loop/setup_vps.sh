#!/usr/bin/env bash
# One-time (and re-runnable) setup of the self-iteration loop on a Debian/Ubuntu
# VPS. Run as root from a checkout of the repository:
#   sudo bash ops/agent-loop/setup_vps.sh
# It never overwrites an existing config or secret file and never starts the
# loop; start it yourself after filling the configs (see README.md).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd)
REPO_URL=${REPO_URL:-https://github.com/WangMingCF/chamo.git}

echo "== packages"
apt-get update -q
apt-get install -y -q git python3 python3-venv python3-pip jq curl ca-certificates tmux \
    libreoffice-core libreoffice-common libreoffice-impress libreoffice-writer fonts-noto-cjk
if ! command -v gh >/dev/null; then
    # GitHub CLI from its official apt repository.
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list
    apt-get update -q && apt-get install -y -q gh
fi

echo "== users"
for u in chamo-agent chamo-review; do
    id "$u" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$u"
    chmod 700 "/home/$u"
done

echo "== rails (root-owned, read-only to the agents)"
install -d -m 755 /opt/chamo-agent /opt/chamo-agent/hooks /opt/chamo-agent/prompts
install -m 755 "$SRC"/run_loop.sh "$SRC"/review_prs.sh /opt/chamo-agent/
install -m 644 "$SRC"/lib.sh "$SRC"/privacy_scan.py /opt/chamo-agent/
install -m 755 "$SRC"/hooks/pre-push /opt/chamo-agent/hooks/
install -m 644 "$SRC"/prompts/*.md /opt/chamo-agent/prompts/
install -d -m 755 /etc/chamo-agent
[ -e /etc/chamo-agent/loop.conf ] || install -m 644 "$SRC/config/loop.conf.example" /etc/chamo-agent/loop.conf

echo "== per-user config, git identity and hooks"
for u in chamo-agent chamo-review; do
    role=${u#chamo-}
    home=/home/$u
    install -d -o "$u" -g "$u" -m 700 "$home/.config/chamo-agent"
    [ -e "$home/.config/chamo-agent/$role.env" ] || \
        install -o "$u" -g "$u" -m 600 "$SRC/config/$role.env.example" "$home/.config/chamo-agent/$role.env"
    # No real identity ever exists on this machine.
    sudo -u "$u" git config --global user.name Chamo
    sudo -u "$u" git config --global user.email dev@chamo.invalid
    sudo -u "$u" git config --global commit.gpgsign false
    sudo -u "$u" git config --global core.hooksPath /opt/chamo-agent/hooks
    sudo -u "$u" git config --global credential.https://github.com.helper '!gh auth git-credential'
done

echo "== agent clone and Python environment"
if [ ! -d /home/chamo-agent/chamo/.git ]; then
    sudo -u chamo-agent git clone -q "$REPO_URL" /home/chamo-agent/chamo
fi
if [ ! -x /home/chamo-agent/venv/bin/python ]; then
    sudo -u chamo-agent python3 -m venv /home/chamo-agent/venv
fi
sudo -u chamo-agent /home/chamo-agent/venv/bin/pip install -q --upgrade pip
sudo -u chamo-agent /home/chamo-agent/venv/bin/pip install -q -r /home/chamo-agent/chamo/third_party/requirements.lock

echo "== systemd"
install -m 644 "$SRC"/systemd/chamo-agent.service "$SRC"/systemd/chamo-review.service \
    "$SRC"/systemd/chamo-review.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable chamo-agent.service chamo-review.timer >/dev/null

cat <<'EOF'

Setup finished. Nothing is running yet. Before starting:
  1. Fill /etc/chamo-agent/loop.conf (STATUS_ISSUE at least).
  2. Fill /home/chamo-agent/.config/chamo-agent/agent.env and
     /home/chamo-review/.config/chamo-agent/review.env (two different tokens).
  3. Copy the hashed keyword file to both users:
     /home/<user>/.config/chamo-agent/private-keywords.txt (mode 600).
  4. Install your harness for both users and check HARNESS_CMD / REVIEW_CMD.
  5. Dry run:   sudo -u chamo-agent DRY_RUN=1 /opt/chamo-agent/run_loop.sh --once
  6. Start:     systemctl start chamo-review.timer chamo-agent.service
Stop at any time:  touch /etc/chamo-agent/STOP   (or open an issue labelled agent-stop)
EOF
