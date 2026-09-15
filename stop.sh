#!/bin/bash
# 快捷：等同 bash manage.sh stop
exec bash "$(dirname "$0")/manage.sh" stop "$@"
