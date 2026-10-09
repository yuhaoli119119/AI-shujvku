#!/usr/bin/env bash
exec /usr/bin/ncat -4 -k -m 16 --sh-exec '/usr/bin/ncat 127.0.0.1 8214' -l 172.18.0.1 8214
