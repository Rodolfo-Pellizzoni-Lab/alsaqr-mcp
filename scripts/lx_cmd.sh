#!/bin/bash
# lx_cmd.sh <uart.log> <uart dev> "<command>" [timeout_s] -- type a command into the Linux shell on the board's console
# one character at a time (the SoC UART overruns on bursts) and print what the console captured until the next prompt.
log=$1; dev=$2; cmd=$3; to=${4:-30}
off=$(stat -c %s "$log")
exec 3>"$dev"
for ((i=0; i<${#cmd}; i++)); do printf '%s' "${cmd:$i:1}" >&3; sleep 0.03; done
printf '\r' >&3; exec 3>&-
for ((t=0; t<to*10; t++)); do
  sleep 0.1
  tail -c +$((off+1)) "$log" | tr -d '\r' | grep -qE '(^|\n)(\$|#) $' && break
done
tail -c +$((off+1)) "$log" | tr -d '\r' | sed '1d' | sed -E '$ s/^(\$|#) $//' | grep -vE '^\s*$'
