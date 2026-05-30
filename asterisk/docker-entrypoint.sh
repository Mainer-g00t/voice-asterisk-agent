#!/bin/sh
set -e

# Substitute ${ASTERISK_EXTERNAL_IP} in pjsip.conf at startup so the config
# is portable — each developer sets ASTERISK_EXTERNAL_IP in their .env file.
# Default: 127.0.0.1 works whenever baresip and Docker are on the same machine.
ASTERISK_EXTERNAL_IP="${ASTERISK_EXTERNAL_IP:-127.0.0.1}"
export ASTERISK_EXTERNAL_IP

AMI_SECRET="${AMI_SECRET:-voiceai_ami_secret}"
export AMI_SECRET

envsubst '${ASTERISK_EXTERNAL_IP}' \
    < /etc/asterisk/pjsip.conf.tmpl \
    > /etc/asterisk/pjsip.conf

envsubst '${AMI_SECRET}' \
    < /etc/asterisk/manager.conf.tmpl \
    > /etc/asterisk/manager.conf

echo "Asterisk external IP: ${ASTERISK_EXTERNAL_IP}"

exec /usr/sbin/asterisk -f "$@"
