#!/bin/sh
set -eu
umask 077
python /app/bootstrap.py
exec supervisord -c /etc/supervisord.conf
