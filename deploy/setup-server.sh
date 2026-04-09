#!/usr/bin/env bash
# Run once on VDS as root (adjust user/paths if needed).
set -euo pipefail
id taskbroker &>/dev/null || useradd --system --home-dir /opt/task-broker --create-home taskbroker
mkdir -p /opt/task-broker/data
chown -R taskbroker:taskbroker /opt/task-broker
echo "Install app files into /opt/task-broker, then:"
echo "  sudo -u taskbroker python3 -m venv /opt/task-broker/venv"
echo "  sudo -u taskbroker /opt/task-broker/venv/bin/pip install -r /opt/task-broker/requirements.txt"
echo "  sudo -u taskbroker cp /opt/task-broker/.env.example /opt/task-broker/.env && chmod 600 /opt/task-broker/.env"
echo "  edit .env, then: sudo systemctl enable --now task-broker"
